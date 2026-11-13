"""Admission of the real CURRENT runtime with inert source-distribution helpers."""
import os
from pathlib import Path
import unittest
import uuid
from finix import core,default_recovery_lifecycle as lifecycle


class CurrentRuntimeSourceLayoutTests(unittest.TestCase):
    def test_inert_repo_helpers_do_not_change_current_runtime_admission(self):
        root=core.private_dir(Path(os.environ['TMPDIR'])/('finix-current-layout-'+uuid.uuid4().hex))
        installed=core.private_dir(root/'plugins/finix')
        source=Path(lifecycle.__file__).resolve().parent.parent
        names=['__init__.py','plugin.yaml','dashboard/plugin_api.py']+['finix/'+p.name for p in sorted((source/'finix').glob('*.py'))]
        for name in names:
            target=installed/name;core.private_dir(target.parent)
            with core.regular_output(target) as fd:os.write(fd,(source/name).read_bytes())
        with core.regular_output(root/'config.yaml') as fd:os.write(fd,b'plugins:\n  enabled: [finix]\n')
        expected=lifecycle._current_finix(root)
        for name in ('tests/test_fixture.py','scripts/release_check.py'):
            target=installed/name;core.private_dir(target.parent)
            with core.regular_output(target) as fd:os.write(fd,b'# inert source-distribution helper; never imported\n')
        self.assertEqual(lifecycle._current_finix(root),expected)
        # Runtime-root shadowing remains forbidden.
        with core.regular_output(installed/'json.py') as fd:os.write(fd,b'# unexpected top-level runtime module\n')
        with self.assertRaises(lifecycle.RecoveryError) as error:lifecycle._current_finix(root)
        self.assertEqual(error.exception.code,'CURRENT_FINIX_CHANGED')


if __name__=='__main__':unittest.main()
