"""SOURCE-owned durable Restore admission; no durable secret/capability recovery.

The kernel worker lease outlives HTTP requests. State readers use a different,
short lease. A dead owner is preserved/interrupted, never silently replayed.
"""
from contextlib import ExitStack
import contextvars
import json
import re
import threading
import time
import uuid

from . import core
from .backup_jobs import file_lease, state_lease, _busy
from .recovery_import import RecoveryImportError

_PUBLIC = ('id','request_id','profile_id','state','phase','cancellable','name',
           'error','receipt','full_acceptance','updated_at','cancel_requested')


def _fail(code):
    raise RecoveryImportError(code)


def _request(value):
    try:
        if str(uuid.UUID(value)) != value: raise ValueError()
    except (ValueError,TypeError,AttributeError):
        _fail('INVALID_REQUEST_ID')
    return value


class RecoveryOperations:
    def __init__(self):
        self._workers = {}
        self._lock = threading.RLock()

    def _paths(self, service):
        root=core.private_dir(service.profile/'cache/finix/recovery-operations')
        return root,root/'current.json',root/'worker.lock'

    def _read(self, service, path):
        try:
            with core.regular_input(path) as fd:
                state=json.loads(core.read_regular(fd,256*1024))
        except FileNotFoundError:return None
        if state.get('profile_id')!=service.profile_id or state.get('activation')!=service.activation:
            _fail('STALE_PROFILE')
        return state

    def _save(self, root, state):
        state['updated_at']=time.time()
        core.atomic_json(core.private_dir(root/'history')/(state['id']+'.json'),state)
        core.atomic_json(root/'current.json',state)

    def _view(self, state):
        if not state:
            return None
        view = {key: state[key] for key in _PUBLIC if key in state}
        view.update(historical_only=True, runtime_capability_supported=False,
                    cancellable=False, full_acceptance=False)
        return view

    def _refresh(self,service,root,path,lease):
        return self._read(service, path)

    def current(self,service):
        service._fence(service.profile_id,service.activation)
        root,path,lease=self._paths(service)
        with state_lease(root/'state.lock'):
            state=self._refresh(service,root,path,lease)
            service._fence(service.profile_id,service.activation)
            return self._view(state)

    def status(self,service,identifier):
        if not isinstance(identifier,str) or not re.fullmatch('[a-f0-9]{32}',identifier):_fail('NOT_FOUND')
        root,path,lease=self._paths(service)
        service._fence(service.profile_id,service.activation)
        with state_lease(root/'state.lock'):
            state=self._refresh(service,root,path,lease)
            if not state or state['id']!=identifier:
                state=self._read(service,root/'history'/(identifier+'.json'))
            if state is None:_fail('NOT_FOUND')
            return self._view(state)

    def replay(self,service,*,request_id,candidate_id,acknowledge_incomplete=False):
        from .recovery_availability import require_public_contract
        require_public_contract()

    def begin(self,service,*,request_id,candidate_id,acknowledge_incomplete=False):
        from .recovery_availability import require_public_contract
        require_public_contract()

    def _publish(self,service,owned,**changes):
        service._fence(service.profile_id,service.activation)
        root,path,_=self._paths(service)
        with state_lease(root/'state.lock'):
            state=self._read(service,path)
            if not state or state['id']!=owned['id']:_fail('RECOVERY_INTERRUPTED')
            # Retain cancellation/key admission fields written by request threads.
            state.update(changes);owned.update(state)
            self._save(root,state)

    def key(self,service,identifier,*,request_id,identity):
        from .recovery_availability import require_public_contract
        try:
            require_public_contract()
        finally:
            if isinstance(identity, bytearray):
                identity[:] = b'\x00' * len(identity)

    def cancel(self,service,identifier):
        from .recovery_availability import require_public_contract
        require_public_contract()

    def _cancelled(self,service,owned):
        root,path,_=self._paths(service)
        with state_lease(root/'state.lock'):
            state=self._read(service,path)
            if not state or state['id']!=owned['id']:_fail('RECOVERY_INTERRUPTED')
            owned.update(state)
            return state['cancel_requested']

    def _execute(self,service,owned):
        from .recovery_availability import require_public_contract
        require_public_contract()


    def close(self):
        with self._lock:slots=list(self._workers.values())
        for slot in slots:slot['thread'].join(timeout=5)
