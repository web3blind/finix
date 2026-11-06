"""Ciphertext-only, bounded-memory staging shared by cloud transports."""
import hashlib
import os
from pathlib import Path
import stat

from . import core

CHUNK = 1024 * 1024


def describe(path, *, native=False):
    path = core.checked_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    digest, size = hashlib.sha256(), 0
    with os.fdopen(fd, 'rb') as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
            raise core.FinixError('Expected nonempty regular ciphertext file')
        first = True
        while chunk := source.read(CHUNK):
            if first and native and not chunk.startswith(b'age-encryption.org/v1\n'):
                raise core.FinixError('Only native age ciphertext may be uploaded')
            first = False
            size += len(chunk)
            if size > before.st_size:
                raise core.FinixError('Ciphertext changed during read')
            digest.update(chunk)
        after = os.fstat(source.fileno())
    if (size != before.st_size or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
        raise core.FinixError('Ciphertext changed during read')
    return {'size': size, 'sha256': digest.hexdigest()}


def copy(source, target, expected=None):
    source, target = core.checked_path(source), core.checked_path(target)
    expected = expected or describe(source)
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(source_fd).st_mode):
            raise core.FinixError('Expected regular ciphertext file')
        target_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(target_fd, 'wb') as output:
                remaining = expected['size']
                while remaining:
                    chunk = os.read(source_fd, min(CHUNK, remaining))
                    if not chunk:
                        raise core.FinixError('Short ciphertext file')
                    output.write(chunk)
                    remaining -= len(chunk)
                if os.read(source_fd, 1):
                    raise core.FinixError('Ciphertext exceeds declared size')
                output.flush()
                os.fsync(output.fileno())
            if describe(target) != expected:
                raise core.FinixError('Ciphertext readback mismatch')
        except BaseException:
            target.unlink(missing_ok=True)
            raise
    finally:
        os.close(source_fd)
    return expected


def verified(path, expected, output=None, *, native=False):
    actual = describe(path, native=native)
    if actual != {key: expected[key] for key in ('size', 'sha256')}:
        raise core.FinixError('Ciphertext readback mismatch; retention not run')
    if output is not None:
        copy(path, output, actual)
    return dict(actual, verified=True)
