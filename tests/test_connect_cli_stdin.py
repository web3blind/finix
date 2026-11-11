"""Real stdin transports; fake controller, no profile/OAuth/network."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

RUNNER = r'''
import json, sys
from pathlib import Path
from finix import connect_cli as cli
class Controller:
    def execute(self, profile, words, actor):
        print('ACTION ' + json.dumps(words), flush=True)
        status = 'cancelled' if words[0] == 'cancel' else 'applied' if words[0] == 'apply' else 'waiting'
        return json.dumps({'id': 'local-test', 'status': status})
    def close(self):
        print('CLOSED', flush=True)
cli.Profile = lambda home: object()
cli.ConnectCommands = Controller
raise SystemExit(cli.interactive('yandex', str(Path.cwd()), 'server-browser', confirm=True))
'''

class NativeStdinTests(unittest.TestCase):
    def run_cli(self, text):
        return subprocess.run([sys.executable, '-u', '-c', RUNNER], input=text,
                              text=True, capture_output=True, timeout=8)

    def run_held_pipe(self, runner, partial=''):
        p = subprocess.Popen([sys.executable, '-u', '-c', runner], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            if partial:
                p.stdin.write(partial)
                p.stdin.flush()
            p.wait(timeout=4)  # stdin remains open: a blocking read cannot pass
            stdout, stderr = p.communicate(timeout=2)
            return subprocess.CompletedProcess(p.args, p.returncode, stdout, stderr)
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()
            for stream in (p.stdin, p.stdout, p.stderr):
                stream.close()

    def test_idle_and_partial_line_keep_polling_to_remote_expiry(self):
        runner = RUNNER.replace("else 'waiting'", "else 'expired' if words[0] == 'status' else 'waiting'")
        for partial in ('', 'app'):
            with self.subTest(partial=partial):
                result = self.run_held_pipe(runner, partial)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn('ACTION ["status", "local-test"]', result.stdout)
                self.assertIn('"status": "expired"', result.stdout)
                self.assertTrue(result.stdout.endswith('CLOSED\n'))

    def test_local_deadline_closes_controller_with_stdin_open(self):
        setup = ('import time as real_time\nfrom types import SimpleNamespace\n'
                 'cli.time = SimpleNamespace(monotonic=lambda: real_time.monotonic()*1000, sleep=real_time.sleep)\n')
        result = self.run_held_pipe(RUNNER.replace('raise SystemExit(', setup + 'raise SystemExit('), 'app')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('ACTION ["status", "local-test"]', result.stdout)
        self.assertTrue(result.stdout.endswith('CLOSED\n'))

    def test_eof_cancels_and_closes(self):
        result = self.run_cli('')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('ACTION ["cancel", "local-test"]', result.stdout)
        self.assertTrue(result.stdout.endswith('CLOSED\n'))

    def test_unterminated_final_command_is_processed(self):
        result = self.run_cli('apply')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_overlong_line_cannot_execute_its_suffix(self):
        result = self.run_cli('x'*128 + 'apply\ncancel\n')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn('ACTION ["apply"', result.stdout)
        self.assertIn('Only status', result.stdout)
        self.assertNotIn('x'*128, result.stdout)
        self.assertIn('ACTION ["cancel", "local-test"]', result.stdout)

    def test_invalid_commands_are_not_forwarded_or_echoed(self):
        result = self.run_cli('choose 100\nchoose -1\nhttps://invalid.test/PRIVATE_INPUT\napply extra\napply\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('PRIVATE_INPUT', result.stdout + result.stderr)
        self.assertNotIn('ACTION ["choose"', result.stdout)
        self.assertEqual(result.stdout.count('Only status'), 4)

    def test_real_pipe_commands_and_cleanup(self):
        result = self.run_cli('choose 2\nstatus\napply\n')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn('ACTION ["choose", "local-test", "2"]', result.stdout)
        self.assertIn('ACTION ["status", "local-test"]', result.stdout)
        self.assertTrue(result.stdout.endswith('CLOSED\n'), result.stdout)

class ConsoleReaderTests(unittest.TestCase):
    def console_poll(self, keys):
        import msvcrt
        from unittest.mock import patch
        from finix.connect_cli import _StdinReader
        read_fd, write_fd = os.pipe()
        with os.fdopen(read_fd, 'rb') as stream:
            try:
                reader = _StdinReader(stream)
                reader.kind = 2
                pending = list(keys)
                with patch.object(msvcrt, 'kbhit', side_effect=lambda: bool(pending)), \
                     patch.object(msvcrt, 'getwch', side_effect=lambda: pending.pop(0)), \
                     patch('os.read', side_effect=AssertionError('blocking console read')):
                    return reader.poll(.02)
            finally:
                os.close(write_fd)

    @unittest.skipUnless(os.name == 'nt', 'Windows console backend')
    def test_console_enter_backspace_and_extended_keys(self):
        self.assertEqual(self.console_poll('appx\b\x00Kly\r'), 'apply\n')

    @unittest.skipUnless(os.name == 'nt', 'Windows console backend')
    def test_console_control_z_is_eof(self):
        self.assertEqual(self.console_poll('\x1a'), '')

    @unittest.skipUnless(os.name == 'nt', 'Windows console backend')
    def test_console_control_c_interrupts(self):
        with self.assertRaises(KeyboardInterrupt):
            self.console_poll('\x03')

    @unittest.skipUnless(os.name == 'nt', 'Windows console backend')
    def test_idle_console_poll_never_calls_blocking_read(self):
        import msvcrt
        from unittest.mock import patch
        from finix.connect_cli import _StdinReader
        read_fd, write_fd = os.pipe()
        with os.fdopen(read_fd, 'rb') as stream:
            try:
                reader = _StdinReader(stream)
                reader.kind = 2  # FILE_TYPE_CHAR (console)
                with patch.object(msvcrt, 'kbhit', return_value=False), \
                     patch('os.read', side_effect=AssertionError('blocking console read')):
                    self.assertIsNone(reader.poll(.02))
            finally:
                os.close(write_fd)

if __name__ == '__main__':
    unittest.main()
