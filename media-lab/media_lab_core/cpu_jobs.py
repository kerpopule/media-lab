"""Shared owned CPU queue lifecycle. Caller holds one canonical root across engines."""
import hashlib
from pathlib import Path
import time
import uuid
from .cpu_worker import cpu_slot, WorkerBusy
from .job_store import InvalidTransition


def run_next_cpu(store, *, root, engine, kind, stage, validate, execute,
                 failure_message, read_input=None, prepare_input=None):
    root = Path(root).resolve()
    with cpu_slot(root) as fd:
        worker_id = engine + '-' + uuid.uuid4().hex
        job = store.claim_cpu_job(worker_id, engine, kind=kind)
        if job is None:
            return None
        jid = job['id']
        last_heartbeat = 0.0

        def cancelled():
            nonlocal last_heartbeat
            current = store.get(jid)
            if current is None or current['claimed_by'] != worker_id:
                raise InvalidTransition('The CPU worker no longer owns this job.')
            if current['status'] != 'running':
                return True
            if time.monotonic() - last_heartbeat >= 5:
                if not store.heartbeat(jid, worker_id, stage):
                    raise InvalidTransition('The CPU worker lease could not be renewed.')
                last_heartbeat = time.monotonic()
            return False

        def finish(target, **values):
            current = store.get(jid)
            if current and current['status'] == 'cancel_requested':
                target, values = 'cancelled', {}
            try:
                return store.transition(jid, worker_id, target, **values)
            except InvalidTransition:
                # Cancellation can commit after our read but before success.
                current = store.get(jid)
                if current and current['claimed_by'] == worker_id and current['status'] == 'cancel_requested':
                    return store.transition(jid, worker_id, 'cancelled')
                raise

        try:
            validate(job['payload'])
            if cancelled():
                return finish('cancelled')
            if prepare_input is None:
                data = (read_input(job) if read_input is not None else
                        store.read_job_input(jid, job['payload']['settings']['inputId']))
                if not isinstance(data, bytes) or len(data) > 20 * 1024**2:
                    raise ValueError('The accepted input is invalid.')
                if hashlib.sha256(data).hexdigest() != job['payload']['settings']['inputSha256']:
                    raise ValueError('The accepted input changed.')
            else:
                # Explicit adapter-owned preparation for immutable text payloads.
                data = prepare_input(job)
                if not isinstance(data, bytes) or len(data) > 20 * 1024**2:
                    raise ValueError('The prepared input is invalid.')
            artifact = execute(job_id=jid, data=data, root=root, cancelled=cancelled, lock_fd=fd)
        except WorkerBusy:
            # A shared resource (GPU lease, memory) is busy: the job goes back to the queue unchanged.
            store.release(jid, worker_id)
            raise
        except Exception:
            # Raw decoder/runtime diagnostics may contain local paths. Keep
            # durable client-facing state free of those details.
            return finish('failed', error=failure_message)
        return finish('succeeded', result={'artifact': artifact})
