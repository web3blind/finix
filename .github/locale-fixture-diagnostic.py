"""Nonsecret synthetic CI fixture check for the native locale read path."""
from pathlib import Path
import tempfile
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from finix import core
from finix.extra_sources import read_config

for private in (False, True):
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / 'profile'
        if private:
            home = core.private_dir(home)
        else:
            home.mkdir()
        (home / 'config.yaml').write_text('display:\n  language: ru\n', encoding='utf-8')
        try:
            doc, _ = read_config(home)
            print('private_fixture', private, 'locale', doc['display']['language'])
        except Exception as exc:
            print('private_fixture', private, 'reader_exception', type(exc).__name__, str(exc))
