"""Synthetic native disk restore; no gateway, OAuth, cloud or archived execution."""
import os
from pathlib import Path
import time
import unittest
import uuid
from unittest.mock import patch

from finix import core, default_recovery as cold, default_recovery_lifecycle as live
from finix.integration import Profile
from finix import workspace_skill_transport as transport

RUNTIME = ('SKILL.md', 'scripts/google_api.py', 'scripts/_hermes_home.py')
SKILL = 'skills/productivity/google-workspace'


@unittest.skipUnless(os.environ.get('FINIX_CURRENT_CLOSURE_TEST_ROOT') and os.environ.get('FINIX_INSTALLED_WORKSPACE_SKILL'),
                     'requires an explicit isolated native root and CURRENT compatible bundle')
class CurrentWorkspaceClosureTests(unittest.TestCase):
    def setUp(self):
        base = Path(os.environ['FINIX_CURRENT_CLOSURE_TEST_ROOT'])
        self.scope = core.private_dir(base / uuid.uuid4().hex)
        self.root = core.private_dir(self.scope / 'sdk-root')
        self.environment = patch.dict(os.environ, HERMES_HOME=str(self.root), HERMES_PROFILE='default')
        self.environment.start()
        self.addCleanup(self.environment.stop)
        from hermes_constants import get_default_hermes_root, get_hermes_home
        from hermes_cli.profiles import get_profile_dir
        self.assertTrue(all(p.samefile(self.root) for p in
                            (get_default_hermes_root(), get_hermes_home(), get_profile_dir('default'))))
        package = Path(live.__file__).resolve().parent.parent
        names = ['__init__.py', 'plugin.yaml', 'dashboard/plugin_api.py']
        names += ['finix/' + p.name for p in (package / 'finix').glob('*.py')]
        for name in names:
            self.write(self.root / 'plugins/finix' / name, (package / name).read_bytes())
        self.write(self.root / 'config.yaml', b'plugins:\n  enabled: [finix]\ngateway:\n  standalone: true\n  multiplex_profiles: false\n')
        self.write(self.root / 'note.txt', b'OLD NOTE')
        bundle = Path(os.environ['FINIX_INSTALLED_WORKSPACE_SKILL'])
        for name in RUNTIME:
            self.write(self.root / SKILL / name, (bundle / name).read_bytes())
        self.write(self.root / SKILL / 'scripts/setup.py', b'raise AssertionError("CURRENT EXTRA MUST NOT EXECUTE")\n')
        self.write(self.root / 'skills/unrelated/SKILL.md', b'---\nname: unrelated\n---\n')
        self.profile = Profile(self.root, default_root=self.root)
        core.atomic_json(self.profile.root / 'settings.json',
                         {'storage': 'workspace', 'workspace_transport': 'skill'})
        self.before = {name: cold._leaf(self.root / SKILL / name) for name in RUNTIME}

    def write(self, path, data):
        core.private_dir(path.parent)
        with core.regular_output(path) as fd:
            os.write(fd, data)

    def prepare(self, archived=True, compatible_archive=False):
        from finix.recovery import RecoveryService, CommittedSource
        source = core.private_dir(self.scope / ('source-' + uuid.uuid4().hex))
        self.write(source / 'config.yaml', b'{}\n')
        self.write(source / 'note.txt', b'NEW NOTE')
        if archived:
            for name in RUNTIME:
                data = (Path(os.environ['FINIX_INSTALLED_WORKSPACE_SKILL']) / name).read_bytes() if compatible_archive else b'raise AssertionError("ARCHIVED CODE MUST NOT EXECUTE")\n'
                self.write(source / SKILL / name, data)
            self.write(source / 'skills/archive-only/SKILL.md', b'---\nname: archive-only\n---\n')
        key = self.scope / ('key-' + uuid.uuid4().hex)
        recipient = core.keygen(key)
        cipher_dir, index = self.scope / ('cipher-' + uuid.uuid4().hex), self.scope / ('index-' + uuid.uuid4().hex)
        core.backup(source, cipher_dir, index, recipient,
                    sqlite_temp_dir=core.private_dir(source / 'cache/finix/sqlite'))
        row = core.load_state(index)['backups'][0]
        cipher = cipher_dir / row['name']
        row = dict(row, size=cipher.stat().st_size)
        service = RecoveryService(profile_id=self.profile.profile_id, profile_root=self.root,
                                  activation='synthetic-epoch', protected_roots=[])
        self.addCleanup(service.close)
        def transfer(record, output, cancel):
            with cipher.open('rb') as stream:
                while chunk := stream.read(65536):
                    output.write(chunk)
        service.add_source(CommittedSource('synthetic-local', 'fixture', lambda: [row], transfer))
        selected = service.catalog(self.profile.profile_id, 'synthetic-epoch')[0]
        job = service.start(selected['id'], identity=key,
                            destination=service.allocate_destination(self.profile.profile_id, 'synthetic-epoch'),
                            profile_id=self.profile.profile_id, activation='synthetic-epoch')
        deadline = time.monotonic() + 30
        while service.status(job)['status'] == 'running' and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(service.status(job)['status'], 'succeeded', service.status(job))
        cap = cold.verified_import(service, job, profile_id=self.profile.profile_id,
                                   activation='synthetic-epoch', name='recovered-' + uuid.uuid4().hex[:12])
        return live.prepare_live(cap, self.root, journal_root=self.root / 'cache/finix/default-recovery',
                                 intent='replace-real-default')

    def test_same_bytes_new_current_identity_after_prepare_refuses_publication(self):
        engine = self.prepare()
        target = self.root / SKILL / 'scripts/google_api.py'
        replacement = target.with_name('replacement.py')
        self.write(replacement, target.read_bytes())
        self.assertNotEqual(cold._leaf(replacement)['identity'], self.before['scripts/google_api.py']['identity'])
        os.replace(replacement, target)
        with patch.object(live, '_current_workspace_skill', wraps=live._current_workspace_skill) as rediscover:
            with self.assertRaises(cold.RecoveryError) as error:
                engine.apply(engine.consent())
        rediscover.assert_not_called()  # sealed native membership precedes any new selection/config read
        self.assertEqual(error.exception.code, 'CURRENT_WORKSPACE_SKILL_CHANGED_REPREPARE_REQUIRED')
        self.assertEqual((self.root / 'note.txt').read_bytes(), b'OLD NOTE')
        self.assertEqual(engine.status()['state'], 'prepared')

    def test_current_generation_rechecked_at_registration_and_worker_predrain(self):
        engine = self.prepare()
        target = self.root / SKILL / 'SKILL.md'
        target.write_bytes(target.read_bytes() + b'\nCURRENT metadata changed after Prepare\n')
        controller = live.ColdController(self.root)
        confirmation = {'consent': engine.consent(), 'runtime': {}}
        for boundary in ('register', 'worker-predrain'):
            with self.subTest(boundary=boundary), patch.object(live.ColdController, 'confirmation', return_value=confirmation), \
                    patch.object(live, 'identify', return_value={}), patch.object(live, '_exact', return_value=True):
                with self.assertRaises(cold.RecoveryError) as error:
                    if boundary == 'register':
                        controller.register(engine, confirmation)
                    else:
                        live._verify_engine(engine)
                self.assertEqual(error.exception.code, 'CURRENT_WORKSPACE_SKILL_CHANGED_REPREPARE_REQUIRED')
        self.assertEqual((self.root / 'note.txt').read_bytes(), b'OLD NOTE')

    def replace_current_identity(self):
        target = self.root / SKILL / 'scripts/google_api.py'
        replacement = target.with_name('replacement.py')
        self.write(replacement, target.read_bytes())
        os.replace(replacement, target)

    def test_current_identity_rechecked_under_publication_fence(self):
        engine = self.prepare()
        original = cold.DefaultRecovery._fence
        def race(this, plan, stack):
            original(this, plan, stack)
            self.replace_current_identity()
        with patch.object(cold.DefaultRecovery, '_fence', race):
            with self.assertRaises(cold.RecoveryError) as error:
                engine.apply(engine.consent())
        self.assertEqual(error.exception.code, 'CURRENT_WORKSPACE_SKILL_CHANGED_REPREPARE_REQUIRED')
        self.assertEqual((self.root / 'note.txt').read_bytes(), b'OLD NOTE')
        self.assertEqual(engine.status()['state'], 'prepared')

    def test_late_current_identity_change_rolls_back_data_without_adopting_new_leaf(self):
        engine = self.prepare()
        original = cold.DefaultRecovery._publish
        fired = []
        def race(this, source, target):
            original(this, source, target)
            if not fired:
                fired.append(True)
                self.replace_current_identity()
        with patch.object(cold.DefaultRecovery, '_publish', race):
            with self.assertRaises(cold.RecoveryError) as error:
                engine.apply(engine.consent())
        self.assertTrue(fired)
        self.assertEqual(error.exception.code, 'APPLY_ROLLED_BACK')
        self.assertEqual((self.root / 'note.txt').read_bytes(), b'OLD NOTE')
        self.assertEqual(engine.status()['state'], 'rolled_back')
        self.assertNotEqual(cold._leaf(self.root / SKILL / 'scripts/google_api.py')['identity'],
                            self.before['scripts/google_api.py']['identity'])
        self.assertEqual(cold._workspace_kept(engine.public_plan()['kept_target'])[SKILL + '/scripts/google_api.py']['leaf'],
                         self.before['scripts/google_api.py'])

    def modeled_job(self, engine, *, after_publish=None, after_ready=None):
        # Only SDK process/readiness seams are doubles. Publication is real.
        from types import SimpleNamespace
        from finix import runtime_attempt
        owner = {'instance_id': 'fixture-owner'}
        confirmation = {'consent': engine.consent(), 'runtime': {}}
        controller = live.ColdController(self.root)
        with patch.object(live.ColdController, 'confirmation', return_value=confirmation), \
                patch.object(live, 'identify', return_value=owner), patch.object(live, '_exact', return_value=True):
            job = controller.register(engine, confirmation)
        directory = live._job(self.root, job)
        record = cold._load(directory / 'record.json')
        dispatch = {'nonce': uuid.uuid4().hex, 'record_digest': cold._digest(record)}
        cold._write(directory / 'dispatch.json', dispatch)
        cold._write(directory / 'release.json', {'record_digest': dispatch['record_digest'], 'dispatch_nonce': dispatch['nonce']})
        self.gateway_calls, self.cleanup_calls, self.commits = [], [], []
        def gateway(*args, **kwargs):
            self.gateway_calls.append(True)
            def ready():
                if after_ready:
                    after_ready()
                return owner
            return SimpleNamespace(ready=ready, cleanup=lambda: self.cleanup_calls.append(True),
                                   child=SimpleNamespace(pid=os.getpid()), native=None)
        original = cold.DefaultRecovery.apply
        def apply(this, consent):
            result = original(this, consent)
            if after_publish:
                after_publish()
            return result
        with patch.object(live, 'identify', return_value=owner), patch.object(live, 'drain'), \
                patch.object(live, 'OwnedGateway', side_effect=gateway), \
                patch.object(cold.DefaultRecovery, 'apply', apply), \
                patch.object(runtime_attempt, 'commit_success', side_effect=lambda *a: self.commits.append(True)):
            try:
                live.run_job(self.root, job)
            finally:
                self.modeled_status = controller.status(job)

    def test_controller_refuses_changed_current_before_restart(self):
        engine = self.prepare()
        with self.assertRaises(cold.RecoveryError) as error:
            self.modeled_job(engine, after_publish=self.replace_current_identity)
        self.assertEqual(error.exception.code, 'CURRENT_WORKSPACE_SKILL_CHANGED_REPREPARE_REQUIRED')
        self.assertEqual(self.gateway_calls, [])
        self.assertEqual(self.commits, [])
        self.assertEqual((self.root / 'note.txt').read_bytes(), b'OLD NOTE')

    def test_post_ready_changed_current_cannot_commit_or_trigger_ready_rollback(self):
        engine = self.prepare()
        with self.assertRaises(cold.RecoveryError) as error:
            self.modeled_job(engine, after_ready=self.replace_current_identity)
        self.assertEqual(error.exception.code, 'CURRENT_WORKSPACE_SKILL_CHANGED_REPREPARE_REQUIRED')
        self.assertEqual(self.modeled_status['state'], 'reconciliation_required')
        self.assertEqual(self.cleanup_calls, [])
        self.assertEqual(self.commits, [])
        self.assertEqual((self.root / 'note.txt').read_bytes(), b'NEW NOTE')

    def assert_no_archive_promotion(self):
        engine = self.prepare(compatible_archive=True)
        self.assertEqual(cold._workspace_kept(engine.public_plan()['kept_target']), {})
        engine.apply(engine.consent())
        self.assertFalse((self.root / SKILL / 'SKILL.md').exists())
        self.assertFalse((self.root / SKILL / 'scripts/google_api.py').exists())
        self.assertTrue((Path(engine._plan()['source']) / '.finix-paused' / SKILL / 'SKILL.md').exists())

    def test_unselected_transport_keeps_every_archived_skill_inert(self):
        core.atomic_json(self.profile.root / 'settings.json', {'storage': 'workspace', 'workspace_transport': 'finix'})
        self.assert_no_archive_promotion()

    def test_other_backend_keeps_every_archived_skill_inert(self):
        core.atomic_json(self.profile.root / 'settings.json', {'storage': 'local', 'workspace_transport': 'skill'})
        self.assert_no_archive_promotion()

    def test_missing_current_never_promotes_compatible_archive(self):
        (self.root / SKILL / 'SKILL.md').unlink()
        self.assert_no_archive_promotion()

    def test_disabled_current_never_promotes_compatible_archive(self):
        marker = self.root / SKILL / 'SKILL.md'
        marker.write_bytes(marker.read_bytes().replace(b'version: 1.2.0', b'version: 1.2.0\ndisabled: true'))
        self.assert_no_archive_promotion()

    def test_changed_current_never_promotes_compatible_archive(self):
        (self.root / SKILL / 'scripts/google_api.py').write_bytes(b'raise AssertionError("CURRENT INCOMPATIBLE")\n')
        self.assert_no_archive_promotion()

    def test_ambiguous_current_never_promotes_compatible_archive(self):
        self.write(self.root / 'skills/second/SKILL.md', (self.root / SKILL / 'SKILL.md').read_bytes())
        self.assert_no_archive_promotion()

    def test_absent_archive_dependency_still_retains_current_and_exact_rollback(self):
        engine = self.prepare(archived=False)
        engine.apply(engine.consent())
        self.assertEqual(transport.discover_skill(self.root), self.root / SKILL)
        reopened = cold.DefaultRecovery(engine.directory, engine.plan_digest)
        self.assertEqual(reopened.rollback(reopened.consent())['state'], 'rolled_back')
        self.assertEqual((self.root / 'note.txt').read_bytes(), b'OLD NOTE')
        self.assertEqual((self.root / 'skills/unrelated/SKILL.md').read_bytes(), b'---\nname: unrelated\n---\n')
        self.assertIn(b'CURRENT EXTRA', (self.root / SKILL / 'scripts/setup.py').read_bytes())
        for name, leaf in self.before.items():
            self.assertEqual(cold._leaf(self.root / SKILL / name), leaf)
            self.assertNotIn(SKILL + '/' + name, cold._load(engine.directory / 'journal.json')['old'])

    def test_raw_digest_change_with_compatible_normalized_pin_requires_reprepare(self):
        engine = self.prepare()
        target = self.root / SKILL / 'scripts/google_api.py'
        target.write_bytes(target.read_bytes().replace(b'\n', b'\r\n'))
        transport.contract_files(self.root / SKILL)  # still a compatible normalized pin
        with self.assertRaises(cold.RecoveryError) as error:
            engine.apply(engine.consent())
        self.assertEqual(error.exception.code, 'CURRENT_WORKSPACE_SKILL_CHANGED_REPREPARE_REQUIRED')
        self.assertEqual(engine.status()['state'], 'prepared')

    def test_changed_selection_after_prepare_requires_reprepare(self):
        engine = self.prepare()
        core.atomic_json(self.profile.root / 'settings.json', {'storage': 'local', 'workspace_transport': 'skill'})
        with self.assertRaises(cold.RecoveryError) as error:
            engine.apply(engine.consent())
        self.assertEqual(error.exception.code, 'CURRENT_WORKSPACE_SKILL_CHANGED_REPREPARE_REQUIRED')
        self.assertEqual(engine.status()['state'], 'prepared')

    def test_prepare_cancellation_leaves_old_current_native_state_untouched(self):
        from finix import preview_budget
        import threading
        cancelled = threading.Event()
        original = live._current_workspace_skill
        def capture(root):
            result = original(root)
            cancelled.set()
            return result
        with patch.object(live, '_current_workspace_skill', capture), preview_budget.scope(cancelled=cancelled.is_set):
            with self.assertRaises(preview_budget.PreviewBudgetError) as error:
                self.prepare()
            self.assertEqual(error.exception.code, 'PREVIEW_CANCELLED')
            cancelled.clear()  # allow the scope's normal closing check
        self.assertEqual((self.root / 'note.txt').read_bytes(), b'OLD NOTE')
        self.assertTrue((self.root / SKILL / 'scripts/setup.py').exists())
        for name, leaf in self.before.items():
            self.assertEqual(cold._leaf(self.root / SKILL / name), leaf)

    def test_cancellation_inside_current_recheck_is_not_misreported_as_generation_change(self):
        from finix import preview_budget
        import threading
        cancelled = threading.Event()
        original = live._current_workspace_skill
        calls = []
        def capture(root):
            calls.append(True)
            if len(calls) == 2:
                cancelled.set()
            return original(root)
        with patch.object(live, '_current_workspace_skill', capture), preview_budget.scope(cancelled=cancelled.is_set):
            try:
                with self.assertRaises(preview_budget.PreviewBudgetError) as error:
                    self.prepare()
                self.assertEqual(error.exception.code, 'PREVIEW_CANCELLED')
            finally:
                cancelled.clear()
        self.assertEqual((self.root / 'note.txt').read_bytes(), b'OLD NOTE')

    def child(self, code, *args):
        import subprocess
        import sys
        sdk = Path(os.environ['FINIX_TEST_SDK_SOURCE'])
        bootstrap = ('import sys,os\n'
                     'sys.path[:0]=' + repr([str(self.root / 'plugins/finix'), str(sdk)]) + '\n'
                     'def guard(event,args):\n'
                     '    if event in ("socket.connect","socket.getaddrinfo","subprocess.Popen","os.system"): raise AssertionError("OFFLINE_ONLY")\n'
                     '    if event=="open" and isinstance(args[0],(str,bytes,os.PathLike)) and any(s in os.fsdecode(args[0]).lower() for s in ("google_token","client_secret","auth.json")): raise AssertionError("NO_CREDENTIALS")\n'
                     'sys.addaudithook(guard)\n')
        return subprocess.run([sys.executable, '-I', '-B', '-c', bootstrap + code, *map(str, args)],
                              env=dict(os.environ), cwd=self.scope, capture_output=True, timeout=30)

    def test_controller_death_retains_current_identity_and_durable_data_rollback(self):
        engine = self.prepare()
        result = self.child('''from pathlib import Path
from finix.default_recovery import DefaultRecovery
engine=DefaultRecovery(Path(sys.argv[1]),sys.argv[2])
original=DefaultRecovery._publish
def die(this,source,target):
    original(this,source,target)
    os._exit(73)
DefaultRecovery._publish=die
engine.apply(engine.consent())
''', engine.directory, engine.plan_digest)
        self.assertEqual(result.returncode, 73, result.stderr.decode('utf8', 'replace'))
        reopened = cold.DefaultRecovery(engine.directory, engine.plan_digest)
        self.assertEqual(reopened.rollback(reopened.consent())['state'], 'rolled_back')
        self.assertEqual((self.root / 'note.txt').read_bytes(), b'OLD NOTE')
        self.assertTrue((self.root / SKILL / 'scripts/setup.py').exists())
        for name, leaf in self.before.items():
            self.assertEqual(cold._leaf(self.root / SKILL / name), leaf)

    def test_published_current_loads_in_guarded_worker_without_sibling_or_credential_execution(self):
        import json
        engine = self.prepare()
        engine.apply(engine.consent())
        # Unpinned siblings must never become imports, even if planted later.
        self.write(self.root / SKILL / 'scripts/hermes_constants.py', b'raise AssertionError("SIBLING EXECUTED")\n')
        self.write(self.root / SKILL / 'scripts/gws_bridge.py', b'raise AssertionError("SIBLING EXECUTED")\n')
        result = self.child('''import json,types
from pathlib import Path
from finix import workspace_skill_transport as transport, workspace_skill_worker as worker
home=Path(os.environ['HERMES_HOME'])
skill=transport.discover_skill(home)
module=transport.load_skill(skill)
module.get_credentials=lambda: object()  # offline seam only; no credential read
class Request:
    def execute(self,**kwargs): return {'user':{'permissionId':'synthetic-requester'}}
class Service:
    def about(self):return self
    def get(self,**kwargs):return Request()
discovery=types.ModuleType('googleapiclient.discovery')
discovery.build=lambda api,version,**kwargs:Service()
package=types.ModuleType('googleapiclient');package.__path__=[]
sys.modules['googleapiclient']=package;sys.modules['googleapiclient.discovery']=discovery
service=module.build_service('drive','v3')
print(json.dumps(worker.perform(service,{'operation':'identity','args':[]})))
''')
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf8', 'replace'))
        self.assertEqual(json.loads(result.stdout), {'account_id': 'synthetic-requester'})

    def test_selected_current_runtime_survives_true_replacement_not_archive(self):
        engine = self.prepare()
        engine.apply(engine.consent())
        self.assertEqual((self.root / 'note.txt').read_bytes(), b'NEW NOTE')
        self.assertEqual(transport.discover_skill(self.root), self.root / SKILL)
        for name, leaf in self.before.items():
            self.assertEqual(cold._leaf(self.root / SKILL / name), leaf)
        self.assertFalse((self.root / SKILL / 'scripts/setup.py').exists())
        self.assertFalse((self.root / 'skills/unrelated/SKILL.md').exists())
        self.assertFalse((self.root / 'skills/archive-only/SKILL.md').exists())
        parked = Path(engine._plan()['source']) / '.finix-paused' / SKILL / 'scripts/google_api.py'
        self.assertIn(b'ARCHIVED CODE', parked.read_bytes())
        kept = engine.public_plan()['kept_target']
        self.assertTrue(all(SKILL + '/' + name in kept for name in RUNTIME))


if __name__ == '__main__':
    unittest.main()
