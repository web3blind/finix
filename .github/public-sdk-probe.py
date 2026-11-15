"""Actual released-SDK namespace probe on an isolated Linux CI home."""
import argparse
import os
from pathlib import Path
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--project', type=Path, required=True)
args = parser.parse_args()
sys.path.insert(0, str(args.project))

from hermes_constants import get_default_hermes_root, named_profile_home
from finix.streaming import _source_namespace

if os.name != 'posix':
    raise SystemExit('This probe exercises native POSIX descriptors, not Windows acceptance')
root = get_default_hermes_root()
named = named_profile_home(root / 'profiles' / 'finix-ci')
if named is None:
    raise RuntimeError('Public SDK did not recognize its own created named profile')
for home, expected in ((root, 'default'), (named, 'finix-ci')):
    fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY)
    try:
        actual = _source_namespace(fd)
    finally:
        os.close(fd)
    if actual != expected:
        raise AssertionError(f'Public SDK namespace mismatch: {actual!r} != {expected!r}')
    print('RELEASED_SDK_NAMESPACE_OK', expected)
