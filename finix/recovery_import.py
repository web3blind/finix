"""Inert archive sanitization helpers; automatic host publication is retired.

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
    'shell-hooks-allowlist.json', 'active_profile', 'cron/.tick.lock', 'kanban/.dispatcher.lock',
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


def profile_digest(target, retained=None):
    from contextlib import nullcontext
    with (ExitStack() if retained is None else nullcontext(retained)) as stack:
        result = {}
        from .preview_budget import check
        for name, path in _members(target, stack):
            check()
            with core.regular_input(path) as fd, os.fdopen(os.dup(fd), 'rb') as stream:
                digest = hashlib.sha256()
                while chunk := stream.read(1024 * 1024):
                    check()
                    digest.update(chunk)
                result[name] = digest.hexdigest()
        return result
