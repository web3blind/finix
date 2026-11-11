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

    def _view(self,state):
        return {k:state[k] for k in _PUBLIC if k in state} if state else None

    def _refresh(self,service,root,path,lease):
        state=self._read(service,path)
        if state and state['state']=='running' and not _busy(lease):
            state.update(state='interrupted',error='RECOVERY_INTERRUPTED',cancellable=False)
            self._save(root,state)
        return state

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
        """Read exact durable intent only; never acquire worker/runtime authority."""
        _request(request_id)
        if not isinstance(candidate_id,str) or not re.fullmatch('[a-f0-9]{64}',candidate_id):_fail('INVALID_CANDIDATE')
        if type(acknowledge_incomplete) is not bool:_fail('INVALID_REQUEST')
        service._fence(service.profile_id,service.activation)
        root,path,lease=self._paths(service)
        with state_lease(root/'state.lock'):
            current=self._refresh(service,root,path,lease)
            prior=self._read(service,root/'requests'/(request_id+'.json'))
            if not prior:
                if current and current['state'] not in ('succeeded','cancelled','failed'):_fail('BUSY')
                return None
            if prior['intent']!=dict(candidate_id=candidate_id,acknowledge_incomplete=acknowledge_incomplete):_fail('REQUEST_CONFLICT')
            historical=self._read(service,root/'history'/(prior['id']+'.json'))
            if historical is None:_fail('RECOVERY_INTERRUPTED')
            service._fence(service.profile_id,service.activation)
            return self._view(historical)

    def begin(self,service,*,request_id,candidate_id,acknowledge_incomplete=False):
        _request(request_id)
        if not isinstance(candidate_id,str) or not re.fullmatch('[a-f0-9]{64}',candidate_id):_fail('INVALID_CANDIDATE')
        if type(acknowledge_incomplete) is not bool:_fail('INVALID_REQUEST')
        service._fence(service.profile_id,service.activation)
        root,path,lease=self._paths(service)
        intent=dict(candidate_id=candidate_id,acknowledge_incomplete=acknowledge_incomplete)
        with state_lease(root/'state.lock'):
            state=self._refresh(service,root,path,lease)
            prior=self._read(service,root/'requests'/(request_id+'.json'))
            if prior:
                if prior['intent']!=intent:_fail('REQUEST_CONFLICT')
                historical=self._read(service,root/'history'/(prior['id']+'.json'))
                if historical is None:_fail('RECOVERY_INTERRUPTED')
                return self._view(historical)
            if state and state['state'] not in ('succeeded','cancelled','failed'):_fail('BUSY')
            from .recovery_shared_host import check_bootstrap_compatibility
            check_bootstrap_compatibility(service)
            ready=threading.Event();proceed=threading.Event();acquired=[]
            owned=dict(id=uuid.uuid4().hex,request_id=request_id,profile_id=service.profile_id,
                activation=service.activation,intent=intent,state='running',phase='preparing',
                cancellable=True,cancel_requested=False,full_acceptance=False)
            slot=dict(owned=owned,wake=threading.Event(),key=None,service=service)
            def worker():
                try:
                    with file_lease(lease):
                        acquired.append(True);ready.set();proceed.wait()
                        if slot.get('committed'):
                            self._execute(service,owned)
                except BaseException as exc:
                    ready.set()
                    if slot.get('committed'):
                        # No authority can be recreated here. Preserve exact phase.
                        safe_failure=(owned['phase']=='data_running' and
                            isinstance(exc,RecoveryImportError) and exc.code in
                            ('WRONG_KEY','CORRUPT_ARCHIVE','TARGET_NOT_EMPTY','COMPLETENESS_CHANGED'))
                        try:self._publish(service,owned,state='failed' if safe_failure else 'interrupted',
                            error=exc.code if safe_failure else 'RECOVERY_INTERRUPTED',cancellable=False)
                        except Exception:pass
                finally:
                    key=slot.get('key')
                    if key is not None:key[:]=b'\x00'*len(key)
                    slot['key']=None
            context=contextvars.copy_context()
            thread=threading.Thread(target=context.run,args=(worker,),daemon=True)
            slot['thread']=thread
            with self._lock:self._workers[owned['id']]=slot
            thread.start()
            try:
                if not ready.wait(2) or not acquired:_fail('BUSY')
                service._fence(service.profile_id,service.activation)
                self._save(root,owned)
                core.atomic_json(core.private_dir(root/'requests')/(request_id+'.json'),
                    dict(id=owned['id'],profile_id=service.profile_id,activation=service.activation,intent=intent))
                slot['committed']=True
            finally:proceed.set()
            return self._view(owned)

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
        _request(request_id)
        if not isinstance(identity,bytearray) or not 0<len(identity)<=16384:_fail('INVALID_IDENTITY')
        root,path,_=self._paths(service)
        service._fence(service.profile_id,service.activation)
        try:
            with state_lease(root/'state.lock'):
                state=self._read(service,path)
                if not state or state['id']!=identifier:_fail('NOT_FOUND')
                if state.get('key_request_id')==request_id:return self._view(state)
                if state['phase']!='awaiting_key' or state['cancel_requested']:_fail('INVALID_STATE')
                with self._lock:slot=self._workers.get(identifier)
                if not slot or not slot['thread'].is_alive():_fail('RECOVERY_INTERRUPTED')
                state.update(key_request_id=request_id,phase='data_queued')
                self._save(root,state)
                slot['key']=bytearray(identity);slot['wake'].set()
                return self._view(state)
        finally:identity[:]=b'\x00'*len(identity)

    def cancel(self,service,identifier):
        root,path,_=self._paths(service)
        service._fence(service.profile_id,service.activation)
        with state_lease(root/'state.lock'):
            state=self._read(service,path)
            if not state or state['id']!=identifier:_fail('NOT_FOUND')
            if state['state']=='cancelled':return self._view(state)
            if state['state']!='running' or not state['cancellable']:_fail('NOT_CANCELLABLE')
            state['cancel_requested']=True
            self._save(root,state)
            with self._lock:slot=self._workers.get(identifier)
            if slot:slot['wake'].set()
            return self._view(state)

    def _cancelled(self,service,owned):
        root,path,_=self._paths(service)
        with state_lease(root/'state.lock'):
            state=self._read(service,path)
            if not state or state['id']!=owned['id']:_fail('RECOVERY_INTERRUPTED')
            owned.update(state)
            return state['cancel_requested']

    def _execute(self,service,owned):
        from hermes_cli import profiles as host
        from .recovery_pipeline import complete_restore
        import io
        slot=self._workers[owned['id']]
        pid,epoch=service.profile_id,service.activation
        service._fence(pid,epoch)
        rows=service.catalog(pid,epoch)
        candidate=next((r for r in rows if r['id']==owned['intent']['candidate_id']),None)
        if candidate is None:_fail('INVALID_CANDIDATE')
        if candidate['complete'] is not True and not owned['intent']['acknowledge_incomplete']:_fail('INCOMPLETE_ACK_REQUIRED')
        name='recovered-'+uuid.uuid4().hex[:16]
        host.validate_profile_name(name)
        if core.checked_path(host.get_profile_dir(name)).exists():_fail('PROFILE_EXISTS')
        destination=service.allocate_destination(pid,epoch)
        self._publish(service,owned,phase='awaiting_key',name=name)
        while not slot['wake'].wait(.1):
            if self._cancelled(service,owned):break
            service._fence(pid,epoch)
        if self._cancelled(service,owned):
            self._publish(service,owned,state='cancelled',phase='cancelled',cancellable=False)
            return
        key=slot['key']
        if key is None:_fail('RECOVERY_INTERRUPTED')
        stream=io.BytesIO(key)
        key[:]=b'\x00'*len(key);slot['key']=None
        job=None
        try:
            self._publish(service,owned,phase='data_running')
            service._fence(pid,epoch)
            job=service.start(candidate['id'],identity=stream,destination=destination,profile_id=pid,activation=epoch)
            while True:
                if self._cancelled(service,owned):service.cancel(job)
                data=service.wait(job,.1)
                service._fence(pid,epoch)
                if data['status']!='running':break
            buffer=stream.getbuffer();buffer[:]=b'\x00'*len(buffer);buffer.release();stream.close()
            # Cancel may race successful data completion. Never proceed to import.
            if self._cancelled(service,owned):
                if data['status']=='succeeded':service.cleanup_managed_result(job)
                self._publish(service,owned,state='cancelled',phase='cancelled',cancellable=False)
                return
            if data['status']!='succeeded':
                code=data.get('error')
                _fail(code if code in ('WRONG_KEY','CORRUPT_ARCHIVE','TARGET_NOT_EMPTY') else 'DECRYPT_FAILED')
            if candidate['complete'] is True and data['result']['complete'] is not True and not owned['intent']['acknowledge_incomplete']:
                service.cleanup_managed_result(job)
                _fail('COMPLETENESS_CHANGED')
            root,path,_=self._paths(service)
            with state_lease(root/'state.lock'):
                current=self._read(service,path)
                if current['cancel_requested']:
                    proceed=False
                else:
                    proceed=True;current.update(phase='importing',cancellable=False);owned.update(current);self._save(root,current)
            if not proceed:
                service.cleanup_managed_result(job)
                self._publish(service,owned,state='cancelled',phase='cancelled',cancellable=False)
                return
            result=complete_restore(service,job,profile_id=pid,activation=epoch,name=name,
                acknowledge_incomplete=owned['intent']['acknowledge_incomplete'],
                phase=lambda phase:self._publish(service,owned,phase=phase,cancellable=False))
            self._publish(service,owned,state='succeeded',phase='backend_target_ready',cancellable=False,receipt=result)
        finally:
            # The data worker owns this stream until wait returns. Never wipe it
            # from the HTTP request or while native age may still be reading.
            if job is not None:service.wait(job)
            if not stream.closed:
                buffer=stream.getbuffer();buffer[:]=b'\x00'*len(buffer);buffer.release();stream.close()


    def close(self):
        with self._lock:slots=list(self._workers.values())
        for slot in slots:slot['thread'].join(timeout=5)
