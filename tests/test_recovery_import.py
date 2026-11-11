"""Disposable-only import acceptance. No gateway, keys, providers or real profiles."""
from contextlib import closing
import json
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

import yaml
from finix.recovery import RecoveryService


class FakeHost:
    PM_RUNTIME_ROOT_DIRS = frozenset({'installs', 'tools', 'cache'})
    def __init__(self, root):
        self.root, self.calls = root, 0
    def validate_profile_name(self, name):
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,39}', name) or name in {'tmp', 'root', 'hermes'}:
            raise ValueError('invalid')
    def get_profile_dir(self, name):
        return self.root / 'profiles' / name
    def import_profile(self, archive_path, name=None):
        self.calls += 1
        target = self.get_profile_dir(name)
        if target.exists():
            raise ValueError('exists')
        with tarfile.open(archive_path) as archive:
            names = archive.getnames()
            assert f'{name}/gateway.parked' in names  # BEFORE visibility to multiplexer
            archive.extractall(self.root / 'profiles', filter='data')
        return target


class ImportTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get('FINIX_TEST_SCRATCH') or (
            str(Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'hermes/cache/scratch')
            if os.name == 'nt' else str(Path.home() / '.cache/hermes/cache/scratch')))
        scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.tmp.cleanup)
        from finix import core
        self.root = core.private_dir(Path(self.tmp.name) / 'private')
        self.owner = core.private_dir(self.root / 'owner')
        core.private_dir(self.owner / 'cache')
        core.private_dir(self.owner / 'cache/finix')
        (self.owner / 'config.yaml').write_text('model: {default: fixture-model}\n')
        self.bundle = self.root / 'recovered'
        self.payload = self.bundle / 'profile'
        self.payload.mkdir(parents=True)
        self.config = {'model': {'default': 'fixture-model', 'provider': 'openai'},
                       'plugins': {'enabled': ['danger']},
                       'hooks': {'outbound': [{'url': 'https://invalid.test'}]},
                       'skills': {'auto_load': ['danger']},
                       'platforms': {'telegram': {'enabled': True, 'token': 'fixture-token'}},
                       'mcp_servers': {'fixture': {'command': 'never-run', 'enabled': True}}}
        (self.payload / 'config.yaml').write_text(yaml.safe_dump(self.config))
        (self.payload / '.env').write_text('OPENAI_API_KEY=fixture-not-secret\nTELEGRAM_BOT_TOKEN=fixture-token\n')
        (self.payload / 'auth.json').write_text('{"fixture_provider":{"token":"fixture-not-secret"}}')
        for name in ['memories/MEMORY.md', 'skills/demo/SKILL.md', 'plugins/danger/__init__.py']:
            path = self.payload / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('raise RuntimeError("NEVER EXECUTE")\n')
        (self.payload / 'desktop.json').write_text('{"theme":"dark"}')
        (self.payload / 'skills/.usage.json').write_text('{"pinned":["demo"]}')
        (self.payload / 'plugins/danger/plugin.yaml').write_text('name: danger\nversion: "1.0.0"\n')
        with closing(sqlite3.connect(self.payload / 'state.db')) as db:
            db.execute('CREATE TABLE fixture (value TEXT)')
            db.execute("INSERT INTO fixture VALUES ('history')")
            db.commit()
        self.service = RecoveryService(profile_id='default', profile_root=self.owner,
                                       activation='epoch', protected_roots=[])
        self.addCleanup(self.service.close)
        self.service._jobs['job'] = ({'status': 'succeeded', 'result': {
            'complete': True, 'omitted_count': 0, 'layout': 'bundle'}}, threading.Event())
        self.service._handoffs['job'] = {'bundle_root': str(self.bundle),
             'profile_root': str(self.payload), 'complete': True, 'cron_disabled': True,
             'activated': False, 'extras_policy': 'separate_manual_import_only'}
        self.host = FakeHost(self.root / 'synthetic-host')

    def importer(self):
        from finix.recovery_import import RecoveryImporter
        return RecoveryImporter(self.service, host=self.host)

    def run_import(self, **kwargs):
        args = dict(profile_id='default', activation='epoch', name='recovered', consent_import=True)
        args.update(kwargs)
        return self.importer().import_job('job', **args)

    def test_portable_import_preserves_core_and_parks_before_publication(self):
        before = {p.relative_to(self.payload).as_posix(): p.read_bytes()
                  for p in self.payload.rglob('*') if p.is_file()}
        extras = self.bundle / 'extras' / 'fixture.txt'
        extras.parent.mkdir()
        extras.write_text('stays staged')
        result = self.run_import()
        self.assertEqual(result, dict(name='recovered', can_activate=True, complete=True,
                                      omitted_count=0, parked=True))
        dest = self.host.get_profile_dir('recovered')
        config = yaml.safe_load((dest / 'config.yaml').read_text())
        self.assertEqual(config['model'], self.config['model'])
        self.assertEqual(config['plugins']['enabled'], [])
        self.assertEqual(config['hooks'], {})
        self.assertEqual(config['skills']['auto_load'], [])
        self.assertFalse(config['platforms']['telegram']['enabled'])
        # Bundled deferred platforms can auto-enable on env credentials even
        # without a recovered YAML platform block; plugins.enabled alone is not a guard.
        self.assertFalse(config['platforms']['google_chat']['enabled'])
        self.assertFalse(config['platforms']['irc']['enabled'])
        self.assertFalse(config['mcp_servers']['fixture']['enabled'])
        self.assertEqual((dest / '.env').read_bytes(), before['.env'])
        for name in ['memories/MEMORY.md', 'skills/demo/SKILL.md', 'skills/.usage.json',
                     'desktop.json', 'auth.json', 'state.db']:
            self.assertEqual((dest / name).read_bytes(), before[name])
        with closing(sqlite3.connect(dest / 'state.db')) as db:
            self.assertEqual(db.execute('SELECT value FROM fixture').fetchone(), ('history',))
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone(), ('ok',))
        self.assertFalse((dest / 'extras').exists())
        self.assertEqual(before, {p.relative_to(self.payload).as_posix(): p.read_bytes()
                                 for p in self.payload.rglob('*') if p.is_file()})
        self.assertEqual((self.owner / 'config.yaml').read_text(), 'model: {default: fixture-model}\n')
        self.assertFalse(any((self.owner / 'cache/finix/import').rglob('*.tar.gz')))

    def assert_code(self, code, **kwargs):
        from finix.recovery_import import RecoveryImportError
        with self.assertRaises(RecoveryImportError) as caught:
            self.run_import(**kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.host.calls, 0)

    def test_incomplete_requires_extra_ack_and_keeps_count(self):
        self.service._handoffs['job']['complete'] = False
        self.service._jobs['job'][0]['result'].update(complete=False, omitted_count=2)
        self.assert_code('INCOMPLETE_ACK_REQUIRED')
        result = self.run_import(acknowledge_incomplete=True)
        self.assertFalse(result['complete'])
        self.assertEqual(result['omitted_count'], 2)

    def test_wrong_profile_epoch_and_job_rejected(self):
        self.assert_code('STALE_PROFILE', profile_id='other')
        self.assert_code('STALE_PROFILE', activation='stale')
        self.service._jobs.clear()
        self.assert_code('NOT_FOUND')

    def test_existing_profile_never_overwritten(self):
        target = self.host.get_profile_dir('recovered')
        target.mkdir(parents=True)
        (target / 'keep').write_text('unchanged')
        self.assert_code('PROFILE_EXISTS')
        self.assertEqual((target / 'keep').read_text(), 'unchanged')

    def test_literal_name_validation_and_consent(self):
        for name in ['default', '../escape', 'abc/def', 'abc\\def', 'ABC', 'a;echo-x', '', 'root']:
            with self.subTest(name=name):
                self.assert_code('INVALID_NAME', name=name)
        self.assert_code('IMPORT_CONSENT_REQUIRED', consent_import=1)

    def test_corrupt_config_and_schema_rejected_without_host(self):
        for config in ['[broken', '[]', 'model: x\nmodel: y', 'model: {default: x}\nplugins: 2',
                       'model: &x {default: x}\ncopy: *x']:
            with self.subTest(config=config):
                (self.payload / 'config.yaml').write_text(config)
                self.assert_code('INVALID_CONFIG')

    def test_foreign_profile_root_and_protected_root_rejected(self):
        self.service._handoffs['job']['profile_root'] = str(self.owner)
        self.assert_code('INVALID_CANDIDATE')
        self.service._handoffs['job']['bundle_root'] = str(self.owner)
        self.assert_code('INVALID_CANDIDATE')

    def test_runtime_stripped_integrations_paused_credentials_preserved(self):
        for name in ['gateway.lock', 'gateway.pid', 'tools/foreign.exe', 'installs/selection',
                     'hooks/evil/handler.py', 'desktop-plugins/evil/plugin.js', 'gateway.json',
                     'cron/jobs.json']:
            path = self.payload / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"jobs":[{"enabled":true}]}')
        self.run_import()
        dest = self.host.get_profile_dir('recovered')
        for name in ['gateway.lock', 'gateway.pid', 'tools', 'installs', 'hooks', 'desktop-plugins', 'plugins', 'gateway.json']:
            self.assertFalse((dest / name).exists(), name)
        for name in ['hooks/evil/handler.py', 'desktop-plugins/evil/plugin.js', 'plugins/danger/__init__.py', 'gateway.json']:
            self.assertTrue((dest / '.finix-paused' / name).is_file(), name)
        self.assertFalse(json.loads((dest / 'cron/jobs.json').read_text())['jobs'][0]['enabled'])
        records = list((self.owner / 'cache/finix/import').glob('*.json'))
        self.assertEqual(len(records), 1)
        record = json.loads(records[0].read_text())
        self.assertEqual(record['previous_profile'], 'default')
        self.assertEqual(record['status'], 'imported')
        self.assertFalse(record['activated'])

    def test_publication_refuses_raced_existing_target_without_adoption(self):
        from finix import recovery_import
        source = self.root / 'host-extracted'
        source.mkdir()
        (source / 'gateway.parked').write_text('parked')
        target = self.root / 'raced-target'
        target.mkdir()
        (target / 'keep').write_text('unchanged')
        self.assertTrue(hasattr(recovery_import, '_publish_profile'))
        with self.assertRaises(Exception):
            recovery_import._publish_profile(source, target)
        self.assertEqual(sorted(p.name for p in target.iterdir()), ['keep'])
        self.assertTrue(source.exists())

    def test_startup_environment_controls_parked_not_credentials(self):
        original = ('OPENAI_API_KEY=fixture-not-secret\nHERMES_HOME=/foreign\n'
                    'HERMES_ACCEPT_HOOKS=1\nPYTHONPATH=/foreign-code\n')
        (self.payload / '.env').write_text(original)
        self.run_import()
        dest = self.host.get_profile_dir('recovered')
        self.assertEqual((dest / '.env').read_text(), 'OPENAI_API_KEY=fixture-not-secret\n')
        self.assertEqual((dest / '.finix-paused/.env').read_text(), original)

    def test_case_variant_executable_roots_are_inert_portably(self):
        for relative in ['Desktop-Plugins/evil/plugin.js', 'Hooks/evil/handler.py', 'BIN/foreign.exe']:
            path = self.payload / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('never execute')
        self.run_import()
        target = self.host.get_profile_dir('recovered')
        self.assertFalse((target / 'Desktop-Plugins').exists())
        self.assertFalse((target / 'Hooks').exists())
        self.assertFalse((target / 'BIN').exists())

    def test_host_failure_preserves_previous_profile_and_records_rollback(self):
        def fail(*args, **kwargs):
            raise RuntimeError('/private/secret-path must never escape')
        self.host.import_profile = fail
        from finix.recovery_import import RecoveryImportError
        with self.assertRaises(RecoveryImportError) as caught:
            self.run_import()
        self.assertEqual(str(caught.exception), 'IMPORT_FAILED')
        self.assertEqual((self.owner / 'config.yaml').read_text(), 'model: {default: fixture-model}\n')
        record = json.loads(next((self.owner / 'cache/finix/import').glob('*.json')).read_text())
        self.assertEqual(record['status'], 'import_failed')
        self.assertFalse(record['target_preserved'])
        self.assertFalse(any((self.owner / 'cache/finix/import').rglob('*.tar.gz')))


