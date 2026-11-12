"""Durable PREVIEW owner; a record never reconstructs a decrypted capability.

One worker lease spans admission, work and terminal persistence. Status/cancel
use only a short state lease. Death is interrupted preservation, never retry.
"""
from contextlib import ExitStack
from contextvars import copy_context
import threading
from . import core, preview_budget
from .backup_jobs import file_lease, state_lease, _busy

CONTRACT = 'default-preview-job-v1'


class PreviewJobs:
    def assert_available(self, profile):
        from .default_recovery_api import cold, RecoveryError
        base=profile.home/'cache/finix/default-preview'
        current=base/'current.json'
        if not current.exists():return
        with state_lease(base/'state.lock'):
            job=cold._load(current)['id']
            from .default_recovery_api import identifier
            path=base/'jobs'/(identifier(job)+'.json')
            state=self._read(profile,profile.home,path)
            if state['state'] in ('running','interrupted'):raise RecoveryError('BUSY')

    def _paths(self, profile, job):
        from .default_recovery_api import authority, identifier
        root = authority(profile)
        base = core.private_dir(root/'cache/finix/default-preview')
        return root, base, core.private_dir(base/'jobs')/(identifier(job)+'.json')

    def _read(self, profile, root, path):
        from .default_recovery_api import cold, RecoveryError
        state = cold._load(path)
        if (state.get('version') != 1 or state.get('profile_id') != profile.profile_id
                or state.get('root_identity') != cold._identity(root)):
            raise RecoveryError('NOT_FOUND')
        return state

    def _public(self, state):
        return dict(contract=CONTRACT, id=state['id'], state=state['state'],
                    error=state['error'], result=state['result'],
                    cancellable=state['state']=='running' and not state['cancel'])

    def _refresh(self, profile, root, base, path):
        from .default_recovery_api import cold
        state = self._read(profile, root, path)
        dispatching = state.get('phase')=='dispatching' and _busy(base/'dispatch.lock')
        if state['state']=='running' and not dispatching and not _busy(base/'worker.lock'):
            state.update(state='interrupted', error='PREVIEW_INTERRUPTED', result=None)
            cold._write(path, state)
        return state

    def status(self, profile, job):
        root, base, path = self._paths(profile, job)
        with state_lease(base/'state.lock'):
            return self._public(self._refresh(profile, root, base, path))

    def cancel(self, profile, job):
        from .default_recovery_api import cold
        root, base, path = self._paths(profile, job)
        with state_lease(base/'state.lock'):
            state = self._refresh(profile, root, base, path)
            if state['state']=='running':
                state['cancel']=True
                cold._write(path, state)
            return self._public(state)

    def begin(self, profile, job, work):
        from .default_recovery_api import cold, activation, RecoveryError
        from .recovery_http import operation_lock
        root, base, path = self._paths(profile, job)
        retained = ExitStack()
        try:
            with state_lease(base/'state.lock'):
                # Exact replay precedes service lookup/key/candidate work. Failed
                # results stay immutable even after another job becomes current.
                if path.exists():
                    return self._public(self._refresh(profile, root, base, path))
                retained.enter_context(file_lease(base/'worker.lock'))
                current = base/'current.json'
                if current.exists():
                    old = cold._load(current)
                    prior = self._read(profile, root, base/'jobs'/(old['id']+'.json'))
                    if prior['state'] in ('running','interrupted'):
                        raise RecoveryError('BUSY')
                retained.enter_context(operation_lock(profile,preview_job=job))
                state = dict(version=1,id=job,profile_id=profile.profile_id,
                    root_identity=cold._identity(root),activation=activation(profile),
                    state='running',error=None,result=None,cancel=False)
                cold._write(path,state)
                cold._write(current,dict(id=job))
                answer=self._public(state)
            context=copy_context()
            worker=threading.Thread(target=lambda held=retained:context.run(self._run,profile,job,work,held),
                                    name='finix-default-preview',daemon=False)
            worker.start()
            retained=None  # Ownership transfers before acknowledgement.
            return answer
        except core.FinixError as exc:
            if str(exc)=='JOB_BUSY':raise RecoveryError('BUSY') from None
            raise
        finally:
            if retained is not None:retained.close()

    def dispatch(self, profile, job, intent, launch):
        """Reserve once before launching a CLI owner; a lost ACK never relaunches."""
        import time, uuid
        from .default_recovery_api import cold, activation, RecoveryError
        from .recovery_http import operation_lock
        root, base, path = self._paths(profile, job)
        with file_lease(base/'dispatch.lock'):
            with state_lease(base/'state.lock'):
                if path.exists():
                    state=self._refresh(profile,root,base,path)
                    if state.get('intent') != intent:raise RecoveryError('INVALID_CANDIDATE')
                    return self._public(state)
                with file_lease(base/'worker.lock'), operation_lock(profile,preview_job=job):
                    current=base/'current.json'
                    if current.exists():
                        prior=self._read(profile,root,base/'jobs'/(cold._load(current)['id']+'.json'))
                        if prior['state'] in ('running','interrupted'):raise RecoveryError('BUSY')
                    nonce=uuid.uuid4().hex
                    state=dict(version=1,id=job,profile_id=profile.profile_id,
                        root_identity=cold._identity(root),activation=activation(profile),
                        state='running',phase='dispatching',nonce=nonce,intent=intent,
                        error=None,result=None,cancel=False)
                    cold._write(path,state)
                    cold._write(current,dict(id=job))
            # No state lease during process launch or acknowledgement wait.
            # A launch error is uncertain: retain the durable running intent.
            try:launch(job,nonce)
            except Exception:raise RecoveryError('PREVIEW_ACK_UNCERTAIN') from None
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                with state_lease(base/'state.lock'):
                    observed=self._read(profile,root,path)
                    if observed.get('phase')!='dispatching' or observed['state']!='running':
                        return self._public(observed)
                time.sleep(.02)
            raise RecoveryError('PREVIEW_ACK_UNCERTAIN')

    def claim(self, profile, job, nonce, work):
        """Fresh child mints its OWN handoff; never restores a serialized service."""
        from .default_recovery_api import cold, activation, RecoveryError
        from .recovery_http import operation_lock
        root, base, path = self._paths(profile,job)
        with ExitStack() as retained:
            retained.enter_context(file_lease(base/'worker.lock'))
            retained.enter_context(operation_lock(profile,backend_lease=False,preview_job=job))
            with state_lease(base/'state.lock'):
                state=self._read(profile,root,path)
                if (state['state']!='running' or state.get('phase')!='dispatching'
                        or state.get('nonce')!=nonce or activation(profile)!=state['activation']):
                    raise RecoveryError('INVALID_CAPABILITY')
                state['phase']='preparing'
                cold._write(path,state)
            self._run(profile,job,work,retained)

    def _run(self, profile, job, work, retained):
        with retained:
            self._execute(profile,job,work,retained)

    def _execute(self, profile, job, work, retained):
        from .default_recovery_api import cold, activation, public_error, RecoveryError
        root, base, path = self._paths(profile, job)
        def cancelled():
            with state_lease(base/'state.lock'):
                return self._read(profile,root,path)['cancel']
        result=None;error=None;terminal='failed'
        try:
            with preview_budget.scope(seconds=float('inf'),cancelled=cancelled):
                result=work()
                preview_budget.check()
                with state_lease(base/'state.lock'):
                    state=self._read(profile,root,path)
                    if state['cancel']:raise RecoveryError('PREVIEW_CANCELLED')
                    if activation(profile)!=state['activation']:raise RecoveryError('STALE_PROFILE')
                    state.update(state='succeeded',result=result,error=None)
                    cold._write(path,state)
                    return
        except BaseException as exc:
            error=public_error(exc)
            terminal='cancelled' if error=='PREVIEW_CANCELLED' else 'failed'
        finally:
            try:
                if error is not None:
                    with state_lease(base/'state.lock'):
                        state=self._read(profile,root,path)
                        if state['state']=='running':
                            state.update(state=terminal,result=None,error=error)
                            cold._write(path,state)
            finally:retained.close()
