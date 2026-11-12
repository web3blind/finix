"""True-default maintenance. Mount only under host authenticated profile scope."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import uuid
from . import core, default_recovery as cold, default_recovery_lifecycle as lifecycle
from .preview_budget import bounded
RecoveryError = cold.RecoveryError


def activation(profile):
    from .recovery_http import binding
    return binding(profile)


def authority(profile):
    from hermes_constants import get_default_hermes_root, get_hermes_home
    from hermes_cli.profiles import get_profile_dir
    root = cold._root(get_default_hermes_root())
    if not re.fullmatch('[a-f0-9]{64}', profile.profile_id):
        raise RecoveryError('LOCAL_OWNER_REQUIRED')
    if not all(os.path.samefile(root, p) for p in (profile.home, getattr(profile, 'default_root', root), get_hermes_home(), get_profile_dir('default'))):
        raise RecoveryError('LOCAL_OWNER_REQUIRED')
    cold._private(root)
    return root


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{32}', value):
        raise RecoveryError('NOT_FOUND')
    return value


def token(challenge):
    return hashlib.sha256(json.dumps(challenge, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def public_error(exc):
    allowed = {'CONSENT_REQUIRED','LOCAL_OWNER_REQUIRED','NOT_FOUND','STALE_PROFILE',
               'ROOT_EPOCH_CHANGED','WRONG_DEFAULT_ROOT','SDK_VERSION_NOT_SUPPORTED',
               'DEFAULT_AUTORUN_NOT_SUPPORTED','SIBLING_AUTORUN_NOT_SUPPORTED',
               'OWNER_NOT_VERIFIED','REOPEN_LIVE_OWNER_UNVERIFIED','BUSY',
               'INVALID_CAPABILITY','INVALID_CANDIDATE','INVALID_IDENTITY',
               'DECRYPT_FAILED','INCOMPLETE_ACK_REQUIRED','JOB_NOT_PREPARED',
               'DEFAULT_RECOVERY_REVIEW_REQUIRED','PREVIEW_DEADLINE',
               'PREVIEW_CANCELLED','PREVIEW_INTERRUPTED','PREVIEW_ACK_UNCERTAIN'}
    code = getattr(exc,'code',None)
    return code if code in allowed else 'DEFAULT_RECOVERY_FAILED'


class _DefaultAuthority:
    """Live default owner/source fence, composed into an exact base service."""
    def __init__(self, profile):
        self.profile = profile
        self.root = authority(profile)
        self.identity = cold._identity(self.root)
        self.profile_id = profile.profile_id
        self.generation = activation(profile)

    def check(self):
        from .recovery import RecoveryError as WorkerError
        try:
            root = authority(self.profile)
            with core.regular_input(self.profile.root/'backend.json') as fd:
                current_id = json.loads(core.read_regular(fd, 1024))['profile_id']
            if (current_id != self.profile_id or root != self.root or cold._identity(root) != self.identity
                    or self.profile.profile_id != self.profile_id
                    or activation(self.profile) != self.generation):
                raise WorkerError('STALE_PROFILE')
        except Exception:
            raise WorkerError('STALE_PROFILE') from None

    def lease(self):
        from contextlib import contextmanager
        from .recovery import RecoveryError as WorkerError
        @contextmanager
        def held():
            lease = core.locked(self.profile.directories())
            try:
                lease.__enter__()
            except (OSError, core.FinixError):
                raise WorkerError('BUSY') from None
            try:
                self.check()
                yield
            finally:
                lease.__exit__(None, None, None)
        return held()


def new_service(profile):
    # Exact RecoveryService is required by cold.verified_import. Ordinary
    # FencedService jobs cannot be reclassified or adopted as capabilities.
    guard = _DefaultAuthority(profile)
    root = guard.root
    from .recovery import RecoveryService, CommittedSource
    from .recovery_sources import register_cloud
    service=RecoveryService(profile_id=profile.profile_id,profile_root=root,
        activation=guard.generation,protected_roots=[root,root/'profiles',profile.root.parent],
        authority_guard=guard)
    service.add_local('local',profile.root/'copies',profile.root/'index')
    source=service._sources['local']
    service.add_source(CommittedSource('local',service.activation,source.records,source.transfer))
    register_cloud(service,profile)
    return service


# Review gate: not an environment/client option. No deployment approval.
DEFAULT_APPLY_REVIEWED = True


def client_preview(engine):
    """Closed UI projection, never expose native paths or raw plan inventories.

    Counts describe the sealed replacement census, not a recursively cloned host.
    The root binding is a comparison token, not client authority over a path.
    """
    plan = engine.public_plan()
    preservation = plan.get('runtime_attempt', {})
    if (plan.get('replacement_policy') != 'profile-context-v1'
            or plan.get('replacement_roots') != ['memories', 'skills']
            or preservation.get('policy') != 'runtime-attempt-preserve-v1'
            or preservation.get('storage_format') != 'opaque-container-v2'
            or preservation.get('retention') != 'explicit-cleanup-only'
            or preservation.get('keepSharedResources') is not True
            or preservation.get('storage_cost') != 'complete old copies + complete current copies and native originals'):
        raise RecoveryError('DEFAULT_RECOVERY_REVIEW_REQUIRED')
    return dict(contract='default-maintenance-preview-v1',
                plan_digest=engine.plan_digest,
                root_binding=token({'root_identity': engine.consent()['root_identity']}),
                scope='replace-real-default',replacement_policy=plan['replacement_policy'],
                replacement_roots=list(plan['replacement_roots']),
                affected_files=len(plan['target_owned']),removed_files=len(plan['deletions']),
                kept_shared_members=plan['kept_target_count'],
                kept_inventory='opaque-subtrees-not-cloned',inert_only=True)


class DefaultRecoveryAPI:
    def _base(self, root):
        return root/'cache/finix/default-api'

    @bounded
    def prepare(self, profile, service, job, *, consent=False):
        if consent is not True:
            raise RecoveryError('CONSENT_REQUIRED')
        root = authority(profile)
        generation = activation(profile)
        cap = cold.verified_import(service, job, profile_id=profile.profile_id,
                                   activation=generation, name='recovered-'+uuid.uuid4().hex[:16])
        engine = lifecycle.prepare_live(cap, root, journal_root=root/'cache/finix/default-recovery',
                                        intent='replace-real-default')
        challenge = lifecycle.ColdController(root).confirmation(engine)
        if activation(profile) != generation:
            raise RecoveryError('STALE_PROFILE')
        preview = client_preview(engine)
        opaque = uuid.uuid4().hex
        directory = core.private_dir(self._base(root)/opaque)
        record = dict(version=1, id=opaque, profile_id=profile.profile_id,
                      root_identity=cold._identity(root), activation=generation,
                      engine=identifier(engine.directory.name), digest=engine.plan_digest,
                      challenge=challenge, job=None)
        cold._write(directory/'record.json', record)
        return dict(id=opaque, state='prepared', confirmation=token(challenge),
                    preview=preview, apply_enabled=DEFAULT_APPLY_REVIEWED,
                    downtime=True, rollback=True, active_shared_autoruns_supported=False,
                    preservation={k:engine.public_plan()['runtime_attempt'][k] for k in
                        ('policy','storage_format','consent_notice','retention','storage_cost','keepSharedResources')})

    def _lookup(self, profile, opaque):
        root = authority(profile)
        path = self._base(root)/identifier(opaque)/'record.json'
        if not path.exists():
            raise RecoveryError('NOT_FOUND')
        record = cold._load(path)
        if (not isinstance(record, dict) or record.get('version') != 1 or record.get('id') != opaque
                or record.get('profile_id') != profile.profile_id
                or record.get('root_identity') != cold._identity(root)):
            raise RecoveryError('NOT_FOUND')
        identifier(record['engine'])
        if record['job'] is not None:
            identifier(record['job'])
        return record

    def _public(self, opaque, state):
        allowed = {'prepared','registered','acknowledged','draining','stopped','publishing',
                   'published_offline','ready','rolling_back','rolled_back_ready','rollback_not_ready','refused',
                   'manual_required','reconciliation_required'}
        value = state.get('state')
        if value not in allowed:
            raise RecoveryError('STATUS_UNAVAILABLE')
        error = state.get('error')
        # Worker exception names and private fields are never a public contract.
        return dict(id=opaque, state=value, error=None if error is None else 'DEFAULT_RECOVERY_FAILED')

    def apply(self, profile, opaque, confirmation, *, consent=False):
        if not DEFAULT_APPLY_REVIEWED:
            raise RecoveryError('DEFAULT_RECOVERY_REVIEW_REQUIRED')
        if consent is not True or not isinstance(confirmation,str) or not re.fullmatch('[a-f0-9]{64}',confirmation):
            raise RecoveryError('CONSENT_REQUIRED')
        root = authority(profile)
        record = self._lookup(profile,opaque)
        if not hmac.compare_digest(confirmation, token(record['challenge'])):
            raise RecoveryError('CONSENT_REQUIRED')
        from .backup_jobs import file_lease
        with file_lease(self._base(root)/opaque/'apply.lock'):
            record = self._lookup(profile,opaque)
            controller = lifecycle.ColdController(root)
            if record['job'] is None:
                if activation(profile) != record['activation']:
                    raise RecoveryError('STALE_PROFILE')
                engine = cold.DefaultRecovery(root/'cache/finix/default-recovery'/record['engine'],record['digest'])
                if controller.confirmation(engine) != record['challenge']:
                    raise RecoveryError('ROOT_EPOCH_CHANGED')
                record['job'] = controller.register(engine,record['challenge'])
                cold._write(self._base(root)/opaque/'record.json',record)
            # Persist the private worker lookup BEFORE releasing detached work.
            answer = controller.launch(record['job'])
            if answer.get('state') in {'ready', 'rolled_back_ready'}:
                return self.status(profile,opaque)
            return self._public(opaque,answer)

    def status(self, profile, opaque):
        record = self._lookup(profile,opaque)
        root = authority(profile)
        state = lifecycle.ColdController(root).status(record['job']) if record['job'] else {'state':'prepared'}
        if state.get('state') in {'ready', 'rolled_back_ready'}:
            # Durable terminal status is historical evidence, not live SDK READY.
            # Revalidate the exact protected SDK owner, not Finix on an archived
            # plugin or a different runtime that subsequently occupied this root.
            try:
                current = lifecycle.identify(root)
                exact = lifecycle._exact(current,root) and current == state.get('identity')
            except Exception:
                exact = False
            if not exact:
                state = {'state':'reconciliation_required','error':'ROOT_EPOCH_CHANGED'}
        return self._public(opaque,state)

    def reopen(self, profile, opaque, *, consent=False):
        if not DEFAULT_APPLY_REVIEWED:
            raise RecoveryError('DEFAULT_RECOVERY_REVIEW_REQUIRED')
        if consent is not True:
            raise RecoveryError('CONSENT_REQUIRED')
        record = self._lookup(profile,opaque)
        if record['job'] is None:
            raise RecoveryError('JOB_NOT_PREPARED')
        answer = lifecycle.ColdController(authority(profile)).reopen(record['job'])
        if answer.get('state') in {'ready', 'rolled_back_ready'}:
            return self.status(profile,opaque)
        return self._public(opaque,answer)

