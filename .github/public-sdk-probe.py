"""Actual released-SDK namespace probe on isolated native CI homes."""
import argparse
from contextlib import ExitStack
import os
from pathlib import Path
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--project', type=Path, required=True)
args = parser.parse_args()
sys.path.insert(0, str(args.project))

from hermes_constants import get_default_hermes_root, named_profile_home
from finix.streaming import _source_namespace

root = get_default_hermes_root()
named = named_profile_home(root / 'profiles' / 'finix-ci')
if named is None:
    raise RuntimeError('Public SDK did not recognize its own created named profile')
for home, expected in ((root, 'default'), (named, 'finix-ci')):
    with ExitStack() as stack:
        if os.name == 'nt':
            from finix import windows_native as win
            fd = stack.enter_context(win.directory(home))
        else:
            fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY)
            stack.callback(os.close, fd)
        actual = _source_namespace(fd)
    if actual != expected:
        raise AssertionError(f'Public SDK namespace mismatch: {actual!r} != {expected!r}')
    print('RELEASED_SDK_NAMESPACE_OK', os.name, expected)
