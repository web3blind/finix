"""Explicit owner-selected external directories; YAML is the sole authority."""
import os
from pathlib import Path, PureWindowsPath
import re
import tempfile

from . import core

ALIAS = re.compile(r'[a-z][a-z0-9-]{0,47}')


def valid_alias(value):
    return isinstance(value, str) and bool(ALIAS.fullmatch(value)) and value not in {'profile', 'external'}


def trusted_root():
    # Capture at registration/construction, never in a later worker/turn.
    try:
        from hermes_constants import get_default_hermes_root
        return core.checked_path(get_default_hermes_root())
    except ImportError:
        return None


def require_default(home, default_root):
    if default_root is None or home != default_root:
        raise core.FinixError('Extra folders require the canonical default profile')
    from hermes_constants import named_profile_home
    if named_profile_home(home) is not None:
        raise core.FinixError('Named profiles cannot include extra folders')


def overlap(a, b):
    return a == b or a in b.parents or b in a.parents


def validate(home, sources, default_root, forbidden=()):
    if not isinstance(sources, list) or len(sources) > 32:
        raise core.FinixError('Expected at most 32 extra source records')
    if not sources:
        return []
    require_default(home, default_root)
    denied = [home, *map(Path, forbidden)]
    denied.extend(Path(p) for p in ('/', '/proc', '/sys', '/dev', '/etc', '/usr', '/bin', '/sbin', '/boot', '/run'))
    result, aliases = [], set()
    for item in sources:
        if (not isinstance(item, dict) or set(item) != {'name', 'path'}
                or not valid_alias(item['name']) or item['name'] in aliases
                or not isinstance(item['path'], str) or not Path(item['path']).is_absolute()):
            raise core.FinixError('Invalid extra source alias or absolute path')
        path = core.checked_path(item['path'])
        if str(path) != item['path'] or not path.is_dir() or not os.access(path, os.R_OK | os.X_OK):
            raise core.FinixError('Extra source must be an existing readable canonical directory')
        # Root itself is denied, not all of its descendants.
        from hermes_constants import named_profile_home
        if (named_profile_home(path) is not None or (path / 'profiles').is_dir()
                or path == Path('/') or any(overlap(path, p) for p in denied if p != Path('/'))):
            raise core.FinixError('Extra source overlaps a protected directory')
        if any(overlap(path, Path(p['path'])) for p in result):
            raise core.FinixError('Extra source directories must be disjoint')
        aliases.add(item['name'])
        result.append({'name': item['name'], 'path': str(path)})
    return result


def read_config(home):
    path = home / 'config.yaml'
    if not path.exists():
        return {}, None
    try:
        import yaml
    except ImportError:
        raise core.FinixError('Hermes YAML support required for configured extra folders') from None
    try:
        with core.regular_input(core.checked_path(path)) as fd:
            before = core.fingerprint(os.fstat(fd))
            data = yaml.safe_load(core.read_regular(fd, 8 * 1024 * 1024))
    except yaml.YAMLError:
        raise core.FinixError('Invalid owning-profile configuration') from None
    # Existing CLI snapshots can contain arbitrary non-Hermes config data.
    return data if isinstance(data, dict) else {}, before


def records(config):
    current = config
    for key in ('plugins', 'entries', 'finix', 'settings'):
        current = current.get(key, {})
        if not isinstance(current, dict):
            raise core.FinixError('Invalid plugin settings')
    value = current.get('extra_sources')
    return [] if value is None else value


def load(home, default_root, forbidden=()):
    config, _ = read_config(home)
    return validate(home, records(config), default_root, forbidden)


def edit(profile, action, name=None, path=None):
    require_default(profile.home, profile.default_root)
    with core.locked(profile.home):
        config, before = read_config(profile.home)
        sources = records(config)
        if action == 'list':
            return {'extra_sources': sources, 'default_profile': True}
        if action == 'add':
            sources = [*sources, {'name': name, 'path': path}]
        elif action == 'remove' and valid_alias(name):
            sources = [item for item in sources if item.get('name') != name]
        else:
            raise core.FinixError('Expected extra list, add NAME PATH, or remove NAME')
        cfg = profile.config()
        from .streaming import _temp_directory
        temp_parent = _temp_directory(cfg.get('sqlite_temp_dir'))
        sources = validate(profile.home, sources, profile.default_root,
                           (profile.root.parent,))
        if any(temp_parent == Path(item['path']) or Path(item['path']) in temp_parent.parents for item in sources):
            raise core.FinixError('SQLite temporary storage overlaps an extra source')
        node = config
        for key in ('plugins', 'entries', 'finix', 'settings'):
            node = node.setdefault(key, {})
            if not isinstance(node, dict):
                raise core.FinixError('Invalid plugin settings')
        node['extra_sources'] = sources
        import yaml
        target = core.checked_path(profile.home / 'config.yaml')
        fd, temp = tempfile.mkstemp(prefix='.finix-config-', dir=profile.home)
        try:
            with os.fdopen(fd, 'w') as stream:
                yaml.safe_dump(config, stream, sort_keys=False)
                stream.flush(); os.fsync(stream.fileno())
            if (core.fingerprint(target.stat()) if target.exists() else None) != before:
                raise core.FinixError('Configuration changed; reload before editing')
            os.replace(temp, target)
        finally:
            Path(temp).unlink(missing_ok=True)
    return {'extra_sources': sources, 'default_profile': True}


def guard_tree(fd, relative, *, root_name):
    """Check anchored directories before exclusions can hide a foreign profiles tree."""
    if os.name == 'nt':
        from .windows_native import guard_tree as native_guard
        return native_guard(fd, relative, root_name=root_name)
    names = set(os.listdir(fd))
    if 'profiles' not in names:
        return
    leaf = Path(relative).name if relative else root_name
    # These are the marker names used by Hermes to recognize a profiles root.
    recognized = leaf == '.hermes' or bool(names & {'config.yaml', '.env', 'state.db'})
    if not recognized:
        import stat
        try:
            profiles = os.open('profiles', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        except (NotADirectoryError, FileNotFoundError):
            return
        try:
            try:
                marker = os.stat('.deleted', dir_fd=profiles, follow_symlinks=False)
            except FileNotFoundError:
                return
            recognized = stat.S_ISDIR(marker.st_mode)
        finally:
            os.close(profiles)
    if recognized:
        raise core.FinixError('Extra source contains another Hermes profile tree')


def admit_bundle(name, aliases):
    if not core.safe_name(name):
        return False
    parts = name.split('/')
    if parts[0] == 'profile':
        return len(parts) > 1 and core.allowed('/'.join(parts[1:]))
    return (len(parts) > 2 and parts[0] == 'external' and parts[1] in aliases
            and valid_alias(parts[1]) and core.allowed('/'.join(parts[2:])))


def manifest_aliases(manifest):
    sources = manifest.get('extra_sources')
    if not isinstance(sources, list) or not 1 <= len(sources) <= 32:
        raise core.FinixError('Invalid bundle sources')
    aliases = set()
    for source in sources:
        if (not isinstance(source, dict) or set(source) != {'name', 'path'}
                or not valid_alias(source['name']) or source['name'] in aliases
                or not isinstance(source['path'], str)
                or not (source['path'].startswith('/') or PureWindowsPath(source['path']).is_absolute())):
            raise core.FinixError('Invalid bundle source metadata')
        aliases.add(source['name'])
    return aliases
