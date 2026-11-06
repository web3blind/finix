"""Disposable default roots only; real age and live SQLite WAL."""
from contextlib import closing
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from finix import core, streaming
from finix.integration import Profile


class ExtraSourcesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.home = self.base / 'custom-home'
        self.home.mkdir()
        (self.home / 'config.yaml').write_text('unrelated: preserve\n')
        self.env = patch.dict(os.environ, {'HERMES_HOME': str(self.home)})
        self.env.start(); self.addCleanup(self.env.stop)
        self.native = patch('hermes_constants._get_platform_default_hermes_home', return_value=self.home)
        self.native.start(); self.addCleanup(self.native.stop)
        self.profile = Profile(self.home)
        self.one = self.base / 'project one'; self.one.mkdir()
        self.two = self.base / 'project two'; self.two.mkdir()
        self.key = self.base / 'key'
        self.recipient = core.keygen(self.key)
        self.profile.recipient(self.profile.profile_id, self.recipient, None)

    def test_real_age_bundle_two_roots_sqlite_and_future_files(self):
        self.profile.extra_sources('add', 'one', str(self.one))
        self.profile.extra_sources('add', 'two', str(self.two))
        for root in (self.one, self.two):
            (root / 'same').write_text(root.name)
            (root / '.git').mkdir(); (root / '.git/HEAD').write_text('ref: main')
            db = sqlite3.connect(root / 'data'); self.addCleanup(db.close)
            db.execute('pragma journal_mode=WAL')
            db.execute('create table sample(value)')
            db.execute('insert into sample values (?)', (root.name,)); db.commit()
        (self.home / 'cron').mkdir()
        (self.home / 'cron/jobs.json').write_text(json.dumps({'jobs': [{'id': 'fixture', 'enabled': True}]}))
        result = self.profile.backup()
        target = self.base / 'restored'
        restored = core.restore(result['path'], self.key, target)
        self.assertEqual(restored['layout'], 'bundle')
        self.assertTrue((target / 'profile/config.yaml').exists())
        restored_jobs = json.loads((target / 'profile/cron/jobs.json').read_text())
        self.assertFalse(restored_jobs['jobs'][0]['enabled'])
        plaintext = core.run_age(['-d', '-i', str(self.key)], Path(result['path']).read_bytes())
        buffered = core.validate_archive(plaintext)
        self.assertNotIn('cron/jobs.json', buffered)
        self.assertFalse(json.loads(buffered['profile/cron/jobs.json'])['jobs'][0]['enabled'])
        for alias, original in [('one', self.one), ('two', self.two)]:
            self.assertEqual((target / 'external' / alias / 'same').read_text(), original.name)
            with closing(sqlite3.connect(target / 'external' / alias / 'data')) as db:
                self.assertEqual(db.execute('select value from sample').fetchone(), (original.name,))
        import yaml
        cfg = yaml.safe_load((self.home / 'config.yaml').read_text())
        self.assertEqual(cfg['unrelated'], 'preserve')
        self.assertEqual(len(cfg['plugins']['entries']['finix']['settings']['extra_sources']), 2)

    def test_named_manual_config_and_schedule_denied(self):
        self.profile.extra_sources('add', 'one', str(self.one))
        named = self.home / 'profiles/named'; named.mkdir(parents=True)
        (named / 'config.yaml').write_bytes((self.home / 'config.yaml').read_bytes())
        profile = Profile(named)
        profile.recipient(profile.profile_id, self.recipient, None)
        cfg = profile.config(); cfg.update(interval_hours=1, next_run=0)
        core.atomic_json(profile.root / 'settings.json', cfg)
        for scheduled in (False, True):
            with self.assertRaises(core.FinixError): profile.backup(scheduled=scheduled)
        with self.assertRaises(core.FinixError): profile.extra_sources('add', 'two', str(self.two))

    def test_invalid_alias_overlap_missing_and_symlink(self):
        for alias in ('../bad', '', 'a/b', 'profile', 'UPPER', 'a' * 65):
            with self.subTest(alias=alias), self.assertRaises(core.FinixError):
                self.profile.extra_sources('add', alias, str(self.one))
        for path in ('/', '/proc', str(self.home), str(self.base), str(self.profile.root)):
            with self.subTest(path=path), self.assertRaises(core.FinixError):
                self.profile.extra_sources('add', 'bad', path)
        link = self.base / 'link'; link.symlink_to(self.one)
        with self.assertRaises(core.FinixError): self.profile.extra_sources('add', 'bad', str(link))
        self.profile.extra_sources('add', 'one', str(self.one))
        with self.assertRaises(core.FinixError): self.profile.extra_sources('add', 'duplicate', str(self.one))
        child = self.one / 'child'; child.mkdir()
        with self.assertRaises(core.FinixError): self.profile.extra_sources('add', 'child', str(child))

    def test_missing_after_save_fails_without_pruning(self):
        self.profile.extra_sources('add', 'one', str(self.one))
        old = self.profile.backup()
        before = (self.profile.root / 'index/index.json').read_bytes()
        self.one.rmdir()
        with self.assertRaises(core.FinixError): self.profile.backup()
        self.assertTrue(Path(old['path']).exists())
        self.assertEqual(before, (self.profile.root / 'index/index.json').read_bytes())

    def test_foreign_profile_introduced_under_saved_source_is_denied(self):
        import shutil
        self.profile.extra_sources('add', 'one', str(self.one))
        old = self.profile.backup()
        before = (self.profile.root / 'index/index.json').read_bytes()
        nested = self.one / 'nested'
        for style in ('markers', 'classic', 'tombstone'):
            with self.subTest(style=style):
                root = nested / ('.hermes' if style == 'classic' else 'custom-hermes')
                named = root / 'profiles/named'
                named.mkdir(parents=True)
                if style == 'markers':
                    (root / 'config.yaml').write_text('fixture: true')
                elif style == 'tombstone':
                    (root / 'profiles/.deleted').mkdir()
                (named / 'state.db').write_bytes(b'foreign profile fixture')
                with self.assertRaises(core.FinixError):
                    self.profile.backup()
                self.assertEqual(before, (self.profile.root / 'index/index.json').read_bytes())
                self.assertTrue(Path(old['path']).exists())
                shutil.rmtree(nested)

    def test_arbitrary_home_not_default(self):
        arbitrary = self.base / 'arbitrary'; arbitrary.mkdir()
        with self.assertRaises(core.FinixError):
            Profile(arbitrary).extra_sources('add', 'one', str(self.one))

    def test_legacy_restore_reports_profile_layout(self):
        import hashlib
        import tarfile
        for version in ('finix-1', 'finix-2'):
            content = b'fixture: legacy'
            manifest = {'format': version, 'files': {'config.yaml': {
                'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()}}}
            raw = io.BytesIO()
            member = 'profile/config.yaml' if version == 'finix-2' else 'config.yaml'
            with tarfile.open(fileobj=raw, mode='w:gz') as archive:
                for name, data in ((member, content), ('manifest.json', json.dumps(manifest).encode())):
                    info = tarfile.TarInfo(name); info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
            cipher = self.base / (version + '.age')
            cipher.write_bytes(core.run_age(['-r', self.recipient], raw.getvalue()))
            target = self.base / ('restored-' + version)
            with self.subTest(version=version):
                self.assertEqual(core.restore(cipher, self.key, target)['layout'], 'profile')
                self.assertEqual((target / 'config.yaml').read_bytes(), content)

    def test_v3_malicious_namespace_and_manifest_rejected(self):
        import hashlib
        import tarfile
        entries = {'external/one/safe': b'fixture'}
        for bad in ('external/two/safe', 'external/../escape', 'profile/../escape', '/absolute', 'external/one/a/../b'):
            records = dict(entries); records[bad] = b'bad'
            manifest = {'format': 'finix-3', 'extra_sources': [{'name': 'one', 'path': '/informational/only'}],
                        'files': {name: {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()} for name, data in records.items()}}
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
                for name, data in [*records.items(), ('manifest.json', json.dumps(manifest).encode())]:
                    info = tarfile.TarInfo(name); info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
            cipher = self.base / 'malicious.age'
            cipher.write_bytes(core.run_age(['-r', self.recipient], buffer.getvalue()))
            target = self.base / 'rejected'
            with self.subTest(path=bad), self.assertRaises(core.FinixError):
                core.restore(cipher, self.key, target)
            self.assertFalse(target.exists())

    def test_cross_root_mutation_fails_before_publication(self):
        self.profile.extra_sources('add', 'one', str(self.one))
        (self.one / 'valuable').write_bytes(b'initial')
        old = self.profile.backup()
        read = streaming._add_payload
        def mutate(*args, **kwargs):
            result = read(*args, **kwargs)
            if args[1].startswith('external/'):
                (self.home / 'new-after-profile-snapshot').write_bytes(b'changed')
            return result
        with patch.object(streaming, '_add_payload', side_effect=mutate), self.assertRaises(core.FinixError):
            self.profile.backup()
        self.assertTrue(Path(old['path']).exists())
        self.assertEqual(len(list((self.profile.root / 'copies').glob('*.age'))), 1)

    def test_real_sdk_captures_default_before_context_handoff_and_denies_foreign_owner(self):
        import asyncio
        from types import SimpleNamespace
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
        from finix.integration import register
        manager = PluginManager(scope_key=str(self.home))
        ctx = PluginContext(PluginManifest(name='finix', path=str(Path(__file__).parents[1])), manager)
        token = set_hermes_home_override(self.home)
        try:
            with patch.object(ctx, 'get_config', side_effect=lambda key, default='': {'owner_id':'owner', 'owner_platform':'telegram'}.get(key, default)):
                register(ctx)
                handler = manager._plugin_commands['finix']['handler']
                other = self.home / 'profiles/named'; other.mkdir(parents=True)
                next_token = set_hermes_home_override(other)
                try:
                    async def calls():
                        foreign = await handler('extra add one "' + str(self.one) + '"', platform='telegram', source=SimpleNamespace(user_id='foreign'))
                        self.assertIn('denied', foreign)
                        self.assertEqual(self.profile.extra_sources('list')['extra_sources'], [])
                        answer = await handler('extra add one "' + str(self.one) + '"', platform='telegram', source=SimpleNamespace(user_id='owner'))
                        self.assertEqual(json.loads(answer)['extra_sources'][0]['name'], 'one')
                    asyncio.run(calls())
                finally:
                    reset_hermes_home_override(next_token)
        finally:
            reset_hermes_home_override(token)

    def test_remove_and_list(self):
        self.profile.extra_sources('add', 'one', str(self.one))
        self.assertEqual(self.profile.extra_sources('list')['extra_sources'], [{'name': 'one', 'path': str(self.one)}])
        self.profile.extra_sources('remove', 'one')
        self.assertEqual(self.profile.extra_sources('list')['extra_sources'], [])
