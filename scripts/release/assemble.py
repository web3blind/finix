"""Fail-closed public candidate assembly. Does not grant publication rights."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tomllib
import zipfile

HERE = Path(__file__).resolve().parent
DENIED = {'.git', '.env', 'node_modules', '__pycache__', '.pytest_cache',
          '.venv', 'cache', '.linux-test-bin', '.linux-test-temp', 'rclone.conf',
          'credentials.json', 'google_token.json', 'token.json'}
PATTERNS = [
    rb'(?i)[A-Z]:[\\/]+Users[\\/]+[a-z][a-z0-9_.-]*[\\/]',
    rb'/(?:home|Users)/[a-z][a-z0-9_.-]*/',
    rb'AGE-SECRET-KEY-1[0-9A-Z]{20,}',
    rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
    rb'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[0-9A-Z]{16})',
    rb'(?i)["\'](?:refresh_token|access_token|client_secret|password)["\']\s*:\s*["\'][^"\'\r\n]{8,}["\']',
]

def unsafe_stat(st):
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, 'st_file_attributes', 0) & 0x400)

def scan(name, data):
    if not isinstance(name, str) or not name or any(ord(c) < 32 or ord(c) > 126 for c in name):
        raise ValueError('Nonportable public path')
    path = PurePosixPath(name)
    parts = name.split('/')
    reserved = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}
    if (any(p in ('', '.', '..') or p.endswith((' ', '.')) or p.split('.')[0].upper() in reserved for p in parts) or
        any(c in name for c in '\\:<>"|?*') or path.is_absolute() or '..' in path.parts or
        any(p.lower() in DENIED or p.startswith('.env.') for p in path.parts) or
        path.suffix.lower() in {'.key', '.age', '.db', '.sqlite', '.log', '.pyc', '.conf'}):
        raise ValueError('Forbidden public path: ' + name)
    for number, pattern in enumerate(PATTERNS):
        if re.search(pattern, data):
            raise ValueError(f'Public content scan failed: {name} rule {number}')

def select(root, allowlist):
    root = Path(root).absolute()
    result = {}
    if not isinstance(allowlist, list) or not all(isinstance(n, str) for n in allowlist):
        raise ValueError('Allowlist must be a list of paths')
    if len({n.casefold() for n in allowlist}) != len(allowlist):
        raise ValueError('Duplicate or case-alias allowlist paths')
    for name in allowlist:
        scan(name, b'')
        path = root / name
        # Check every ancestor, including the source root, without resolving links.
        for parent in [path, *path.parents]:
            if unsafe_stat(parent.lstat()):
                raise ValueError('Link/reparse path rejected: ' + name)
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise ValueError('Not a single-link regular file: ' + name)
        data = path.read_bytes()
        stable = ('st_mode', 'st_dev', 'st_ino', 'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
        after = path.lstat()
        if unsafe_stat(after) or any(getattr(after, key) != getattr(st, key) for key in stable):
            raise ValueError('Source changed: ' + name)
        scan(name, data)
        result[name] = data
    return result

def archive(path, entries):
    with zipfile.ZipFile(path, 'x', compression=zipfile.ZIP_STORED) as z:
        for name, data in sorted(entries.items()):
            scan(name, data)
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            z.writestr(info, data)

def normalize_wheel(path):
    """Normalize ZIP container metadata only; preserve every payload/RECORD byte."""
    entries = {}
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            mode = info.external_attr >> 16
            if stat.S_IFMT(mode) not in (0, stat.S_IFREG) or mode & 0o7111:
                raise ValueError('Wheel contains link, special file or executable mode')
            if info.filename.casefold() in {n.casefold() for n in entries}:
                raise ValueError('Duplicate wheel members')
            data = z.read(info)
            scan(info.filename, data)
            entries[info.filename] = data
    temporary = path.with_suffix('.normalized')
    archive(temporary, entries)
    os.replace(temporary, path)


def inspect_archive(path):
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if len(names) != len({n.casefold() for n in names}):
            raise ValueError('Duplicate archive members')
        for info in z.infolist():
            mode = info.external_attr >> 16
            if stat.S_IFMT(mode) not in (0, stat.S_IFREG) or mode & 0o7022:
                raise ValueError('Archive link, special file or unsafe permissions')
            scan(info.filename, z.read(info))
    return len(names)

def sha(data):
    return hashlib.sha256(data).hexdigest()

def check_license(entries):
    """Require the author's approved MIT grant, not merely an SPDX claim."""
    try:
        # Exact approved text in either Git LF or Windows checkout CRLF form.
        # Accept only these two byte hashes; never rewrite release payload bytes.
        if sha(entries['LICENSE']) not in {
            '9115d8e9c32d890495f56a5f71cd4c26098b4d22811139f867dbca3f63eb7867',
            '8e4add75e585a74ffcf6372f623749f7869ade543e3196f184e2ecb431fe8962',
        }:
            raise ValueError('Approved MIT LICENSE differs')
        project = tomllib.loads(entries['pyproject.toml'].decode())['project']
        package = json.loads(entries['package.json'])
        lock = json.loads(entries['package-lock.json'])
        if (project.get('license') != 'MIT' or project.get('license-files') != ['LICENSE'] or
            package.get('license') != 'MIT' or lock['packages'][''].get('license') != 'MIT'):
            raise ValueError('MIT license metadata missing or inconsistent')
    except (KeyError, TypeError, UnicodeError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError('MIT license inputs missing or malformed') from exc

def check_wheel_license(data):
    from email.parser import BytesParser
    metadata = BytesParser().parsebytes(data, headersonly=True)
    if (metadata.get_all('License-Expression') != ['MIT'] or
        metadata.get_all('License-File') != ['LICENSE']):
        raise ValueError('Wheel MIT license metadata missing or ambiguous')


def native_payload(entries):
    # Match the explicit wheel data-files, including shipped Desktop sources.
    return {n: d for n, d in entries.items()
            if not n.startswith(('web/', 'scripts/'))
            and n not in ('package.json', 'package-lock.json', 'pyproject.toml')}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path)
    p.add_argument('--output', type=Path)
    p.add_argument('--provenance', type=Path)
    p.add_argument('--wheel', type=Path)
    p.add_argument('--publish-check', action='store_true')
    args = p.parse_args()
    if args.publish_check:
        p.error('PUBLICATION BLOCKED: explicit post-recording user approval is required; MIT is approved, publication is not')
    if not all((args.source, args.output, args.provenance, args.wheel)):
        p.error('Assembly requires --source, --output, --provenance and --wheel')
    names = json.loads((HERE / 'allowlist.json').read_text())
    entries = select(args.source, names)
    check_license(entries)
    provenance = json.loads(args.provenance.read_text())
    if provenance.get('assets_verified') is not True:
        raise ValueError('Missing generated-asset rebuild verification')
    for name in ('desktop/plugin.js', 'dist/keygen.html'):
        if provenance['assets'][name] != sha(entries[name]):
            raise ValueError('Generated asset provenance mismatch')
    if provenance['inputs'] != {n: sha(entries[n]) for n in names if n not in ('desktop/plugin.js', 'dist/keygen.html')}:
        raise ValueError('Source changed since rebuild provenance')
    if provenance.get('wheel') != {'name': args.wheel.name, 'sha256': sha(args.wheel.read_bytes())}:
        raise ValueError('Wheel provenance mismatch')
    inspect_archive(args.wheel)
    native = native_payload(entries)
    with zipfile.ZipFile(args.wheel) as z:
        wheel_entries = {n: z.read(n) for n in z.namelist()}
    expected = {n: d for n, d in entries.items() if n.startswith('finix/')}
    expected.update({'finix-0.1.0.data/data/share/finix/' + n: d for n, d in native.items()})
    metadata_names = {'finix-0.1.0.dist-info/' + n for n in ('METADATA', 'WHEEL', 'entry_points.txt', 'top_level.txt', 'RECORD')}
    expected['finix-0.1.0.dist-info/licenses/LICENSE'] = entries['LICENSE']
    metadata = wheel_entries.get('finix-0.1.0.dist-info/METADATA', b'')
    check_wheel_license(metadata)
    if set(wheel_entries) != set(expected) | metadata_names:
        raise ValueError('Wheel inventory differs from explicit public payload')
    if any(wheel_entries[n] != d for n, d in expected.items()):
        raise ValueError('Wheel payload differs from reviewed inputs')
    args.output.mkdir(parents=True, exist_ok=False)
    manifest = {'schema': 1, 'license_status': 'MIT_AUTHOR_APPROVED',
                'publication_status': 'BLOCKED_PENDING_EXPLICIT_POST_RECORDING_USER_APPROVAL',
                'candidate_scope': 'Integrated async-backup candidate; offline Linux/Windows gates only; no installed or live-provider acceptance',
                'provenance': provenance}
    for kind, payload in (('source', dict(entries)), ('native', dict(native))):
        member_manifest = dict(manifest, files={n: sha(d) for n, d in sorted(payload.items())})
        payload['RELEASE-MANIFEST.json'] = (json.dumps(member_manifest, indent=2, sort_keys=True) + '\n').encode()
        target = args.output / ('finix-0.1.0-' + kind + '-candidate.zip')
        archive(target, payload)
        inspect_archive(target)
    wheel_target = args.output / args.wheel.name
    wheel_target.write_bytes(args.wheel.read_bytes())
    outputs = {x.name: sha(x.read_bytes()) for x in sorted(args.output.iterdir())}
    full_manifest = dict(manifest, artifacts=outputs,
                         wheel_members={n: sha(d) for n, d in sorted(wheel_entries.items())})
    (args.output / 'RELEASE-MANIFEST.json').write_text(json.dumps(full_manifest, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    outputs['RELEASE-MANIFEST.json'] = sha((args.output / 'RELEASE-MANIFEST.json').read_bytes())
    (args.output / 'SHA256SUMS').write_text(''.join(f'{v}  {k}\n' for k, v in outputs.items()), encoding='utf-8')
    print(json.dumps(outputs, indent=2))

if __name__ == '__main__':
    main()
