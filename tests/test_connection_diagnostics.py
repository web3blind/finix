"""Closed diagnostics; all provider work is replaced before invocation."""
import io
import os
from contextlib import nullcontext
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from finix import connection_worker as worker
from finix.connections import Connections


class DiagnosticsTests(unittest.TestCase):
    def test_worker_errors_are_closed_codes_not_provider_text(self):
        cases = [(ValueError('provider'), 'RCLONE_PROVIDER_FAILED'),
                 (ValueError('unsupported'), 'CONNECTION_UNSUPPORTED'),
                 (TimeoutError('SECRET'), 'CONNECTION_TIMEOUT'),
                 (ValueError(['SECRET']), 'CONNECTION_FAILED'),
                 (ValueError('SECRET token=private'), 'CONNECTION_FAILED')]
        for error, code in cases:
            with self.subTest(code=code), patch.object(worker.sys, 'argv', ['worker', 'yandex', 'unused', 'unused', 'unused']), patch.object(worker, 'connect_rclone', side_effect=error), patch.object(worker, 'emit') as emit, patch('threading.Timer'), patch('signal.alarm', create=True), patch('signal.signal'):
                self.assertEqual(worker.main(), 1)
                emit.assert_called_once_with(status='failed', code=code)

    def test_reader_preserves_only_allowlisted_failure_codes(self):
        for code in ('RCLONE_PROVIDER_FAILED', 'CONNECTION_TIMEOUT', 'CONNECTION_UNSUPPORTED', 'SECRET token=private'):
            process = Mock()
            process.stdout = io.BytesIO(json.dumps({'status': 'failed', 'code': code}).encode() + b'\n')
            process.stdin = io.BytesIO()
            process.wait.return_value = 1
            with tempfile.TemporaryDirectory() as temp:
                session = {'process': process, 'stage': Path(temp) / 'stage', 'public': {'status': 'working', 'provider': 'yandex'}}
                with patch('finix.windows_native.cleanup') if os.name == 'nt' else nullcontext():
                    Connections()._read(session)
                self.assertEqual(session['public'].get('code'), code if not code.startswith('SECRET') else 'CONNECTION_FAILED')
                self.assertNotIn('SECRET', json.dumps(session['public']))
