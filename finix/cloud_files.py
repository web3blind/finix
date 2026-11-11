"""Ciphertext-only, bounded-memory staging shared by cloud transports."""
import hashlib
import os
from pathlib import Path
import stat

from . import core
from .streaming import check_cancel

CHUNK = 1024 * 1024


def completion_record(descriptor, completion):
    """Bind producer completeness to the exact ciphertext, never its filename."""
    if completion is None:
        return None  # Unknown legacy provenance must never authorize deletion.
    if (not isinstance(completion, dict)
            or completion.get('sha256') != descriptor['sha256']
            or type(completion.get('complete')) is not bool
            or type(completion.get('omitted_count')) is not int
            or completion['omitted_count'] < 0
            or completion['complete'] != (completion['omitted_count'] == 0)):
        raise core.FinixError('Invalid ciphertext completeness provenance')
    return {key: completion[key] for key in ('sha256', 'complete', 'omitted_count')}


def known_completion(row):
    try:
        return completion_record(row, row.get('completion'))
    except (core.FinixError, KeyError, TypeError):
        return None


def retention_candidates(records, keep):
    if type(keep) is not int or not 1 <= keep <= 1000:
        raise core.FinixError('Invalid retention')
    latest = known_completion(records[-1]) if records else None
    if not latest or not latest['complete']:
        return []
    return [row for row in records[:-keep] if known_completion(row) is not None]



def report(progress, phase, completed, total, *, done=False):
    """Only phase-local, path-free counters; unfinished work never reports 100."""
    if progress is not None:
        progress(dict(phase=phase, completed_bytes=completed, total_bytes=total,
                      percent=100 if done else min(99, completed * 100 // total)))


def describe(path, *, native=False, progress=None, cancel=None, phase='verify'):
    check_cancel(cancel)
    path = core.checked_path(path)
    digest, size = hashlib.sha256(), 0
    with core.regular_input(path) as fd, os.fdopen(os.dup(fd), 'rb') as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
            raise core.FinixError('Expected nonempty regular ciphertext file')
        report(progress, phase, 0, before.st_size)
        first = True
        while True:
            check_cancel(cancel)
            chunk = source.read(CHUNK)
            if not chunk:
                break
            if first and native and not chunk.startswith(b'age-encryption.org/v1\n'):
                raise core.FinixError('Only native age ciphertext may be uploaded')
            first = False
            size += len(chunk)
            if size > before.st_size:
                raise core.FinixError('Ciphertext changed during read')
            digest.update(chunk)
            report(progress, phase, size, before.st_size)
        after = os.fstat(source.fileno())
    if (size != before.st_size or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
        raise core.FinixError('Ciphertext changed during read')
    check_cancel(cancel)
    report(progress, phase, size, size, done=True)
    return {'size': size, 'sha256': digest.hexdigest()}


def copy(source, target, expected=None, *, progress=None, cancel=None, phase='verify'):
    check_cancel(cancel)
    source, target = core.checked_path(source), core.checked_path(target)
    expected = expected or describe(source, cancel=cancel)
    with core.regular_input(source) as source_fd:
        if not stat.S_ISREG(os.fstat(source_fd).st_mode):
            raise core.FinixError('Expected regular ciphertext file')
        created = False
        try:
            with core.regular_output(target) as target_fd, os.fdopen(os.dup(target_fd), 'wb') as output:
                created = True
                remaining = expected['size']
                report(progress, phase, 0, remaining)
                while remaining:
                    check_cancel(cancel)
                    chunk = os.read(source_fd, min(CHUNK, remaining))
                    if not chunk:
                        raise core.FinixError('Short ciphertext file')
                    output.write(chunk)
                    remaining -= len(chunk)
                    report(progress, phase, expected['size'] - remaining, expected['size'])
                check_cancel(cancel)
                if os.read(source_fd, 1):
                    raise core.FinixError('Ciphertext exceeds declared size')
                output.flush()
                os.fsync(output.fileno())
            if describe(target, cancel=cancel) != expected:
                raise core.FinixError('Ciphertext readback mismatch')
            check_cancel(cancel)
            report(progress, phase, expected['size'], expected['size'], done=True)
        except BaseException:
            if created:
                target.unlink(missing_ok=True)
            raise
    return expected


def verified(path, expected, output=None, *, native=False, progress=None, cancel=None, phase='verify'):
    def hashing(event):
        # The hash must match the expected descriptor before phase completion.
        if progress is not None:
            progress(dict(event, percent=min(99, event['percent'])))
    actual = describe(path, native=native, progress=hashing if progress is not None else None,
                      cancel=cancel, phase=phase)
    if actual != {key: expected[key] for key in ('size', 'sha256')}:
        raise core.FinixError('Ciphertext readback mismatch; retention not run')
    if output is not None:
        copy(path, output, actual, cancel=cancel)
    check_cancel(cancel)
    report(progress, phase, actual['size'], actual['size'], done=True)
    return dict(actual, verified=True)
