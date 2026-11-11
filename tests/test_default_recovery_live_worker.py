"""Synthetic native-root tests; never select a personal default."""
import importlib.util
import json
import os
from pathlib import Path
import unittest

class AdmissionTests(unittest.TestCase):
    def test_missing_live_controller(self):
        self.assertIsNotNone(importlib.util.find_spec('finix.default_recovery_lifecycle'),
                             'Detached default controller is not implemented')

    def test_empty_stores_admitted_due_sibling_and_unknown_refused(self):
        from finix import core
        from finix.default_recovery_lifecycle import preflight
        from finix.default_recovery import RecoveryError
        from hermes_constants import get_default_hermes_root
        root = get_default_hermes_root()
        core.private_dir(root)
        core.atomic_json(root/'config.yaml', {'gateway':{'standalone':True,'multiplex_profiles':False}})
        sibling=core.private_dir(root/'profiles/named-sentinel')
        core.atomic_json(sibling/'config.yaml', {})
        (sibling/'sentinel').write_text('never-dispatched')
        self.assertEqual(set(preflight(root)), {str(root),str(sibling)})
        cron=core.private_dir(sibling/'cron')
        for store in [{'jobs':[{'id':'due-sentinel','enabled':True,'next_run_at':'2020-01-01T00:00:00Z'}]}, {'jobs':{}}, {'unknown':[]}]:
            core.atomic_json(cron/'jobs.json',store)
            with self.assertRaises(RecoveryError) as caught: preflight(root)
            self.assertEqual(caught.exception.code,'SIBLING_AUTORUN_NOT_SUPPORTED')
            self.assertEqual((sibling/'sentinel').read_text(),'never-dispatched')
        (cron/'jobs.json').unlink()
        core.atomic_json(root/'config.yaml', {'cron':{'provider':'invented-disabled'}})
        with self.assertRaises(RecoveryError) as caught: preflight(root)
        self.assertEqual(caught.exception.code,'DEFAULT_AUTORUN_NOT_SUPPORTED')
        core.atomic_json(root/'config.yaml', {'hooks':{'startup':'anything'}})
        with self.assertRaises(RecoveryError): preflight(root)
        for malformed in ({'model':7},{'model':{'default':[]}},{'gateway':{'platforms':{'local':{'enabled':1}}}}):
            core.atomic_json(root/'config.yaml',malformed)
            with self.assertRaises(RecoveryError): preflight(root)
        core.atomic_json(root/'config.yaml', {})

    def test_writer_fence_and_server_job_validation(self):
        from finix import core
        from finix.default_recovery_lifecycle import writer_fence, ColdController, _job
        from finix.default_recovery import RecoveryError
        from hermes_constants import get_default_hermes_root
        root=get_default_hermes_root()
        core.private_dir(root)
        with writer_fence(root):
            self.assertTrue((root/'.backup.lock').exists())
        controller=ColdController(root)
        for invalid in ('../other','0'*31,'G'*32,False):
            with self.assertRaises(RecoveryError): controller.status(invalid)
        self.assertEqual(_job(root,'0'*32),root/'cache/finix/default-controller'/('0'*32))

if __name__=='__main__': unittest.main()
