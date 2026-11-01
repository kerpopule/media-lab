"""Advisory model ONLY: no collector, authentication, durable IO or actuator.

A future trusted coordinator must authenticate begin_boot events, serialize them
with observations, and durably commit returned state before accepting samples or
acting on a proposal. Never reconstruct an empty State after ledger corruption.
"""
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class State:
    boot_id: str = ''
    sequence: int = -1
    failure_samples: int = 0
    reboot_requests: int = 0
    quarantined: bool = True
    incident_id: str = ''
    epoch: int = 0
    retired_boot_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Sample:
    boot_id: str
    sequence: int
    host_stalled: bool = False
    independent_confirmation: bool = False
    quarantine_verified: bool = False
    evidence_saved: bool = False
    ledger_writable: bool = False
    incident_id: str = ''
    epoch: int = 0


def _identity(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 256


def _valid_state(state):
    if not isinstance(state, State):
        return False
    if not (type(state.epoch) is int and state.epoch >= 0
            and type(state.sequence) is int and state.sequence >= -1
            and type(state.failure_samples) is int and 0 <= state.failure_samples <= 3
            and type(state.reboot_requests) is int and state.reboot_requests in (0, 1)
            and state.quarantined is True
            and isinstance(state.retired_boot_ids, (tuple, list))
            and all(_identity(b) for b in state.retired_boot_ids)):
        return False
    retired = state.retired_boot_ids
    if len(set(retired)) != len(retired) or state.boot_id in retired:
        return False
    if state.epoch == 0:
        return state == State()
    return (_identity(state.boot_id) and _identity(state.incident_id)
            and len(retired) == state.epoch - 1)


def begin_boot(state: State, *, incident_id: str, boot_id: str, epoch: int) -> State:
    """Explicit trusted transition; not callable from ordinary observation input.

    Caller authenticates actual boot identity and commits this result durably.
    Incident reset is deliberately absent. Budget survives every transition.
    Invalid/corrupt ledgers raise; callers must quarantine/alert, NOT use State().
    """
    if not _valid_state(state):
        raise ValueError('invalid recovery ledger')
    if not (_identity(incident_id) and _identity(boot_id)
            and type(epoch) is int and epoch == state.epoch + 1):
        raise ValueError('invalid boot transition')
    if state.epoch and incident_id != state.incident_id:
        raise ValueError('incident reset requires separate operator provisioning')
    if boot_id == state.boot_id or boot_id in state.retired_boot_ids:
        raise ValueError('boot identity already used')
    retired = tuple(state.retired_boot_ids) + ((state.boot_id,) if state.epoch else ())
    return replace(state, boot_id=boot_id, incident_id=incident_id, epoch=epoch,
                   retired_boot_ids=retired, sequence=-1, failure_samples=0)


def observe(state: State, sample: Sample) -> tuple[State, str]:
    """Ordinary observations NEVER establish or change the boot epoch.

    Three samples require trusted spaced collection; cadence/authentication and
    hard IO deadlines are obligations of a future executor, not implemented here.
    Persist the spent budget BEFORE any separately authorized actuation.
    """
    if not _valid_state(state) or state.epoch == 0:
        return state, 'invalid_ledger_alert'
    if not (isinstance(sample, Sample) and type(sample.epoch) is int
            and sample.epoch == state.epoch and sample.boot_id == state.boot_id
            and sample.incident_id == state.incident_id):
        return state, 'reject_epoch_sample'
    if not (type(sample.sequence) is int and sample.sequence >= 0
            and all(type(getattr(sample, field)) is bool for field in (
                'host_stalled', 'independent_confirmation', 'quarantine_verified',
                'evidence_saved', 'ledger_writable'))):
        return state, 'reject_invalid_sample'
    if sample.sequence <= state.sequence:
        return state, 'reject_stale_sample'
    state = replace(state, sequence=sample.sequence)
    if not (sample.quarantine_verified and sample.evidence_saved and sample.ledger_writable):
        return replace(state, failure_samples=0), 'quarantine_and_alert'
    if not (sample.host_stalled and sample.independent_confirmation):
        return replace(state, failure_samples=0), 'observe_and_alert'
    if state.reboot_requests >= 1:
        return state, 'budget_exhausted_alert'
    state = replace(state, failure_samples=state.failure_samples + 1)
    if state.failure_samples < 3:
        return state, 'observe_and_alert'
    return replace(state, reboot_requests=1), 'propose_reboot'
