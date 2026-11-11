"""Public native CLI shares the same durable job service as HTTP."""
import argparse
import contextlib
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from finix.plugin_cli import setup_parser, run


class NativeJobCLI(unittest.TestCase):
    def parse(self, words):
        parser = argparse.ArgumentParser()
        setup_parser(parser)
        return parser.parse_args(words)

    def test_jobs_start_waits_for_shared_worker_and_emits_public_json(self):
        args = self.parse(['jobs', 'start', '--request-id', '11111111-1111-4111-8111-111111111111'])
        profile = SimpleNamespace(profile_id='a'*64)
        ack = {'profile_id': profile.profile_id, 'code_version': 'backup-jobs-v1', 'job': {'id':'b'*32, 'state':'running'}}
        terminal = {**ack, 'job': {'id':'b'*32, 'state':'succeeded'}}
        service = SimpleNamespace(start=lambda p, pid, rid: ack, current=lambda p: terminal)
        output = io.StringIO()
        with patch('finix.backup_jobs.BackupJobs', return_value=service), contextlib.redirect_stdout(output):
            self.assertEqual(run(args, profile_factory=lambda: profile), 0)
        self.assertEqual([json.loads(line) for line in output.getvalue().splitlines()], [ack, terminal])

    def test_jobs_status_reads_shared_state_without_start(self):
        args = self.parse(['jobs', 'status'])
        service = SimpleNamespace(current=lambda p: {'job':None})
        with patch('finix.backup_jobs.BackupJobs', return_value=service), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(run(args, profile_factory=lambda: object()), 0)
        self.assertEqual(json.loads(output.getvalue()), {'job': None})

    def test_configure_existing_storage_uses_profile_contract(self):
        args = self.parse(['configure', 'storage', 'workspace'])
        calls = []
        profile = SimpleNamespace(configure=lambda key,value: calls.append((key,value)) or {'storage': value})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run(args, profile_factory=lambda: profile), 0)
        self.assertEqual(calls, [('storage','workspace')])


if __name__ == '__main__': unittest.main()
