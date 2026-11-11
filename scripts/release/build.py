"""Build candidate assets and wheel in a new isolated work tree, then assemble."""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from assemble import select, sha, scan, check_license, normalize_wheel


def run(argv, cwd, env):
    subprocess.run([str(a) for a in argv], cwd=cwd, env=env, check=True, timeout=300)


def materialize(reviewed, assets, notices, tree):
    """Write a new public snapshot; never write through source links or hardlinks."""
    entries = dict(reviewed)
    entries.update(assets)
    entries['THIRD-PARTY-NOTICES.md'] = notices
    for name, data in entries.items():
        scan(name, data)
        target = tree / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as out:
            out.write(data)
    return entries


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--work', type=Path, required=True)
    p.add_argument('--node', type=Path, required=True)
    p.add_argument('--npm-cli', type=Path, required=True)
    p.add_argument('--offline-npm', action='store_true')
    p.add_argument('--npm-cache', type=Path, help='Optional absolute isolated cache for an offline rebuild')
    args = p.parse_args()
    if args.npm_cache and not args.npm_cache.is_absolute():
        p.error('--npm-cache must be absolute')
    if sys.prefix == sys.base_prefix:
        p.error('Run only from an isolated Python venv')
    for path in (args.source, args.work, args.node, args.npm_cli):
        if not path.is_absolute():
            p.error('All paths must be absolute')
    if args.work == args.source or args.source in args.work.parents:
        p.error('Work must be outside the source tree')
    names = json.loads(Path(__file__).with_name('allowlist.json').read_text())
    reviewed = select(args.source, names)
    check_license(reviewed)
    args.work.mkdir(parents=True, exist_ok=False)
    npm_tree = args.work / 'npm'
    npm_tree.mkdir()
    env = {k: v for k, v in os.environ.items() if k.upper() in
           {'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'PATH'}}
    env.update(HOME=str(args.work), USERPROFILE=str(args.work),
               TMPDIR=str(args.work), TEMP=str(args.work), TMP=str(args.work),
               npm_config_cache=str(args.npm_cache or (args.work / 'npm-cache')), npm_config_audit='false',
               npm_config_fund='false', SOURCE_DATE_EPOCH='315532800', PYTHONHASHSEED='0')
    env['PATH'] = str(args.node.parent) + os.pathsep + env.get('PATH', '')
    build_names = [n for n in names if n.startswith(('web/', 'desktop/src/')) or
                   n in ('package.json', 'package-lock.json', 'scripts/build-web.mjs', 'scripts/build-desktop.mjs')]
    for n in build_names:
        data = reviewed[n]
        dest = npm_tree / n
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    version = subprocess.check_output([str(args.node), '--version'], env=env, text=True).strip()
    if not version.startswith(('v22.', 'v23.', 'v24.')):
        p.error('package.json requires Node >=22 <25')
    npm_version = subprocess.check_output([str(args.node), str(args.npm_cli), '--version'], env=env, text=True).strip()
    command = [args.node, args.npm_cli, 'ci', '--ignore-scripts', '--no-audit', '--no-fund']
    if args.offline_npm:
        command.append('--offline')
    run(command, npm_tree, env)  # npm verifies package-lock integrity, no lifecycle scripts
    assets = {}
    for attempt in range(2):
        for script in ('scripts/build-desktop.mjs', 'scripts/build-web.mjs'):
            run([args.node, script], npm_tree, env)
        observed = {n: (npm_tree / n).read_bytes() for n in ('desktop/plugin.js', 'dist/keygen.html')}
        if attempt and observed != assets:
            raise ValueError('Asset rebuild is not deterministic')
        assets = observed
        if not attempt:
            for n in assets:
                (npm_tree / n).unlink()
    notices = ['# Third-party notices\n\nFinix is licensed under the author-approved MIT LICENSE. The following notices cover third-party components and must be preserved.\n\nThe generated bundles also embed the complete notices of their bundled dependencies.\n']
    lock = json.loads((npm_tree / 'package-lock.json').read_text())
    dependencies = {}
    for name, info in sorted(lock['packages'].items()):
        if not name or not (npm_tree / name).is_dir():
            continue
        package = npm_tree / name
        metadata = json.loads((package / 'package.json').read_text())
        licenses = [x for x in package.iterdir() if x.is_file() and x.name.upper().startswith(('LICENSE', 'COPYING'))]
        if not licenses and metadata['name'].startswith('@esbuild/') and metadata.get('license') == 'MIT':
            # Platform binary packages declare MIT but share the parent project's notice.
            parent = npm_tree / 'node_modules/esbuild'
            parent_metadata = json.loads((parent / 'package.json').read_text())
            if parent_metadata['version'] != metadata['version'] or parent_metadata.get('license') != 'MIT':
                raise ValueError('esbuild platform notice version/license mismatch')
            licenses = [parent / 'LICENSE.md']
        if not licenses or not all(x.is_file() for x in licenses):
            raise ValueError('Dependency notice missing: ' + name)
        dependencies[name] = {'version': metadata['version'], 'integrity': info.get('integrity'),
                              'resolved': info.get('resolved')}
        notices.append('\n## ' + metadata['name'] + ' ' + metadata['version'] + '\n\n' + '\n'.join(x.read_text(encoding='utf-8') for x in sorted(licenses)))
    dist = importlib.metadata.distribution('PyYAML')
    yaml_license = [f for f in dist.files if 'license' in str(f).lower() and str(f).lower().endswith(('license', 'license.txt'))]
    if not yaml_license:
        raise ValueError('PyYAML license metadata missing')
    notices.append('\n## PyYAML ' + dist.version + ' (external Python runtime dependency, not bundled)\n\n' + '\n'.join(dist.locate_file(f).read_text(encoding='utf-8') for f in yaml_license))
    tree = args.work / 'public-tree'
    entries = materialize(reviewed, assets, ('\n'.join(notices) + '\n').encode('utf-8'), tree)
    wheel_dir = args.work / 'wheel'
    run([sys.executable, '-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation', '--no-index', str(tree), '-w', str(wheel_dir)], args.work, env)
    wheel = next(wheel_dir.glob('*.whl'))
    normalize_wheel(wheel)  # Windows setuptools emits writable POSIX ZIP modes.
    tools = {name: importlib.metadata.version(name) for name in ('pip', 'setuptools', 'wheel', 'PyYAML', 'packaging')}
    provenance = {'schema': 1, 'platform': platform.system(), 'architecture': platform.machine(),
                  'python': platform.python_version(), 'tools': tools, 'node': version, 'npm': npm_version,
                  'source_date_epoch': env['SOURCE_DATE_EPOCH'], 'npm_ci_ignore_scripts': True,
                  'npm_dependencies': dependencies, 'assets_verified': True,
                  'assets': {n: sha(d) for n, d in assets.items()},
                  'inputs': {n: sha(d) for n, d in entries.items() if n not in assets},
                  'wheel': {'name': wheel.name, 'sha256': sha(wheel.read_bytes())},
                  'scope': 'Candidate build only; no OS encryption, live provider, Desktop activation or publication acceptance'}
    provenance_path = args.work / 'provenance.json'
    provenance_path.write_text(json.dumps(provenance, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    run([sys.executable, Path(__file__).with_name('assemble.py'), '--source', tree,
         '--output', args.work / 'artifacts', '--provenance', provenance_path, '--wheel', wheel], args.work, env)
    print('MIT candidate artifacts assembled. PUBLICATION BLOCKED pending explicit post-recording user approval.')

if __name__ == '__main__':
    main()
