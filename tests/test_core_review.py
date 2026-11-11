"""Review regressions: synthetic profiles, real age, no production data."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from finix import core


class CoreReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'profile'
        self.source.mkdir()
        self.put('config.yaml', b'model: synthetic\n')
        self.key = self.root / 'identity'
        self.public = core.keygen(self.key)
        self.output, self.state = self.root / 'output', self.root / 'state'

    def put(self, name, data=b'synthetic-fixture'):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def backup(self, **kwargs):
        return core.backup(self.source, self.output, self.state, self.public, **kwargs)

    def restore(self, result, name='restored'):
        target = self.root / name
        core.restore(result['path'], self.key, target)
        return target

    def test_canonical_profile_and_nested_wal_sqlite_roundtrip(self):
        names = [
            'plugins/custom/__init__.py', 'plugins/custom/token.json',
            'plugin-data/custom/settings.json', 'desktop-plugins/custom/plugin.js',
            'vault/vault.key', 'vault/vault.json.enc', 'secrets/custom.json',
            'pairing/telegram.json', 'platforms/pairing/discord.json',
            'sessions/custom/session.json', 'auth.json', '.env',
            'channel_aliases.json',
            'processes.json', 'feishu_comment_pairing.json',
        ]
        for name in names:
            self.put(name)
        # Current durable policy deliberately excludes regenerated runtime caches.
        for name in ('gateway_state.json','channel_directory.json'):
            self.put(name)
        databases = ['memory_store.db', 'response_store.db', 'projects.db',
                     'verification_evidence.db', 'kanban.db',
                     'kanban/boards/custom.db', 'gateway/discord_message_recovery.db',
                     'cron/executions.db', 'plugin-data/custom/cache.sqlite',
                     'plugins/custom/data/no-extension']
        for name in databases:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(path)
            self.addCleanup(db.close)
            db.execute('pragma journal_mode=WAL')
            db.execute('create table fixture(value text)')
            db.execute('insert into fixture values (?)', ('synthetic-wal-row',))
            db.commit()
            self.assertGreater(Path(str(path) + '-wal').stat().st_size, 0)
        # Suffix alone is not a database: preserve its bytes and unrelated -wal files.
        self.put('plugin-data/custom/not-sqlite.db')
        self.put('plugin-data/custom/not-sqlite.db-wal')
        self.put('plugins/custom/node_modules/omitted.js')
        self.put('plugins/custom/.git/objects/omitted')
        self.put('logs/omitted.log')
        self.put('unknown/omitted')
        result = self.backup()
        entries = core.validate_archive(core.decrypt(result['path'], self.key))
        target = self.restore(result)
        for name in ('gateway_state.json','channel_directory.json'):
            self.assertFalse((target/name).exists())
        self.assertEqual([n for n in names if not (target / n).is_file()], [])
        for name in names:
            self.assertEqual((target / name).read_bytes(), (self.source / name).read_bytes())
        for name in databases:
            self.assertTrue((target / name).is_file(), name)
            with closing(sqlite3.connect(target / name)) as db:
                self.assertEqual(db.execute('select value from fixture').fetchall(),
                                 [('synthetic-wal-row',)])
            for suffix in ('-wal', '-shm', '-journal'):
                # Check the archive rather than SQLite's newly generated local companions.
                self.assertNotIn(name + suffix, entries)
        self.assertEqual((target / 'plugin-data/custom/not-sqlite.db-wal').read_bytes(),
                         b'synthetic-fixture')
        for name in ['plugins/custom/node_modules', 'logs']:
            self.assertFalse((target / name).exists())
        for name in ['plugins/custom/.git/objects/omitted', 'unknown/omitted']:
            self.assertEqual((target / name).read_bytes(), b'synthetic-fixture')
        self.assertEqual((target / 'plugins/custom/__init__.py').stat().st_mode & 0o777, 0o600)

    def test_supported_cron_shapes_survive_retention(self):
        job = {'id': 'fixture', 'enabled': True, 'state': 'scheduled',
               'schedule': {'kind': 'interval', 'minutes': 1},
               'next_run_at': '2000-01-01T00:00:00+00:00'}
        shapes = [ {'jobs': [job]}, [job], {'jobs': {'fixture': {k: v for k, v in job.items() if k != 'id'}}} ]
        for index, payload in enumerate(shapes):
            with self.subTest(shape=index):
                self.put('cron/jobs.json', json.dumps({'jobs': []}).encode())
                old = self.backup(keep=1)
                self.restore(old, f'old-{index}')
                original = json.dumps(payload).encode()
                self.put('cron/jobs.json', original)
                result = self.backup(keep=1)
                target = self.restore(result, f'cron-{index}')
                jobs = json.loads((target / 'cron/jobs.json').read_bytes())['jobs']
                self.assertEqual(len(jobs), 1)
                self.assertEqual(jobs[0]['id'], 'fixture')
                self.assertIs(jobs[0]['enabled'], False)
                self.assertEqual(jobs[0]['state'], 'paused')
                self.assertIsNone(jobs[0]['next_run_at'])
                self.assertFalse(Path(old['path']).exists())
                self.assertEqual((self.source / 'cron/jobs.json').read_bytes(), original)
                self.native_cron_disabled(target)

    def native_cron_disabled(self, target):
        checkout = os.environ.get('FINIX_HERMES_SOURCE')
        if not checkout:
            return  # Optional native-runtime verification, enabled explicitly in CI/operator runs.
        env = dict(os.environ, HERMES_HOME=str(target), HERMES_RUNTIME_DIR=str(target / 'runtime'),
                   PYTHONDONTWRITEBYTECODE='1')
        proc = subprocess.run([sys.executable, '-c',
            'from cron.jobs import load_jobs,get_due_jobs; '
            'j=load_jobs(); assert j and all(x["enabled"] is False for x in j); '
            'assert all(x["state"] == "paused" for x in j); assert get_due_jobs() == []'],
            cwd=checkout, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, 'Native cron suppression check failed')

    def test_bad_cron_cannot_publish_or_prune(self):
        old = self.backup(keep=1)
        old_index = (self.state / 'index.json').read_bytes()
        malformed = [b'not json', b'null', b'{}', b'{"jobs":null}',
                     b'{"jobs":[null]}', b'{"jobs":{"fixture":null}}']
        for payload in malformed:
            with self.subTest(payload_type=malformed.index(payload)):
                self.put('cron/jobs.json', payload)
                with self.assertRaises(core.FinixError):
                    self.backup(keep=1)
                self.assertTrue(Path(old['path']).exists())
                self.assertEqual((self.state / 'index.json').read_bytes(), old_index)
                self.assertEqual(len(list(self.output.iterdir())), 1)
        self.restore(old)

    def test_tar_padding_and_old_buffer_caps_do_not_limit_streaming(self):
        for limit in (10239, 10240, 32768):
            with self.subTest(old_buffer_limit=limit), mock.patch.object(core, 'MAX_BYTES', limit):
                self.put('arbitrary-file', b'0' * (limit + 1))
                result = self.backup(keep=1)
                target = self.restore(result, f'unbounded-{limit}')
                self.assertEqual((target / 'arbitrary-file').stat().st_size, limit + 1)

    def test_nested_sqlite_companion_symlink_rejected(self):
        path = self.put('plugin-data/custom/store.sqlite', b'')
        with closing(sqlite3.connect(path)) as db:
            db.execute('create table fixture(value text)')
            db.commit()
        Path(str(path) + '-wal').symlink_to(self.root / 'outside')
        with self.assertRaises(core.FinixError):
            self.backup()
        self.assertEqual(list(self.output.glob('*.age')), [])


if __name__ == '__main__':
    unittest.main()
