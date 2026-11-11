"""Durable, profile-scoped setup jobs. Polling never takes the operation lock.

Only safe aggregate state is persisted. A separate OS file lease is held for the
entire worker lifetime; neither an HTTP disconnect nor a second process can
launch another worker. Interrupted jobs require reconciliation, not recapture.
"""
from contextlib import contextmanager
import errno
import contextvars
import json
import math
import os
import threading
import time
import uuid

from . import core
from .setup_flow import SetupFlow
from .setup_errors import setup_error_code

VERSION = 'backup-jobs-v1'
_SLOTS = threading.BoundedSemaphore(4)
_PUBLIC = ('id', 'state', 'phase', 'progress', 'started_at', 'updated_at',
           'error_code', 'verified', 'complete', 'omitted_count')


@contextmanager
def file_lease(path):
    """Nonblocking, kernel-released cross-process file lock on both platforms."""
    try:
        with core.regular_output(path) as fd:
            os.write(fd, b'0')
    except FileExistsError:
        pass
    with core.regular_input(path) as fd:
        if os.name == 'nt':
            import msvcrt
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise core.FinixError('JOB_BUSY') from None
                raise
            try:
                yield
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise core.FinixError('JOB_BUSY') from None
            yield


@contextmanager
def state_lease(path):
    from contextlib import ExitStack
    with ExitStack() as stack:
        for attempt in range(100):
            try:
                stack.enter_context(file_lease(path))
                break
            except core.FinixError as exc:
                if str(exc) != 'JOB_BUSY' or attempt == 99: raise
                time.sleep(.005)
        yield


def _busy(path):
    try:
        with file_lease(path):
            return False
    except core.FinixError as exc:
        if str(exc) != 'JOB_BUSY': raise
        return True


def _progress():
    return dict(percent=None, completed_bytes=None, total_bytes=None,
                completed_files=None, total_files=None)


