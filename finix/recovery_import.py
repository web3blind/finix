"""Publish an inert recovered profile through the supported Hermes importer.

No activation, plugin discovery, gateway start, or credential hydration.
Only RecoveryService.handoff is a candidate authority; HTTP paths are never accepted.
"""
from contextlib import ExitStack
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tarfile
import uuid

import yaml

from . import core


class RecoveryImportError(Exception):
    """Stable public code only; never expose filesystem/provider exception text."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


# Reviewed host enum AND bundled deferred-platform manifests (no plugin imports).
# Local is the manual-chat surface and must remain usable.
BUNDLED_PLATFORMS = frozenset('a2a bluebubbles buzz dingtalk discord email feishu google_chat '
    'irc line matrix mattermost ntfy photon qqbot raft signal simplex slack sms teams '
    'telegram wecom weixin whatsapp whatsapp_cloud yuanbao'.split())
PLATFORMS = frozenset('telegram discord whatsapp whatsapp_cloud slack signal mattermost '
    'matrix email sms dingtalk api_server webhook msgraph_webhook feishu wecom '
    'wecom_callback weixin bluebubbles qqbot yuanbao relay'.split()) | BUNDLED_PLATFORMS | frozenset(
        name + '-platform' for name in BUNDLED_PLATFORMS)
PARKED_ROOTS = frozenset({'plugins', 'desktop-plugins', 'hooks', 'agent-hooks'})
RUNTIME_ROOTS = frozenset({'installs', 'tools', 'cache', 'models', 'runtimes', 'node',
    'profiles', 'hermes-agent', '.worktrees', 'runtime', 'logs', 'tmp', 'temp',
    'bin', 'node_modules', '.hermes-runtime', '.venv', 'venv'})
RUNTIME_FILES = frozenset(value.casefold() for value in {'gateway.pid', 'gateway.lock', 'gateway.parked', 'cron.pid',
    'processes.json', 'gateway_state.json', 'auth.lock', 'shared/nous_auth.lock',
    'shell-hooks-allowlist.json', 'active_profile', 'cron/.tick.lock',
    'cron/ticker_heartbeat', 'cron/ticker_last_success',
    'bot-desktop/launcher.pid', 'bot-desktop/env', 'bot-desktop/rfb.sock',
    'bot-desktop/lease.json', 'bot-desktop/lease.lock',
    'bot-desktop/browser-profile/DevToolsActivePort',
    'bot-desktop/browser-profile/SingletonLock',
    'bot-desktop/browser-profile/SingletonCookie', 'bot-desktop/browser-profile/SingletonSocket'})
PAUSED = '.finix-paused'
MAX_CONFIG = 8 * 1024 * 1024
UNSAFE_ENV = frozenset({'HERMES_HOME', 'HERMES_PROFILE', 'HERMES_PROFILE_NAME',
    'HERMES_CONFIG', 'HERMES_ENV', 'HERMES_ACCEPT_HOOKS', 'HERMES_PLUGIN_DIR',
    'HERMES_CONFIG_BACKEND', 'HERMES_CONFIG_BACKEND_URL', 'PATH', 'PYTHONPATH',
    'PYTHONHOME', 'PYTHONSTARTUP', 'BASH_ENV', 'ENV', 'NODE_OPTIONS', 'LD_PRELOAD',
    'LD_LIBRARY_PATH', 'VIRTUAL_ENV', 'HERMES_BUNDLED_PLUGINS',
    'HERMES_ENABLE_PROJECT_PLUGINS', 'HERMES_OPTIONAL_MCPS', 'HERMES_CONFIG_PATH',
    'HERMES_ENV_PATH', 'HERMES_YOLO_MODE', 'HERMES_NEMO_RELAY_PLUGINS_TOML'})
DESKTOP_SDK_CONTRACT_VERSION = '0.23.0'
HOST_EXIT_CODES = {'HOST_UNAVAILABLE': 20, 'INVALID_CONFIG': 21, 'PROFILE_EXISTS': 22,
                   'HOST_VERIFICATION_FAILED': 23, 'INVALID_NAME': 24,
                   'HOST_IMPORT_CONTRACT_UNSUPPORTED': 25}


def _safe_env(data):
    kept = []
    changed = False
    for line in data.decode('utf-8-sig').splitlines(keepends=True):
        assignment = re.match(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)', line)
        if assignment and assignment[1].upper() in UNSAFE_ENV:
            changed = True
            value = assignment[2].strip()
            if value.startswith(('"', "'")) and (len(value) < 2 or value[-1] != value[0]):
                raise RecoveryImportError('ENV_REVIEW_REQUIRED')
            continue
        kept.append(line)
    return ''.join(kept).encode('utf-8') if changed else data


def _yaml_mapping(data):
    # Reject aliases, recursive objects and duplicate keys rather than host-dependent coercion.
    class StrictLoader(yaml.SafeLoader):
        def compose_node(self, parent, index):
            if self.check_event(yaml.AliasEvent):
                raise RecoveryImportError('INVALID_CONFIG')
            return super().compose_node(parent, index)
        def construct_mapping(self, node, deep=False):
            pairs = self.construct_pairs(node, deep=deep)
            result = {}
            for key, value in pairs:
                if not isinstance(key, str) or key in result:
                    raise RecoveryImportError('INVALID_CONFIG')
                result[key] = value
            return result
    try:
        value = yaml.load(data, Loader=StrictLoader)
    except (yaml.YAMLError, UnicodeError, RecursionError):
        raise RecoveryImportError('INVALID_CONFIG') from None
    if not isinstance(value, dict):
        raise RecoveryImportError('INVALID_CONFIG')
    return value


def _safe_config(data):
    config = _yaml_mapping(data)
    for key in list(config):
        if key.upper() in UNSAFE_ENV:
            config.pop(key)
    # Model selection is optional for stock SDK history/onboarding serving.
    # Preserve missing/empty/null values; readiness is confirmed by the SDK,
    # not by inventing a model or provider. Reject malformed untrusted types.
    model = config.get('model')
    if model is not None and not isinstance(model, (str, dict)):
        raise RecoveryImportError('INVALID_CONFIG')
    if isinstance(model, dict) and model.get('default') is not None and not isinstance(model['default'], str):
        raise RecoveryImportError('INVALID_CONFIG')
    for key in ('gateway', 'platforms', 'plugins', 'skills', 'mcp_servers', 'secrets'):
        if key in config and not isinstance(config[key], dict):
            raise RecoveryImportError('INVALID_CONFIG')
    gateway = config.setdefault('gateway', {})
    names = set(PLATFORMS)
    for parent in (config, gateway):
        blocks = parent.setdefault('platforms', {})
        if not isinstance(blocks, dict):
            raise RecoveryImportError('INVALID_CONFIG')
        names.update(blocks)
        for block in blocks.values():
            if not isinstance(block, dict):
                raise RecoveryImportError('INVALID_CONFIG')
    for parent in (config, gateway):
        for name in names:
            if name == 'local':
                continue
            parent['platforms'].setdefault(name, {})['enabled'] = False
            if name in parent:
                if not isinstance(parent[name], dict):
                    raise RecoveryImportError('INVALID_CONFIG')
                parent[name]['enabled'] = False
        # Remove recovered cross-profile routing; manual chat stays local to new profile.
        parent['profile_routes'] = []
    config.setdefault('plugins', {})['enabled'] = []
    config['hooks'] = {}
    config.setdefault('skills', {})['auto_load'] = []
    for key in ('mcp_servers', 'secrets'):
        for name, block in config.get(key, {}).items():
            # Host secrets.sources is precedence metadata, not a provider.
            # Keep its list type/order; never register or fetch external sources.
            if key == 'secrets' and name == 'sources':
                if not isinstance(block, list) or any(not isinstance(x, str) for x in block):
                    raise RecoveryImportError('INVALID_CONFIG')
                continue
            if not isinstance(block, dict):
                raise RecoveryImportError('INVALID_CONFIG')
            block['enabled'] = False
    return yaml.safe_dump(config, allow_unicode=True, sort_keys=False).encode('utf-8')


def _read(path, limit):
    # Paths come only from pinned _members traversal, including Linux fd anchors.
    with core.regular_input(path) as fd:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RecoveryImportError('INVALID_CANDIDATE')
        data = core.read_regular(fd, limit)
        if len(data) > limit:
            raise RecoveryImportError('INVALID_CONFIG')
        return data


def _members(root, stack, prefix=''):
    """Pin every traversed directory, refuse links/reparse and nonregular files."""
    anchor = stack.enter_context(core.locked(root))
    view = root if os.name == 'nt' else Path('/proc/self/fd') / str(anchor)
    for path in sorted(view.iterdir()):
        relative = prefix + path.name
        info = path.lstat()
        if path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise RecoveryImportError('INVALID_CANDIDATE')
        if stat.S_ISDIR(info.st_mode):
            yield from _members(path, stack, relative + '/')
        elif stat.S_ISREG(info.st_mode):
            yield relative, path
        else:
            raise RecoveryImportError('INVALID_CANDIDATE')


def _add_bytes(archive, name, data, expected):
    entry = tarfile.TarInfo(name)
    entry.size, entry.mode = len(data), 0o600
    archive.addfile(entry, io.BytesIO(data))
    expected[name.split('/', 1)[1]] = hashlib.sha256(data).hexdigest()


def _prepare_history(source, destination, target_name, stage, *, source_binding=None, expected_payload=None):
    """Rekey only a new private inert copy of authenticated archived legacy history.

    A unanimous row stamp is necessary but not sufficient: the manifest's
    integrity-bound namespace must match it exactly. It is NOT proof of the
    encrypting sender or authority to convert a foreign principal. Old unstamped
    archives remain readable but cannot authorize migration from rows alone.
    Canonical policy/principal/route receipts are explicitly NOT portable here.
    Never call profile_identity's global/root migration wrapper.
    """
    import sqlite3
    import inspect
    from contextlib import closing
    destination, stage = Path(destination), Path(stage)
    if destination.parent != stage or destination.exists():
        raise RecoveryImportError('HISTORY_OWNERSHIP_UNSUPPORTED')
    if not isinstance(target_name, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', target_name) or target_name == 'default':
        raise RecoveryImportError('INVALID_NAME')
    # Copy through the existing native no-follow/private-file boundaries. All
    # SDK initialization and writes below are confined to this disposable file.
    with core.regular_input(source) as src, core.regular_output(destination) as dst:
        with os.fdopen(os.dup(src), 'rb') as inp, os.fdopen(os.dup(dst), 'wb') as out:
            shutil.copyfileobj(inp, out)
    if expected_payload is not None and expected_payload != {
            'size': destination.stat().st_size, 'sha256': core.digest_file(destination)}:
        raise RecoveryImportError('HISTORY_SOURCE_BINDING_REQUIRED')
    with closing(sqlite3.connect(destination.as_uri() + '?mode=ro', uri=True)) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'sessions' not in tables:
            return None  # opaque non-history SQLite payload; no SDK writes
        if conn.execute('PRAGMA integrity_check').fetchone() != ('ok',):
            raise RecoveryImportError('HISTORY_OWNERSHIP_UNSUPPORTED')
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute('SELECT * FROM sessions')]
        owners = {r.get('profile_name') for r in rows}
        if not rows:
            return None
        if len(owners) != 1:
            raise RecoveryImportError('HISTORY_OWNERSHIP_UNSUPPORTED')
        source_name = next(iter(owners))
        if not isinstance(source_name, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', source_name) or source_name == target_name:
            raise RecoveryImportError('HISTORY_OWNERSHIP_UNSUPPORTED')
        if any(r.get('source') not in {'cli', 'tui', 'gui', 'desktop'} or any(
                r.get(k) for k in ('user_id', 'session_key', 'chat_id', 'origin_json', 'transport_profile')) for r in rows):
            raise RecoveryImportError('HISTORY_OWNERSHIP_UNSUPPORTED')
        allowed = {'schema_version', 'system_prompts', 'sessions', 'messages',
                   'sqlite_sequence', 'session_model_usage', 'state_meta'}
        empty = {'runtime_epoch', 'session_admissions', 'worker_executions', 'worker_receipts',
                 'gateway_routing', 'gateway_hygiene_state', 'conversation_generations',
                 'gateway_heartbeats', 'compression_locks', 'session_turn_leases', 'async_delegations'}
        for table in tables:
            if table in allowed or table.startswith(('messages_fts_', 'messages_fts_trigram_')) or table in {'messages_fts', 'messages_fts_trigram'}:
                continue
            if table not in empty or conn.execute('SELECT 1 FROM "' + table + '" LIMIT 1').fetchone():
                raise RecoveryImportError('HISTORY_OWNERSHIP_UNSUPPORTED')
        meta = {r[0] for r in conn.execute('SELECT key FROM state_meta')}
        if meta - {'store_instance_id', 'store_created_at_utc', 'message_uid_backfill', 'fts_storage_version', 'db_file_generation'}:
            raise RecoveryImportError('HISTORY_OWNERSHIP_UNSUPPORTED')
        schema = conn.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall()
        if hashlib.sha256(json.dumps([tuple(r) for r in schema], ensure_ascii=True, separators=(',', ':')).encode()).hexdigest() != '1d7cbb0ac1fcbe8caa240171f948b08fb5de1f73e707344d116424b16ae48a6c':
            raise RecoveryImportError('HISTORY_OWNERSHIP_UNSUPPORTED')
        if source_binding != source_name:
            raise RecoveryImportError('HISTORY_SOURCE_BINDING_REQUIRED')
        frozen = {}
        for table in tables:
            values = [list(r) for r in conn.execute('SELECT * FROM "' + table + '"')]
            if table == 'sessions':
                columns = [r[1] for r in conn.execute('PRAGMA table_info(sessions)')]
                index = columns.index('profile_name')
                for value in values:
                    value[index] = target_name
            frozen[table] = sorted((tuple(r) for r in values), key=repr)
    from hermes_state import SessionDB
    method = inspect.getsource(SessionDB.rekey_profile_state).replace('\r\n', '\n')
    if hashlib.sha256(method.encode()).hexdigest() != 'cbb9a632c35add9c8150990f916597a1711b0e311755170dadf9b50fd35c9301':
        raise RecoveryImportError('HOST_UNAVAILABLE')
    # SDK public transactional namespace migration; never SQL UPDATE ownership.
    db = SessionDB(destination)
    try:
        counts = db.rekey_profile_state(source_name, target_name)
    finally:
        db.close()
    with closing(sqlite3.connect(destination.as_uri() + '?mode=ro', uri=True)) as conn:
        for table, expected in frozen.items():
            if sorted(conn.execute('SELECT * FROM "' + table + '"').fetchall(), key=repr) != expected:
                raise RecoveryImportError('HISTORY_OWNERSHIP_UNSUPPORTED')
    return dict(version=1, source_name=source_name, target_name=target_name,
                contract='sdk-legacy-unrouted-v1', counts=counts,
                sdk_rekey_sha256=hashlib.sha256(method.encode()).hexdigest())


class RecoveryImporter:
    def __init__(self, service, *, host=None):
        self.service = service
        self.host = host  # dependency injection for no-runtime unit tests

    def import_job(self, job_id, *, profile_id, activation, name, consent_import,
                   acknowledge_incomplete=False, history_mode='namespace-rekey'):
        """Return a path-free parked import receipt, NOT an activation acknowledgement.

        Adapter must supply authenticated profile/activation fences, never client paths.
        External extras remain in the completed recovery candidate, untouched.
        """
        if history_mode not in {'namespace-rekey', 'new-copy'}:
            raise RecoveryImportError('INVALID_REQUEST')
        if consent_import is not True:
            raise RecoveryImportError('IMPORT_CONSENT_REQUIRED')
        if type(acknowledge_incomplete) is not bool or not isinstance(name, str):
            raise RecoveryImportError('INVALID_REQUEST')
        try:
            host = self.host
            if host is None:
                from hermes_cli import profiles as host
            # Strict as-given validation: do not repair path-like or malformed names.
            host.validate_profile_name(name)
            if name == 'default':
                raise RecoveryImportError('INVALID_NAME')
        except RecoveryImportError:
            raise
        except (TypeError, ValueError):
            raise RecoveryImportError('INVALID_NAME') from None
        except ImportError:
            raise RecoveryImportError('HOST_UNAVAILABLE') from None
        if self.host is None:
            verify_host_compatibility(host)
        try:
            # Serialize cancellation/profile invalidation through publication, using
            # the SAME fence lock as RecoveryService, not an unrelated UI state.
            with self.service._lock:
                candidate = self.service.handoff(job_id, profile_id, activation)
                job = self.service.status(job_id)
                result = job.get('result', {})
                complete, omitted = result.get('complete'), result.get('omitted_count')
                if (type(complete) is not bool or type(omitted) is not int or omitted < 0
                        or complete != (omitted == 0) or candidate.get('complete') != complete):
                    raise RecoveryImportError('INVALID_CANDIDATE')
                if not complete and acknowledge_incomplete is not True:
                    raise RecoveryImportError('INCOMPLETE_ACK_REQUIRED')
                return self._import(host, candidate, name, profile_id, complete, omitted, history_mode)
        except RecoveryImportError:
            raise
        except Exception as exc:
            code = getattr(exc, 'code', None)
            if code in {'NOT_FOUND', 'STALE_PROFILE', 'CLOSED', 'INVALID_DESTINATION'}:
                raise RecoveryImportError(code) from None
            raise RecoveryImportError('IMPORT_FAILED') from None

    def _import(self, host, candidate, name, previous, complete, omitted, history_mode='namespace-rekey'):
        bundle = core.checked_path(candidate['bundle_root'])
        source = core.checked_path(candidate['profile_root'])
        if source not in (bundle, bundle / 'profile') or not source.is_dir():
            raise RecoveryImportError('INVALID_CANDIDATE')
        if (not self.service.is_managed_result(bundle) and any(
                bundle == p or bundle.is_relative_to(p) or p.is_relative_to(bundle)
                for p in self.service.protected)):
            raise RecoveryImportError('INVALID_CANDIDATE')
        target = core.checked_path(host.get_profile_dir(name))
        if history_mode == 'new-copy' and self.host is None:
            from hermes_constants import get_default_hermes_root
            root = core.checked_path(get_default_hermes_root())
            if target != root / 'profiles' / name:
                raise RecoveryImportError('HISTORY_TARGET_MISMATCH')
        if target.exists():
            raise RecoveryImportError('PROFILE_EXISTS')
        if target == source or target.is_relative_to(source) or source.is_relative_to(target):
            raise RecoveryImportError('INVALID_CANDIDATE')
        private = core.private_dir(self.service.profile / 'cache/finix/import')
        core.validate_sqlite_temp_location(private, self.service.profile)
        operation = uuid.uuid4().hex
        stage = core.private_dir(private / operation)
        metadata = private / (operation + '.json')
        record = dict(version=1, name=name, previous_profile=previous,
                      previous_connection='local', complete=complete, omitted_count=omitted,
                      parked=True, activated=False, status='preparing',
                      rollback='ensureAgent_previous_route_after_explicit_confirmation')
        expected = {}
        try:
            with ExitStack() as stack:
                stack.enter_context(self.service.managed_result_guard(bundle))
                for parent in [self.service.profile, self.service.profile / 'cache',
                               self.service.profile / 'cache/finix', private, stage]:
                    stack.enter_context(core.locked(parent))
                members = list(_members(source, stack))
                relatives = [relative.casefold() for relative, path in members]
                if len(set(relatives)) != len(relatives) or any(
                        relative.split('/')[0].casefold() == PAUSED for relative, path in members):
                    raise RecoveryImportError('INVALID_CANDIDATE')
                config_paths = [path for relative, path in members if relative.casefold() == 'config.yaml']
                if len(config_paths) != 1:
                    raise RecoveryImportError('CONFIG_NOT_READY')
                config = _safe_config(_read(config_paths[0], MAX_CONFIG))
                history = [(relative, path) for relative, path in members if relative.casefold() == 'state.db']
                portable_plan = None
                if history and history_mode == 'new-copy':
                    if len(history) != 1 or history[0][0] != 'state.db' or any(
                            relative.casefold() in {'state.db-wal', 'state.db-shm', 'state.db-journal'} for relative, path in members):
                        raise RecoveryImportError('HISTORY_OWNERSHIP_UNSUPPORTED')
                    from .portable_history import prepare
                    portable_stage = core.private_dir(stage / 'portable')
                    portable_plan = prepare(history[0][1], portable_stage,
                        expected_payload=candidate.get('new_copy_payload'))
                    record['history_projection'] = dict(contract=portable_plan['contract'],
                        original=portable_plan['original'], full_acceptance=False,
                        status='awaiting_authenticated_target_import')
                    # Never install original authority tables as the active DB.
                    members = [(relative, path) for relative, path in members if relative != 'state.db']
                    members += [('.finix-paused/history-original/' + dst, portable_stage / src)
                        for dst, src in [('state.db','original.db'),('census.json','census.json'),
                            ('exports.json','exports.json'),('plan.json','plan.json')]]
                elif history:
                    if len(history) != 1 or history[0][0] != 'state.db' or any(
                            relative.casefold() in {'state.db-wal', 'state.db-shm', 'state.db-journal'} for relative, path in members):
                        raise RecoveryImportError('HISTORY_OWNERSHIP_UNSUPPORTED')
                    prepared = stage / 'history.db'
                    stamp = candidate.get('source_profile')
                    payload = candidate.get('history_payload')
                    if stamp is not None and not isinstance(payload, dict):
                        raise RecoveryImportError('HISTORY_SOURCE_BINDING_REQUIRED')
                    proof = _prepare_history(history[0][1], prepared, name, stage,
                        source_binding=stamp['namespace'] if stamp else None,
                        expected_payload=payload if stamp else None)
                    if proof is not None:
                        record['history_ownership'] = proof
                        record['history_source_profile'] = dict(stamp)
                        record['history_payload'] = dict(payload)
                        record['cipher_sha256'] = candidate['cipher_sha256']
                        record['cipher_size'] = candidate['cipher_size']
                        members = [(relative, prepared if relative == 'state.db' else path) for relative, path in members]
                archive_path = stage / 'profile.tar.gz'
                with core.regular_output(archive_path) as fd, os.fdopen(os.dup(fd), 'wb') as out:
                    with tarfile.open(fileobj=out, mode='w:gz', format=tarfile.PAX_FORMAT) as archive:
                        for relative, path in members:
                            policy_name = relative.casefold()
                            parts = PurePosixPath(relative).parts
                            if not core.safe_name(relative) or ':' in relative:
                                raise RecoveryImportError('INVALID_CANDIDATE')
                            destination = relative
                            if parts[0].casefold() in RUNTIME_ROOTS or policy_name in RUNTIME_FILES:
                                if history_mode != 'new-copy':
                                    continue
                                # Preserve these archive FILES durably but inertly;
                                # never execute recovered tools/installs/runtime.
                                destination = PAUSED + '/archive-files/' + relative
                            if parts[0].casefold() in PARKED_ROOTS or policy_name == 'gateway.json':
                                destination = PAUSED + '/' + relative
                            if policy_name == 'config.yaml':
                                _add_bytes(archive, name + '/' + PAUSED + '/config.yaml',
                                           _read(path, MAX_CONFIG), expected)
                                _add_bytes(archive, name + '/config.yaml', config, expected)
                            elif policy_name == '.env':
                                original = _read(path, MAX_CONFIG)
                                filtered = _safe_env(original)
                                # Ordinary credentials remain byte-identical. Only runtime
                                # location/code-injection controls require an inert original.
                                if filtered != original:
                                    _add_bytes(archive, name + '/' + PAUSED + '/.env', original, expected)
                                _add_bytes(archive, name + '/.env', filtered, expected)
                            elif policy_name == 'cron/jobs.json':
                                data = json.loads(_read(path, 4 * 1024 * 1024))
                                if not isinstance(data, dict) or not isinstance(data.get('jobs'), list):
                                    raise RecoveryImportError('INVALID_CONFIG')
                                for job in data['jobs']:
                                    if not isinstance(job, dict):
                                        raise RecoveryImportError('INVALID_CONFIG')
                                    job.update(enabled=False, state='paused', next_run_at=None, paused_at='finix-restore')
                                _add_bytes(archive, name + '/cron/jobs.json', json.dumps(data).encode(), expected)
                            else:
                                with core.regular_input(path) as srcfd:
                                    info = os.fstat(srcfd)
                                    if not stat.S_ISREG(info.st_mode):
                                        raise RecoveryImportError('INVALID_CANDIDATE')
                                    with os.fdopen(os.dup(srcfd), 'rb') as stream:
                                        entry = tarfile.TarInfo(name + '/' + destination)
                                        entry.size, entry.mode = info.st_size, 0o600
                                        # Source is private inert candidate, not the live profile.
                                        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                                        stream.seek(0)
                                        archive.addfile(entry, stream)
                                        expected[destination] = digest
                        _add_bytes(archive, name + '/gateway.parked', b'finix-recovered-review-required\n', expected)
                self._metadata(metadata, record)
                self.service._fence(self.service.profile_id, self.service.activation)
                self.service.is_managed_result(bundle)
                if target.exists():
                    raise RecoveryImportError('PROFILE_EXISTS')
                record['status'] = 'importing'
                self._metadata(metadata, record)
                if self.host is None:
                    actual = _host_import(host, archive_path, name, target, stage)
                else:
                    actual = Path(host.import_profile(str(archive_path), name=name))
                if actual != target:
                    raise RecoveryImportError('HOST_VERIFICATION_FAILED')
                # Read back the exact publication, not just the host return value.
                with ExitStack() as verify:
                    published = dict(_members(target, verify))
                    if set(published) != set(expected):
                        raise RecoveryImportError('HOST_VERIFICATION_FAILED')
                    for relative, path in published.items():
                        with core.regular_input(path) as srcfd, os.fdopen(os.dup(srcfd), 'rb') as stream:
                            if hashlib.file_digest(stream, 'sha256').hexdigest() != expected[relative]:
                                raise RecoveryImportError('HOST_VERIFICATION_FAILED')
                record['status'] = 'imported'
                self._metadata(metadata, record)
            if history_mode == 'new-copy':
                return dict(name=name, can_activate=False, complete=complete,
                    omitted_count=omitted, parked=True, history_pending=bool(portable_plan),
                    full_acceptance=False, contract='sdk-new-content-copy-v1')
            return dict(name=name, can_activate=True, complete=complete, omitted_count=omitted, parked=True)
        except Exception:
            record.update(status='import_failed', target_preserved=target.exists())
            self._metadata(metadata, record)
            # Never delete an ambiguous host publication or any pre-existing profile.
            raise
        finally:
            shutil.rmtree(stage)

    @staticmethod
    def _metadata(path, record):
        core.atomic_json(path, record)


def _publish_profile(source, target):
    """Same-filesystem atomic no-replace, never shutil.move's nested adoption.

    This is a Finix-owned primitive, not an SDK importer adapter. The public
    importer cannot accept it as a capability; its unsupported path stays closed.
    """
    source, target = core.checked_path(source), core.checked_path(target)
    with core.locked(target.parent), core.locked(source):
        if target.exists() or not (source / 'gateway.parked').is_file():
            raise RecoveryImportError('PROFILE_EXISTS')
        # Windows source no-delete-share handle must be released before rename.
    with core.locked(target.parent):
        core.publish_directory(source, target)
    return str(target)


def _host_import(host, archive, name, target, stage):
    """No public SDK import currently satisfies the protected publication contract.

    A separate ``hermes profile import <archive> --name <name>`` process isolates
    Python state, but its public implementation still uses shutil.move: an
    existing-directory race adopts the source, and EXDEV can expose a partial
    copy. It accepts no native directory-owner/lease or rollback capability.
    Do not dispatch it, replace its filesystem primitives, or alter temp globals.
    """
    raise RecoveryImportError('HOST_IMPORT_CONTRACT_UNSUPPORTED')


def verify_host_compatibility(host):
    """A source hash cannot manufacture a missing public publication capability.

    Used by pipeline admission as well as activation: fail before key dispatch,
    target startup, or any serving/selection acknowledgement can be issued.
    """
    raise RecoveryImportError('HOST_IMPORT_CONTRACT_UNSUPPORTED')


def _host_worker(archive, name, target, stage):
    """Reject the retired child protocol before SDK imports or any side effects."""
    raise RecoveryImportError('HOST_IMPORT_CONTRACT_UNSUPPORTED')


if __name__ == '__main__':
    if len(sys.argv) != 6 or sys.argv[1] != '--host-import':
        sys.exit(2)
    try:
        _host_worker(*sys.argv[2:])
    except RecoveryImportError as exc:
        sys.exit(HOST_EXIT_CODES.get(exc.code, 1))
    except Exception:
        # Never emit candidate/config/credential paths or raw exception text.
        sys.exit(1)
