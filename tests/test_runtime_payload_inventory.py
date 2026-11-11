"""Independent first-party runtime census: package AND native payload must match."""
import json
from pathlib import Path
import tomllib
import unittest

class RuntimeInventoryTests(unittest.TestCase):
    def test_every_runtime_module_has_allowlist_and_explicit_native_data_entry(self):
        root=Path(__file__).resolve().parents[1]
        actual={p.relative_to(root).as_posix() for p in (root/'finix').rglob('*.py')}
        allowlist=set(json.loads((root/'scripts/release/allowlist.json').read_text()))
        config=tomllib.loads((root/'pyproject.toml').read_text())
        native=set(config['tool']['setuptools']['data-files']['share/finix/finix'])
        intentional_exclusions=set()  # Named explicitly here if ever needed.
        with self.subTest(projection='release-allowlist'):
            self.assertEqual(actual-intentional_exclusions,{p for p in allowlist if p.startswith('finix/') and p.endswith('.py')})
        with self.subTest(projection='native-data-files'):
            self.assertEqual(actual-intentional_exclusions,native)
        self.assertEqual(len(native),len(config['tool']['setuptools']['data-files']['share/finix/finix']))

if __name__=='__main__':unittest.main()
