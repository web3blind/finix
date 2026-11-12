import argparse,io,json,os,unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from finix import plugin_cli,default_recovery_api as api

class CLIProtocolTests(unittest.TestCase):
    def test_new_runtime_modules_ship_in_native_and_wheel_payload(self):
        import json,tomllib
        root=Path(__file__).resolve().parents[1]
        native=json.loads((root/'scripts/release/allowlist.json').read_text())
        wheel=tomllib.loads((root/'pyproject.toml').read_text())['tool']['setuptools']['data-files']['share/finix/finix']
        for name in ('preview_budget','default_preview_jobs','default_preview_cli','default_preview_worker'):
            self.assertIn('finix/'+name+'.py',native)
            self.assertIn('finix/'+name+'.py',wheel)

    def test_registered_prepare_uses_detached_shared_owner_before_key_read(self):
        parser=argparse.ArgumentParser();plugin_cli.setup_parser(parser)
        # Parser absence is an explicit missing-protocol assertion, not SystemExit.
        prepare=next(a for a in parser._subparsers._group_actions if hasattr(a,'choices')).choices['recovery']
        commands=prepare._subparsers._group_actions[0].choices
        self.assertIn('--request-id',commands['default-prepare']._option_string_actions,'CLI lacks durable request identity')
        args=parser.parse_args(['recovery','default-prepare','--request-id','b'*32,'--copy-id','c'*64,'--identity','unopened-reference','--consent-prepare'])
        profile=SimpleNamespace(home=Path(os.environ['FINIX_PREVIEW_TEST_ROOT']),profile_id='a'*64)
        ack=dict(contract='default-preview-job-v1',id='b'*32,state='running',error=None,result=None,cancellable=True)
        with patch.object(api,'authority',return_value=profile.home),patch('finix.default_preview_cli.start',return_value=ack) as start,patch.object(api,'new_service',side_effect=AssertionError('parent opened key or decrypted')),redirect_stdout(io.StringIO()) as out:
            self.assertEqual(plugin_cli.run(args,lambda:profile),0)
        self.assertEqual(json.loads(out.getvalue()),ack);start.assert_called_once()

    def test_registered_preview_status_is_read_only_shared_envelope(self):
        parser=argparse.ArgumentParser();plugin_cli.setup_parser(parser)
        recovery=parser._subparsers._group_actions[0].choices['recovery']
        self.assertIn('default-preview',recovery._subparsers._group_actions[0].choices,'CLI lacks exact preview GET equivalent')
        args=parser.parse_args(['recovery','default-preview','b'*32])
        profile=SimpleNamespace(home=Path(os.environ['FINIX_PREVIEW_TEST_ROOT']),profile_id='a'*64)
        answer=dict(contract='default-preview-job-v1',id='b'*32,state='failed',error='PREVIEW_DEADLINE',result=None,cancellable=False)
        with patch.object(api,'authority',return_value=profile.home),patch('finix.default_preview_jobs.PreviewJobs.status',return_value=answer) as status,redirect_stdout(io.StringIO()) as out:
            self.assertEqual(plugin_cli.run(args,lambda:profile),0)
        self.assertEqual(json.loads(out.getvalue()),answer);status.assert_called_once_with(profile,'b'*32)

if __name__=='__main__':unittest.main()