class BackupJobs:
    def __init__(self, flow_factory=SetupFlow):
        self.flow_factory = flow_factory

    def _paths(self, profile):
        root = core.private_dir(profile.root / 'backup-jobs')
        return root, root / 'state.json', root / 'worker.lock'

    def _read(self, profile, path):
        try:
            with core.regular_input(path) as fd:
                state = json.loads(core.read_regular(fd, 256 * 1024))
        except FileNotFoundError:
            return {'profile_id': profile.profile_id, 'job': None, 'requests': {}}
        if state.get('profile_id') != profile.profile_id:
            raise core.FinixError('PROFILE_CHANGED')
        return state

    def _envelope(self, profile, job):
        return {'code_version': VERSION, 'profile_id': profile.profile_id,
                'job': {key: job[key] for key in _PUBLIC} if job else None}

    def _external(self, profile):
        # Metadata/lock probe only: never opens an adapter, spawns rclone, waits,
        # cancels, reconciles, or changes the legacy pending intent.
        try:
            with core.locked(profile.root):
                return None
        except (core.FinixError, OSError) as exc:
            if 'Another Finix operation' not in str(exc): raise
        now = time.time()
        return dict(id='external-' + profile.profile_id, state='running', phase='external',
                    progress=_progress(), started_at=None, updated_at=now,
                    error_code=None, verified=False, complete=None, omitted_count=None)

    def _refresh(self, profile, state, lease):
        job = state['job']
        if job and job['state'] == 'running' and not _busy(lease):
            job.update(state='interrupted', phase='interrupted', error_code='BACKUP_INTERRUPTED',
                       updated_at=time.time(), verified=False)
        return job

    def current(self, profile):
        root, path, lease = self._paths(profile)
        with state_lease(root / 'state.lock'):
            state = self._read(profile, path)
            job = self._refresh(profile, state, lease)
            if not job or job['state'] != 'running':
                external = self._external(profile)
                if external: return self._envelope(profile, external)
            if job: core.atomic_json(path, state)
            return self._envelope(profile, job)

    def _receipt_path(self, root, identifier, kind):
        # Identifiers are generated or canonical UUIDs, never caller paths.
        uuid.UUID(identifier)
        return core.private_dir(root / kind) / (identifier + '.json')

    def _receipt_read(self, root, identifier, kind):
        try:
            with core.regular_input(self._receipt_path(root, identifier, kind)) as fd:
                # Archived jobs retain the entire private checkpoint, not just
                # the small public receipt. Match the current job-state bound.
                limit = 256 * 1024 if kind == 'history' else 16384
                return json.loads(core.read_regular(fd, limit))
        except FileNotFoundError:
            return None

    def _remember(self, root, request_id, job):
        core.atomic_json(self._receipt_path(root, request_id, 'requests'), {'id': job['id']})

    def _archive(self, root, job):
        # Private history retains the owned checkpoint and original binding;
        # public envelopes continue to expose only aggregate terminal fields.
        core.atomic_json(self._receipt_path(root, job['id'], 'history'), job)

    def _transition(self, profile, job, binding):
        try:
            with core.regular_input(profile.root / 'connection-transition.json') as fd:
                value = json.loads(core.read_regular(fd, 65536))
        except FileNotFoundError:
            return False
        return (value.get('version') == 1 and value.get('profile_id') == profile.profile_id
                and value.get('job_id') == job['id']
                and value.get('from_binding') == job.get('binding')
                and value.get('to_binding') == binding)

    def start(self, profile, profile_id, request_id):
        if profile_id != profile.profile_id:
            raise core.FinixError('PROFILE_CHANGED')
        try:
            if str(uuid.UUID(request_id)) != request_id: raise ValueError()
        except (ValueError, AttributeError, TypeError):
            raise core.FinixError('INVALID_REQUEST_ID') from None
        root, path, lease = self._paths(profile)
        with state_lease(root / 'state.lock'):
            state = self._read(profile, path)
            job = self._refresh(profile, state, lease)
            # Private per-request receipts and per-job terminal records keep
            # state reads bounded without expiring idempotency or a lifetime cap.
            # Migrate the previous bounded inline journal under the same lease.
            for key, value in state.get('requests', {}).items():
                self._remember(root, key, value)
                if not job or value['id'] != job['id']: self._archive(root, value)
            state['requests'] = {}
            receipt = self._receipt_read(root, request_id, 'requests')
            seen = receipt is not None
            if receipt and (not job or receipt['id'] != job['id']):
                archived = self._receipt_read(root, receipt['id'], 'history')
                if archived: return self._envelope(profile, archived)
                # Crash before state admission: no committed worker exists.
                seen = False
            external = self._external(profile) if not job or job['state'] != 'running' else None
            if external: return self._envelope(profile, external)
            if job and (job['state'] == 'running' or _busy(lease)
                        or seen and job['state'] != 'interrupted'):
                if not seen: self._remember(root, request_id, job)
                core.atomic_json(path, state)
                return self._envelope(profile, job)
            if not _SLOTS.acquire(blocking=False): raise core.FinixError('JOB_CAPACITY')
            try:
                binding = self.flow_factory(profile)._binding()
                changed = bool(job and job.get('binding') != binding)
                if seen and changed:
                    # A prior request never authorizes work in a new account,
                    # including the special same-account interrupted replay path.
                    core.atomic_json(path, state)
                    _SLOTS.release()
                    return self._envelope(profile, job)
                owned = bool(job and (job['state'] == 'interrupted'
                                      or job['state'] == 'failed' and job.get('capture')))
                if changed and owned and not self._transition(profile, job, binding):
                    raise core.FinixError('BACKUP_BINDING_CHANGED')
                resume = bool(not changed and owned)
            except BaseException:
                _SLOTS.release()
                raise
            ready, proceed = threading.Event(), threading.Event()
            acquired, committed = [], []
            started = False
            def worker():
                try:
                    with file_lease(lease):
                        acquired.append(True)
                        ready.set()
                        proceed.wait()
                        if committed: self._run(profile, job['id'], resume)
                except BaseException:
                    # _run handles execution errors; lease/admission failure is
                    # reconciled by the next read if the process disappears.
                    ready.set()
                finally:
                    _SLOTS.release()
            try:
                # A fresh Workspace continuation intent adopts the private
                # checkpoint, not the previous request's terminal outcome. Keep
                # old receipts byte-identical and replay their archived failure.
                adopt = bool(resume and not seen and job.get('capture')
                             and job['state'] == 'failed'
                             and profile.config().get('storage') == 'workspace')
                checkpoint = job.get('capture') if adopt else None
                if not resume or adopt:
                    if job: self._archive(root, job)
                    now = time.time()
                    job = dict(id=uuid.uuid4().hex, state='running', phase='starting',
                               progress=_progress(), started_at=now, updated_at=now,
                               error_code=None, verified=False, complete=None, omitted_count=None,
                               binding=binding)
                    if adopt:
                        job.update(capture=checkpoint, phase='reconcile')
                else:
                    job.update(state='running', phase='reconcile', error_code=None, updated_at=time.time())
                state['job'] = job
                self._remember(root, request_id, job)
                # Every admitted worker owns a fresh authenticated context. Never
                # mutate process environment or reuse a Context across jobs.
                context = contextvars.copy_context()
                thread = threading.Thread(target=context.run, args=(worker,), name='finix-backup', daemon=True)
                thread.start()
                started = True
                ready.wait(2)
                if not acquired: raise core.FinixError('JOB_BUSY')
                core.atomic_json(path, state)
                committed.append(True)
                return self._envelope(profile, job)
            except BaseException:
                if not started: _SLOTS.release()
                raise
            finally:
                proceed.set()

    def _update(self, profile, identifier, changes):
        root, path, _ = self._paths(profile)
        # Short progress writes may collide with a poll. Skip that event instead
        # of failing an otherwise valid backup; terminal writes retry boundedly.
        for attempt in range(200):
            try:
                with state_lease(root / 'state.lock'):
                    state = self._read(profile, path)
                    if not state['job'] or state['job']['id'] != identifier: return
                    state['job'].update(changes, updated_at=time.time())
                    core.atomic_json(path, state)
                    return
            except core.FinixError as exc:
                if str(exc) != 'JOB_BUSY': raise
                if 'state' not in changes: return
                time.sleep(.01)
        raise core.FinixError('JOB_STATE_BUSY')

    def _run(self, profile, identifier, resume):
        last = [0.0, None]
        def progress(event):
            if not isinstance(event, dict): return
            phase = event.get('phase')
            if phase == 'captured':
                import re
                archive = event.get('archive')
                digest = event.get('sha256')
                if (not isinstance(archive, str) or '/' in archive or '\\' in archive
                        or not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest)):
                    raise core.FinixError('Invalid capture checkpoint')
                self._update(profile, identifier, {'state': 'running', 'capture': {
                    key: event[key] for key in ('archive', 'sha256', 'size', 'namespace', 'complete', 'omitted_count') if key in event}})
                return
            if phase not in {'snapshot', 'sqlite', 'checksum', 'upload', 'verify', 'commit', 'reconcile'}: return
            values = _progress()
            if 'completed_bytes' not in event and 'bytes' in event:
                event = dict(event, completed_bytes=event['bytes'])
            for key in values:
                value = event.get(key)
                if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                    values[key] = min(value, 100) if key == 'percent' else int(value)
            # Phase percentages, never an invented overall percentage.
            now = time.monotonic()
            if now-last[0] < .2 and last[1] == phase: return
            last[:] = [now, phase]
            self._update(profile, identifier, {'phase': phase, 'progress': values})
        try:
            flow = self.flow_factory(profile)
            _, path, _ = self._paths(profile)
            state = self._read(profile, path)
            if state['job']['binding'] != flow._binding():
                raise core.FinixError('BACKUP_BINDING_CHANGED')
            if resume:
                resume = state['job'].get('capture') or True
            if (not resume or resume is True) and callable(getattr(profile, 'pending_backup_checkpoint', None)):
                checkpoint = profile.pending_backup_checkpoint(progress=progress)
                if checkpoint is not None:
                    # fsync via atomic_json BEFORE any reconciliation/provider I/O.
                    self._update(profile, identifier, {'state': 'running', 'phase': 'reconcile', 'capture': checkpoint})
                    resume = checkpoint
            result = flow.check_and_run(progress=progress, cancel=None, **({'resume': resume} if resume else {}))
            backup = result['first_backup']
            complete = backup.get('complete')
            verified = result.get('verified') is True or backup.get('verified') is True
            self._update(profile, identifier, dict(state='succeeded' if complete and verified else 'incomplete',
                phase='committed', progress=dict(_progress(), percent=100 if complete and verified else None), verified=verified,
                complete=complete, omitted_count=backup.get('omitted_count'), error_code=None))
        except BaseException as exc:
            safe = {'PENDING_RECONCILIATION': 'SETUP_CLOUD_RECONCILIATION_REQUIRED',
                    'BACKUP_BINDING_CHANGED': 'BACKUP_BINDING_CHANGED'}
            code = (safe.get(str(exc), setup_error_code(exc)) if isinstance(exc, Exception)
                    else 'BACKUP_INTERRUPTED')
            self._update(profile, identifier, dict(state='failed', phase='failed', verified=False,
                                                 error_code=code, progress=_progress()))
