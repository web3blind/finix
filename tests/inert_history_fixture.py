"""Synthetic public-SDK history; no runtime startup or retired-controller cleanup."""
from pathlib import Path
import pytest
from finix import core


@pytest.fixture(autouse=True)
def fenced_root(tmp_path, monkeypatch):
    from hermes_constants import get_default_hermes_root, get_hermes_home
    root = core.private_dir(tmp_path / 'inert-sdk-root')
    monkeypatch.setenv('HERMES_HOME', str(root))
    assert Path(get_default_hermes_root()).resolve() == root.resolve()
    assert Path(get_hermes_home()).resolve() == root.resolve()
    yield root


def seed(_fixture_path):
    from hermes_constants import get_default_hermes_root, named_profile_home
    from hermes_state import SessionDB
    home = core.private_dir(Path(get_default_hermes_root()) / 'profiles' / 'source')
    assert named_profile_home(home) == home
    db = SessionDB(home / 'state.db')
    try:
        for i in range(3):
            db.create_session('history-' + str(i), source='desktop')
            for j in range(4):
                db.append_message('history-' + str(i), role='assistant', content='synthetic ' + str(j))
    finally:
        db.close()
    return home / 'state.db'
