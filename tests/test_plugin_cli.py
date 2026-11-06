"""Native SDK registration/parser/handler contracts with a disposable profile."""
import argparse
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from finix import core
from finix.integration import Profile
from finix.plugin_cli import run, setup_parser


class PluginCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / 'profile'
        self.home.mkdir(mode=0o700)
        (self.home / 'config.yaml').write_text('synthetic: true\n')
        self.profile = Profile(self.home, default_root=self.home)
        self.key = Path(self.tmp.name) / 'key'
        self.recipient = core.keygen(self.key)
        self.parser = argparse.ArgumentParser()
        commands = self.parser.add_subparsers()
        parser = commands.add_parser('finix')
        setup_parser(parser)
        parser.set_defaults(func=lambda args: run(args, profile_factory=lambda: self.profile))

    def invoke(self, *args):
        output = io.StringIO()
        parsed = self.parser.parse_args(['finix', *args])
        with redirect_stdout(output):
            result = parsed.func(parsed)
        return result, json.loads(output.getvalue())

    def test_no_args_noninteractive_never_reads_stdin(self):
        with patch('sys.stdin.isatty', return_value=False), patch('builtins.input', side_effect=AssertionError):
            code, view = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(view['stage'], 'key')
        self.assertIn('hermes finix', view['help'])

    def test_key_check_schedule_status_real_archive(self):
        code, missing = self.invoke('key')
        self.assertEqual(code, 0)
        self.assertTrue(Path(missing['offline_page_path']).is_file())
        self.assertEqual(Path(missing['offline_page_path']).as_uri(), missing['offline_page_url'])
        self.assertEqual(self.invoke('key', '--recipient', self.recipient)[0], 0)
        code, checked = self.invoke('check')
        self.assertEqual(code, 0)
        self.assertTrue(Path(checked['first_backup']['path']).is_file())
        self.assertEqual(self.invoke('schedule', '7d')[1]['interval_hours'], 168)
        self.assertEqual(self.invoke('status')[1]['stage'], 'complete')
        self.assertEqual(self.invoke('key')[1]['recipient'], self.recipient)
        replacement = core.keygen(Path(self.tmp.name) / 'replacement')
        self.assertEqual(self.invoke('key', '--recipient', replacement)[0], 1)
        self.assertEqual(self.profile.config()['recipient'], self.recipient)

    def test_schedule_before_check_and_invalid_period_fail(self):
        self.assertEqual(self.invoke('schedule', '24h')[0], 1)
        self.assertEqual(self.invoke('schedule', '1.5h')[0], 1)
        self.assertEqual(self.profile.config()['interval_hours'], 0)

    def test_connect_help_local_and_consent(self):
        self.assertIn('instructions', self.invoke('connect')[1])
        self.assertEqual(self.invoke('connect', 'local')[1]['storage'], 'local')
        self.assertEqual(self.invoke('connect', 'yandex', '--mode', 'tunnel')[0], 1)
        with patch('sys.stdin.isatty', return_value=False):
            code, value = self.invoke('connect', 'yandex', '--mode', 'tunnel', '--confirm')
        self.assertEqual(code, 1)
        self.assertIn('INTERACTIVE_TERMINAL_REQUIRED', value['error'])

    def test_wizard_numbered_key_and_successful_backup_then_period(self):
        self.profile.recipient(self.profile.profile_id, self.recipient, None)
        parsed = self.parser.parse_args(['finix'])
        output = io.StringIO()
        with redirect_stdout(output), patch('sys.stdin.isatty', return_value=True), \
                patch.object(output, 'isatty', return_value=True), \
                patch('builtins.input', side_effect=['1', '3', 'run', '24h', '0']):
            self.assertEqual(parsed.func(parsed), 0)
        self.assertIn('1. Key', output.getvalue())
        self.assertIn('2. Cloud', output.getvalue())
        self.assertIn('3. Check everything', output.getvalue())
        self.assertEqual(self.profile.config()['interval_hours'], 24)

    def test_real_sdk_cli_registration_and_native_parser_wiring(self):
        from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
        manager = PluginManager(scope_key=str(self.home))
        manifest = PluginManifest(name='finix', path=str(Path(__file__).parents[1]), source='user')
        ctx = PluginContext(manifest, manager)
        from finix.integration import register
        with patch('hermes_constants.get_hermes_home', return_value=self.home), \
                patch('finix.extra_sources.trusted_root', return_value=self.home):
            register(ctx)
        entry = manager._cli_commands['finix']
        parser = argparse.ArgumentParser()
        commands = parser.add_subparsers()
        child = commands.add_parser('finix', help=entry['help'])
        entry['setup_fn'](child)
        child.set_defaults(func=entry['handler_fn'])
        for argv in (['key', '--recipient', self.recipient], ['check'], ['schedule', '24h'], ['status']):
            args = parser.parse_args(['finix', *argv])
            output = io.StringIO()
            with redirect_stdout(output), \
                    patch('hermes_constants.get_hermes_home', side_effect=AssertionError('owning scope must be captured')):
                self.assertEqual(args.func(args), 0)
            state = json.loads(output.getvalue())
            self.assertEqual(state['profile_id'], self.profile.profile_id)
        self.assertEqual(state['stage'], 'complete')


if __name__ == '__main__':
    unittest.main()
