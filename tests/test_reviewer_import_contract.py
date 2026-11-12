"""Public import contract: containment, not a successful activation claim."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from finix import core, recovery_import as subject


class PublicImportContract(unittest.TestCase):
    def test_unsupported_public_contract_refuses_before_dispatch(self):
        # Mocked negative control: no real profile import is authorized here.
        with patch.object(subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            with self.assertRaises(subject.RecoveryImportError) as caught:
                subject._host_import(SimpleNamespace(__file__=__file__), Path('missing.tar.gz'),
                                     'recovered', Path('target'), Path('stage'))
        self.assertEqual(caught.exception.code, 'HOST_IMPORT_CONTRACT_UNSUPPORTED')
        run.assert_not_called()

    def test_legacy_worker_refuses_before_sdk_or_global_state_changes(self):
        before_temp = tempfile.tempdir
        before_env = dict(os.environ)
        # No SDK is required to reject the removed child protocol.
        with self.assertRaises(subject.RecoveryImportError) as caught:
            subject._host_worker('missing.tar.gz', 'recovered', 'target', 'stage')
        self.assertEqual(caught.exception.code, 'HOST_IMPORT_CONTRACT_UNSUPPORTED')
        self.assertEqual(tempfile.tempdir, before_temp)
        self.assertEqual(dict(os.environ), before_env)

    def test_preflight_does_not_advertise_unsafe_import_as_compatible(self):
        with self.assertRaises(subject.RecoveryImportError) as caught:
            subject.verify_host_compatibility(SimpleNamespace())
        self.assertEqual(caught.exception.code, 'HOST_IMPORT_CONTRACT_UNSUPPORTED')

    def test_real_importer_refuses_before_candidate_handoff(self):
        try:
            from hermes_cli import profiles
        except ImportError:
            self.skipTest('SDK dependency closure not selected')
        with self.assertRaises(subject.RecoveryImportError) as caught:
            subject.RecoveryImporter(SimpleNamespace()).import_job('job', profile_id='source',
                activation='epoch', name='recovered', consent_import=True)
        self.assertEqual(caught.exception.code, 'HOST_IMPORT_CONTRACT_UNSUPPORTED')

    def test_actual_sdk_public_cli_parser_and_root_guard_in_separate_process(self):
        try:
            import hermes_constants
        except ImportError:
            self.skipTest('SDK dependency closure not selected')
        scratch = Path(os.environ['TMPDIR'])
        with tempfile.TemporaryDirectory(dir=scratch, prefix='reviewer-public-cli-') as tmp:
            root = core.private_dir(Path(tmp) / 'root')
            env = dict(os.environ, HERMES_HOME=str(root), HERMES_DISABLE_LAZY_INSTALLS='1',
                       PYTHONDONTWRITEBYTECODE='1')
            code = '''
import argparse, os
from pathlib import Path
from hermes_constants import get_default_hermes_root
from hermes_cli.profiles import get_profile_dir
root = Path(os.environ['HERMES_HOME'])
root_matches = Path(get_default_hermes_root()).resolve() == root.resolve()
target_matches = Path(get_profile_dir('recovered')).resolve() == (root/'profiles/recovered').resolve()
from hermes_cli.subcommands.profile import build_profile_parser
from hermes_cli.profile_cmd import cmd_profile
parser = argparse.ArgumentParser(prog='hermes')
build_profile_parser(parser.add_subparsers(dest='command'), cmd_profile=cmd_profile)
args = parser.parse_args(['profile', 'import', str(root/'missing.tar.gz'), '--name', 'recovered'])
assert args.archive == str(root/'missing.tar.gz') and args.import_name == 'recovered'
print('PUBLIC_CLI_ENTRY', flush=True)
if not (root_matches and target_matches):
    print('ROOT_GUARD_REFUSED_DISPATCH', flush=True)
    raise SystemExit(3)
args.func(args)
'''
            result = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True,
                                    text=True, timeout=60)
            self.assertIn('PUBLIC_CLI_ENTRY', result.stdout, result.stderr)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root/'profiles/recovered').exists())


if __name__ == '__main__':
    unittest.main()
