import tempfile
import threading
import unittest
from pathlib import Path

from media_lab_core.job_store import InvalidTransition, JobStore


class JobStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = JobStore(Path(self.temp.name) / "jobs.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_claim_is_atomic_across_workers(self):
        jid = self.store.enqueue("video", {"seed": 7})
        barrier = threading.Barrier(3)
        claimed = []

        def claim(worker):
            barrier.wait()
            value = self.store.claim_next(worker)
            claimed.append(value["id"] if value else None)

        threads = [threading.Thread(target=claim, args=(f"worker-{n}",)) for n in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual([jid], [x for x in claimed if x])

    def test_queued_cancel_cannot_be_claimed(self):
        jid = self.store.enqueue("video", {})
        self.assertTrue(self.store.request_cancel(jid))
        self.assertIsNone(self.store.claim_next("worker"))
        self.assertEqual("cancelled", self.store.get(jid)["status"])

    def test_running_cancel_is_durable_and_terminal(self):
        jid = self.store.enqueue("video", {})
        job = self.store.claim_next("worker")
        self.assertEqual(jid, job["id"])
        self.assertTrue(self.store.request_cancel(jid))
        self.assertEqual("cancel_requested", self.store.get(jid)["status"])
        done = self.store.transition(jid, "worker", "cancelled")
        self.assertEqual("cancelled", done["status"])
        with self.assertRaises(InvalidTransition):
            self.store.transition(jid, "worker", "succeeded")

    def test_non_owner_cannot_publish(self):
        jid = self.store.enqueue("video", {})
        self.store.claim_next("worker-a")
        with self.assertRaises(InvalidTransition):
            self.store.transition(jid, "worker-b", "succeeded", result={"path": "x.mp4"})


if __name__ == "__main__":
    unittest.main()