@unittest.skipUnless(os.environ.get('FINIX_TEST_REAL_HOST') == '1',
                     'Explicit disposable installed-host/source integration gate')
class InstalledHostIntegration(unittest.TestCase):
    def fixture(self):
        fixture = ImportTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def isolated_environment(self, fixture):
        return patch.dict(os.environ, {'HERMES_HOME': str(fixture.owner),
                           'LOCALAPPDATA': str(fixture.root / 'localappdata'),
                           'HOME': str(fixture.root / 'account'),
                           'USERPROFILE': str(fixture.root / 'account')})

    def test_actual_importer_only_in_disposable_hermes_home(self):
        fixture = ImportTests('test_portable_import_preserves_core_and_parks_before_publication')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        from finix.recovery_import import RecoveryImporter
        # Patch only this disposable test process, never a live Hermes process.
        with patch.dict(os.environ, {'HERMES_HOME': str(fixture.owner),
                                     'LOCALAPPDATA': str(fixture.root / 'localappdata'),
                                     'HOME': str(fixture.root / 'account'),
                                     'USERPROFILE': str(fixture.root / 'account')}):
            from hermes_cli import profiles as host
            self.assertEqual(Path(host.get_profile_dir('recovered')),
                             fixture.owner / 'profiles/recovered')
            self.assertTrue(Path(host.__file__).is_file())
            fixture.host = host
            fixture.importer = lambda: RecoveryImporter(fixture.service)
            fixture.test_portable_import_preserves_core_and_parks_before_publication()
            # Host rejects existing/default names; importer must never invoke overwrite.
            from finix.recovery_import import RecoveryImportError
            with self.assertRaises(RecoveryImportError) as caught:
                fixture.run_import()
            self.assertEqual(caught.exception.code, 'PROFILE_EXISTS')

    def test_real_authenticated_recovery_then_host_import(self):
        from finix import core
        from finix.recovery_import import RecoveryImporter
        fixture = self.fixture()
        for relative in ['state.db', 'memories/MEMORY.md', 'skills/demo/SKILL.md']:
            dest = fixture.owner / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(fixture.payload / relative, dest)
        key = fixture.root / 'fixture-identity'
        recipient = core.keygen(key)
        output, state = fixture.root / 'backups', fixture.root / 'backup-state'
        core.backup(fixture.owner, output, state, recipient,
                    sqlite_temp_dir=core.private_dir(fixture.owner / 'cache/finix/sqlite-temp'))
        fixture.service._jobs.clear()
        fixture.service._handoffs.clear()
        fixture.service.add_local('local', output, state)
        candidate = fixture.service.catalog('default', 'epoch')[0]
        job = fixture.service.start(candidate['id'], identity=key,
                    destination=fixture.root / 'authenticated-candidate',
                    profile_id='default', activation='epoch')
        status = fixture.service.wait(job, timeout=30)
        self.assertEqual(status['status'], 'succeeded', status)
        with self.isolated_environment(fixture):
            from hermes_cli import profiles as host
            self.assertEqual(host.get_profile_dir('recovered'), fixture.owner / 'profiles/recovered')
            result = RecoveryImporter(fixture.service).import_job(job, profile_id='default',
                            activation='epoch', name='recovered', consent_import=True)
            self.assertTrue(result['complete'])
            target = host.get_profile_dir('recovered')
            self.assertTrue((target / 'gateway.parked').is_file())
            with closing(sqlite3.connect(target / 'state.db')) as db:
                self.assertEqual(db.execute('SELECT value FROM fixture').fetchone(), ('history',))
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone(), ('ok',))

    def test_host_schema_errors_refuse_publication(self):
        from finix.recovery_import import RecoveryImporter, RecoveryImportError
        fixture = self.fixture()
        fixture.config['voice'] = {'submit_mode': 'not-a-supported-mode'}
        (fixture.payload / 'config.yaml').write_text(yaml.safe_dump(fixture.config))
        with self.isolated_environment(fixture):
            from hermes_cli import profiles as host
            self.assertEqual(host.get_profile_dir('recovered'), fixture.owner / 'profiles/recovered')
            with self.assertRaises(RecoveryImportError) as caught:
                RecoveryImporter(fixture.service).import_job('job', profile_id='default',
                    activation='epoch', name='recovered', consent_import=True)
            self.assertEqual(caught.exception.code, 'INVALID_CONFIG')
            self.assertFalse(host.get_profile_dir('recovered').exists())


if __name__ == '__main__':
    unittest.main()
