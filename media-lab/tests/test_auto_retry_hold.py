"""Maintenance marker suspends AUTO retries without touching jobs/queues."""
from copy import deepcopy
import threading
import unittest
from unittest import mock

import app as studio

class AutoRetryHold(unittest.TestCase):
    def test_maintenance_preserves_failed_jobs_and_queues(self):
        jobs={'creep':{'status':'error','kind':'musicvideo','retryable':True,
                       'finished':100,'auto_retries':0,'request':{'engine':'h3'}}}
        before=deepcopy(jobs); queue=[]; online=['other-online']
        with mock.patch.multiple(studio,jobs=jobs,queue=queue,online_queue=online,
                                 cv=threading.Condition()), \
             mock.patch.object(studio,'ENGINE_MAINTENANCE') as marker, \
             mock.patch.object(studio.time,'time',return_value=101), \
             mock.patch.object(studio,'save_state') as save:
            marker.exists.return_value=True
            studio.auto_requeue()
        self.assertEqual(jobs,before)
        self.assertEqual(queue,[])
        self.assertEqual(online,['other-online'])
        save.assert_not_called()

    def test_unheld_existing_retry_budget_still_applies(self):
        jobs={'eligible':{'status':'error','retryable':True,'finished':100,'auto_retries':0},
              'exhausted':{'status':'error','retryable':True,'finished':100,'auto_retries':3},
              'cancelled':{'status':'error','retryable':True,'finished':100,'cancel':True}}
        queue=[]
        # This test isolates the retry budget. Execution evidence itself is
        # exercised by test_retry_evidence_boundaries and must never be bypassed
        # in production merely because maintenance is absent.
        with mock.patch.multiple(studio,jobs=jobs,queue=queue,cv=threading.Condition()), \
             mock.patch.object(studio,'_auto_retry_recovery_error',return_value=None), \
             mock.patch.object(studio,'ENGINE_MAINTENANCE') as marker, \
             mock.patch.object(studio.time,'time',return_value=101), \
             mock.patch.object(studio,'save_state'):
            marker.exists.return_value=False
            studio.auto_requeue()
        self.assertEqual(queue,['eligible'])
        self.assertEqual(jobs['eligible']['auto_retries'],1)
        self.assertEqual(jobs['exhausted']['status'],'error')
        self.assertEqual(jobs['cancelled']['status'],'error')

    def test_existing_queued_jobs_are_untouched(self):
        jobs={'queued':{'status':'queued'},'failed':{'status':'error','retryable':True}}
        queue=['queued'];before=deepcopy(jobs)
        with mock.patch.multiple(studio,jobs=jobs,queue=queue), \
             mock.patch.object(studio,'ENGINE_MAINTENANCE') as marker:
            marker.exists.return_value=True
            studio.auto_requeue()
        self.assertEqual(jobs,before)
        self.assertEqual(queue,['queued'])
