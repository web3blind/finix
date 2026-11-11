"""Real local age round trips; never invoke cloud tools or live profiles."""
import hashlib
import io
import json

from pathlib import Path
import tarfile
import tempfile
import unittest

from finix import core, streaming


class EntryCountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def test_discovery_visits_more_than_ten_thousand_directories(self):
        source = core.private_dir(self.base / 'source')
        expected = {f'd{index:05d}' for index in range(10001)}
        for name in expected:
            (source / name).mkdir()
        visited = set()
        with core.locked(source) as anchor:
            inventory = core.scan(anchor, lambda fd, prefix: visited.add(prefix))
        self.assertEqual(inventory, {})  # Empty directories are not payloads.
        self.assertEqual(visited, expected | {''})

    def test_directory_depth_remains_bounded(self):
        source = core.private_dir(self.base / 'source')
        nested = source
        for _ in range(33):
            nested = nested / 'd'
            nested.mkdir()
        with core.locked(source) as anchor:
            with self.assertRaisesRegex(core.FinixError, 'Unsupported source path|depth'):
                core.scan(anchor)

    def test_real_age_backup_restore_more_than_ten_thousand_files(self):
        source = core.private_dir(self.base / 'source')
        # Include installation/tool roots, not a narrower source selection.
        for folder in ('tools', 'installs'):
            (source / folder).mkdir()
        expected = {f'{("tools" if index % 2 else "installs")}/f{index:05d}':
                    bytes([index % 251]) for index in range(10001)}
        for name, data in expected.items():
            (source / name).write_bytes(data)
        identity = self.base / 'identity'
        recipient = core.keygen(identity)
        result = core.backup(source, self.base / 'output', self.base / 'state', recipient)
        ciphertext = Path(result['path']).read_bytes()
        self.assertTrue(ciphertext.startswith(b'age-encryption.org/v1'))
        restored = self.base / 'restored'
        core.restore(result['path'], identity, restored)
        actual = {p.relative_to(restored).as_posix(): p.read_bytes()
                  for p in restored.rglob('*') if p.is_file()}
        self.assertEqual(len(actual), 10001)
        self.assertEqual(actual, expected)
        plaintext = core.run_age(['-d', '-i', str(identity)], ciphertext)
        self.assertEqual(core.validate_archive(plaintext), expected)

    def test_real_age_bundle_preserves_all_roots_above_old_total_count(self):
        profile = core.private_dir(self.base / 'profile')
        extra = core.private_dir(self.base / 'extra')
        (profile / 'config.yaml').write_bytes(b'profile')
        expected = {'profile/config.yaml': b'profile'}
        # Each root is below the former cap; their combined manifest is not.
        for index in range(10001):
            root = profile if index % 2 else extra
            name = f'f{index:05d}'
            (root / name).write_bytes(b'x')
            expected[('profile/' if root == profile else 'external/projects/') + name] = b'x'
        identity = self.base / 'identity'
        recipient = core.keygen(identity)
        encrypted = self.base / 'bundle.age'
        with core.locked(profile) as main, core.locked(extra) as other, encrypted.open('wb') as output:
            streaming.write_encrypted_snapshot(main, recipient, output,
                extra_fds=[({'name': 'projects', 'path': str(extra)}, other)])
        restored = self.base / 'restored'
        result = core.restore(encrypted, identity, restored)
        self.assertEqual(result['layout'], 'bundle')
        actual = {p.relative_to(restored).as_posix(): p.read_bytes()
                  for p in restored.rglob('*') if p.is_file()}
        self.assertEqual(len(actual), 10002)
        self.assertEqual(actual, expected)
        plaintext = core.run_age(['-d', '-i', str(identity)], encrypted.read_bytes())
        self.assertEqual(core.validate_archive(plaintext), expected)

    def test_real_age_legacy_restore_more_than_ten_thousand_files(self):
        expected = {f'skills/f{index:05d}': b'x' for index in range(10001)}
        records = {name: {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                   for name, data in expected.items()}
        manifest = {'format': 'finix-1', 'files': records}
        compressed = io.BytesIO()
        with tarfile.open(fileobj=compressed, mode='w:gz') as archive:
            for name, data in [('manifest.json', json.dumps(manifest).encode()), *expected.items()]:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        identity = self.base / 'identity'
        recipient = core.keygen(identity)
        encrypted = self.base / 'legacy.age'
        encrypted.write_bytes(core.run_age(['-r', recipient], compressed.getvalue()))
        restored = self.base / 'restored'
        core.restore(encrypted, identity, restored)
        actual = {p.relative_to(restored).as_posix(): p.read_bytes()
                  for p in restored.rglob('*') if p.is_file()}
        self.assertEqual(len(actual), 10001)
        self.assertEqual(actual, expected)
        self.assertEqual(core.validate_archive(compressed.getvalue()), expected)


if __name__ == '__main__':
    unittest.main()
