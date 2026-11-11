"""Authenticated local Desktop HTTP adapter. No automatic upload spooling."""
from contextlib import contextmanager
import hashlib
import ipaddress
import io
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
import uuid
from fastapi import APIRouter, HTTPException, Request, Response
from . import core, cloud_files
from .recovery import RecoveryService, RecoveryError, CommittedSource, _ManagedDestination
from .recovery_import import RecoveryImporter, RecoveryImportError
from .recovery_contract import public_job
from .setup_flow import SetupFlow
from starlette.concurrency import run_in_threadpool


@contextmanager
def operation_lock(profile):
    from .backup_jobs import file_lease, state_lease
    from contextlib import ExitStack
    root=core.private_dir(profile.home/'cache/finix/recovery-operations')
    retained=ExitStack()
    try:
        retained.enter_context(file_lease(root/'worker.lock'))
        with state_lease(root/'state.lock'):
            try:
                with core.regular_input(root/'current.json') as fd:
                    state=json.loads(core.read_regular(fd,256*1024))
            except FileNotFoundError:state=None
            if state and state.get('state') not in ('succeeded','cancelled','failed'):
                fail('BUSY')
        retained.enter_context(core.locked(profile.directories()))
    except (OSError, core.FinixError):
        retained.close()
        fail('BUSY')
    except BaseException:
        retained.close()
        raise
    try:
        yield
    finally:
        retained.close()


@contextmanager
def state_lock(adapter):
    if not adapter.lock.acquire(blocking=False):
        fail('BUSY')
    try:
        yield
    finally:
        adapter.lock.release()

MAX_KEY = 16384
MAX_BODY = MAX_KEY + 2048
MAX_CIPHER = 1024 ** 4


def fail(code, status=409):
    raise HTTPException(status, code, headers={'Cache-Control':'no-store'})


def local_request(request):
    try:
        return bool(request.client and ipaddress.ip_address(request.client.host).is_loopback)
    except ValueError:
        return False


def explicit_path(value):
    if (not isinstance(value, str) or not value or len(value) > 4096 or '\x00' in value
            or not Path(value).is_absolute() or '..' in Path(value).parts
            or (os.name == 'nt' and (value.startswith(('\\\\', '//')) or ':' in value[2:]))):
        raise RecoveryError('INVALID_SOURCE')
    return core.checked_path(value)


@contextmanager
def trusted_file(path):
    with core.regular_input(core.checked_path(path)) as fd:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RecoveryError('INVALID_SOURCE')
        if os.name == 'nt':
            import msvcrt
            from . import windows_native as win
            if not win.trusted_handle(msvcrt.get_osfhandle(fd)):
                raise RecoveryError('INVALID_SOURCE')
        elif info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise RecoveryError('INVALID_SOURCE')
        yield fd


def binding(profile):
    return SetupFlow(profile)._binding()


class FencedService(RecoveryService):
    def _check_authority(self):
        super()._check_authority()
        if binding(self.owner)!=self.generation:
            raise RecoveryError('STALE_PROFILE')

    def add_source(self, source):
        transfer = getattr(source.transfer,'_finix_transfer',source.transfer)
        def measured(row, output, cancel):
            with self._lock:
                active = next((job for job,event in self._jobs.values() if job['status']=='running'),None)
                if active is not None:
                    active.update(phase='local_reading' if source.connection in {'local','explicit-local'} else 'cloud_reading',
                                  download_bytes=0,download_total=row['size'])
            class Sink:
                def write(inner, data):
                    count = output.write(data)
                    with self._lock:
                        if active is not None:
                            active['download_bytes'] += count
                    return count
            return transfer(row,Sink(),cancel)
        measured._finix_transfer = transfer
        return super().add_source(CommittedSource(source.connection,source.generation,source.records,measured))

    def __init__(self, profile, generation):
        self.owner, self.generation = profile, generation
        from .extra_sources import load
        extras = load(profile.home, profile.default_root, (profile.root.parent,))
        protected = [profile.default_root, profile.default_root/'profiles', profile.root.parent]
        protected.extend(Path(x['path']) for x in extras)
        super().__init__(profile_id=profile.profile_id, profile_root=profile.home,
                         activation=generation, protected_roots=protected)

    def _destination(self, destination):
        if binding(self.owner) != self.generation:
            raise RecoveryError('STALE_PROFILE')
        result = super()._destination(destination if isinstance(destination, _ManagedDestination)
                                        else explicit_path(str(destination)))
        with core.locked(result.parent):
            if result.exists():
                with core.locked(result):
                    if any(result.iterdir()):
                        raise RecoveryError('INVALID_DESTINATION')
            volume_path = result if result.exists() else result.parent
            if volume_path.stat().st_dev != self.profile.stat().st_dev:
                fail('DESTINATION_VOLUME_UNSUPPORTED')
        return result

    def _run(self, *args):
        try:
            with core.locked(self.owner.directories()):
                return super()._run(*args)
        except Exception:
            with self._lock:
                self._jobs[args[0]][0].update(status='failed', phase='complete', error='BUSY')


def add_explicit_archive(service, value):
    path = explicit_path(value)
    if any(path == p or p in path.parents for p in service.protected):
        raise RecoveryError('INVALID_SOURCE')
    deadline = time.monotonic() + 120
    with trusted_file(path) as fd:
        before = core.fingerprint(os.fstat(fd))
        size = os.fstat(fd).st_size
        if not 0 < size <= MAX_CIPHER:
            raise RecoveryError('INVALID_SOURCE')
        digest = hashlib.sha256()
        while chunk := os.read(fd, 1024*1024):
            if time.monotonic() > deadline:
                raise RecoveryError('INVALID_SOURCE')
            digest.update(chunk)
        if core.fingerprint(os.fstat(fd)) != before:
            raise RecoveryError('INVALID_SOURCE')
    row = dict(size=size, sha256=digest.hexdigest())
    def records():
        with trusted_file(path) as fd:
            if core.fingerprint(os.fstat(fd)) != before:
                raise RecoveryError('INVALID_SOURCE')
        return [row]
    def transfer(record, output, cancel):
        with trusted_file(path) as fd:
            if core.fingerprint(os.fstat(fd)) != before:
                raise RecoveryError('INVALID_SOURCE')
            while chunk := os.read(fd, 1024*1024):
                if cancel():
                    raise RecoveryError('CANCELLED')
                output.write(chunk)
            if core.fingerprint(os.fstat(fd)) != before:
                raise RecoveryError('INVALID_SOURCE')
    service.add_source(CommittedSource('explicit-local', uuid.uuid4().hex, records, transfer))
    rows = service.catalog(service.profile_id, service.activation)
    return next(x['id'] for x in rows if service._catalog[x['id']][0].connection == 'explicit-local')


async def bounded_body(request, limit):
    headers = request.headers.getlist('content-length')
    if (len(headers) != 1 or not re.fullmatch(r'[0-9]{1,8}', headers[0])
            or request.headers.get('transfer-encoding') or request.headers.get('content-encoding')):
        fail('CONTENT_LENGTH_REQUIRED',413)
    length = int(headers[0])
    if not 0 < length <= limit:
        fail('BODY_TOO_LARGE',413)
    data = bytearray()
    async for chunk in request.stream():
        if len(data)+len(chunk) > length or len(data)+len(chunk) > limit:
            fail('BODY_TOO_LARGE',413)
        data.extend(chunk)
    if len(data) != length:
        fail('INVALID_BODY',422)
    return data


def key_part(data, content_type):
    match = re.fullmatch(r'multipart/form-data;\s*boundary=(?:"([A-Za-z0-9_-]{1,70})"|([A-Za-z0-9_-]{1,70}))', content_type)
    if not match:
        fail('INVALID_MULTIPART',422)
    boundary = (match[1] or match[2]).encode()
    prefix, suffix = b'--'+boundary+b'\r\n', b'\r\n--'+boundary+b'--\r\n'
    if not data.startswith(prefix) or not data.endswith(suffix):
        fail('INVALID_MULTIPART',422)
    split = data.find(b'\r\n\r\n',len(prefix),len(prefix)+1028)
    if split < 0:
        fail('MULTIPART_HEADERS_TOO_LARGE',413)
    try:
        lines = bytes(data[len(prefix):split]).decode('ascii').split('\r\n')
    except UnicodeError:
        fail('INVALID_MULTIPART',422)
    if len(lines) not in (1,2) or not re.fullmatch(
            r'Content-Disposition: form-data; name="file"; filename="[^"\r\n]{1,255}"',lines[0],re.I):
        fail('INVALID_MULTIPART',422)
    if len(lines) == 2 and not re.fullmatch(r'Content-Type: [A-Za-z0-9.+/-]{1,100}',lines[1],re.I):
        fail('INVALID_MULTIPART',422)
    key = bytes(data[split+4:-len(suffix)])
    if b'\r\n--'+boundary in key:
        fail('INVALID_MULTIPART',422)
    if not 0 < len(key) <= MAX_KEY:
        fail('KEY_TOO_LARGE',413)
    return key


class RecoveryHTTP:
    def __init__(self):
        self.lock = threading.RLock()
        self.services, self.jobs = {}, {}

    def service(self, profile):
        generation = binding(profile)
        service = self.services.get(profile.profile_id)
        if service and service.activation != generation:
            service.deactivate()
            if service._worker and service._worker.is_alive():
                raise RecoveryError('BUSY')
            service = None
        if service is None:
            service = FencedService(profile,generation)
            service.add_local('local',profile.root/'copies',profile.root/'index')
            source = service._sources['local']
            service.add_source(CommittedSource('local',generation,source.records,source.transfer))
            register_cloud(service,profile)
            self.services[profile.profile_id] = service
        return service

    def view(self, job):
        if job.get('pipeline_state'):
            state=job['pipeline_state']
            return dict(id=job['id'],profile_id=job['profile_id'],state=state,status=state,
                phase=job['pipeline_phase'],cancellable=False,can_import=False,
                backend_target_ready=state=='succeeded',full_acceptance=False,
                archive_verified=True,content_verified=state=='succeeded',completion_scope='backend',
                receipt=job.get('backend_receipt'))
        if job['service_id']:
            state = job['service'].status(job['service_id'])
            return dict(public_job(job['service'],job['service_id'],job['profile_id'],state),
                        id=job['id'],profile_id=job['profile_id'])
        if time.monotonic()-job['created'] > 600:
            job['state'] = 'cancelled'
        return dict(id=job['id'],profile_id=job['profile_id'],state=job['state'],
                    status=job['state'],phase=job['state'],progress=0,bytes=0,
                    error=None,code=None,complete=None,omitted_count=None,can_import=False,
                    extras_policy='separate_manual_import_only')

    def lookup(self, profile, identifier):
        job = self.jobs.get(identifier)
        if job is None or job['profile_id'] != profile.profile_id:
            fail('NOT_FOUND',404)
        if job['service'].activation != binding(profile):
            job['service'].deactivate()
            fail('STALE_PROFILE')
        return job


from .recovery_sources import register_cloud


def create_router(current_profile):
    router = APIRouter(prefix='/recovery')
    adapter = RecoveryHTTP()
    def guard(request,response):
        response.headers['Cache-Control'] = 'no-store'
        if not local_request(request):
            fail('LOCAL_DESKTOP_REQUIRED',403)
        try:
            profile = current_profile()
            if not re.fullmatch(r'[a-f0-9]{64}',profile.profile_id):
                fail('PROFILE_UNAVAILABLE')
            return profile
        except Exception:
            fail('PROFILE_UNAVAILABLE')
    @contextmanager
    def closed_errors():
        try:
            yield
        except HTTPException:
            raise
        except (RecoveryError, RecoveryImportError) as exc:
            fail(exc.code,404 if exc.code == 'NOT_FOUND' else 409)
        except Exception:
            fail('RESTORE_FAILED')

    from .recovery_operations import RecoveryOperations
    operations = RecoveryOperations()
    router.recovery_operations = operations

    def operation_service(profile):
        # Reuse an already admitted service without holding adapter state across
        # SDK work; creation is short and contains no archive import/start.
        with state_lock(adapter):
            return adapter.service(profile)

    @router.post('/operations')
    async def operation_begin(request: Request,response: Response):
        profile=guard(request,response)
        with closed_errors():
            raw=await bounded_body(request,4096)
            try:body=json.loads(raw)
            except (ValueError,UnicodeError):fail('INVALID_REQUEST',422)
            required={'profile_id','candidate_id','consent_restore','request_id'}
            if (not isinstance(body,dict) or not required<=set(body)
                    or set(body)-required-{'acknowledge_incomplete'}
                    or body.get('consent_restore') is not True
                    or type(body.get('acknowledge_incomplete',False)) is not bool):fail('INVALID_REQUEST',422)
            if body['profile_id']!=profile.profile_id:fail('STALE_PROFILE')
            service=operation_service(profile)
            return operations.begin(service,request_id=body['request_id'],candidate_id=body['candidate_id'],
                acknowledge_incomplete=body.get('acknowledge_incomplete',False))

    @router.get('/operations/current')
    def operation_current(request: Request,response: Response):
        profile=guard(request,response)
        with closed_errors():return operations.current(operation_service(profile))

    @router.get('/operations/{identifier}')
    def operation_status(identifier: str,request: Request,response: Response):
        profile=guard(request,response)
        with closed_errors():return operations.status(operation_service(profile),identifier)

    @router.post('/operations/{identifier}/cancel')
    def operation_cancel(identifier: str,request: Request,response: Response):
        profile=guard(request,response)
        with closed_errors():return operations.cancel(operation_service(profile),identifier)

    @router.post('/operations/{identifier}/key')
    async def operation_key(identifier: str,request: Request,response: Response):
        profile=guard(request,response)
        with closed_errors():
            data=await bounded_body(request,MAX_BODY)
            identity=None
            try:
                identity=bytearray(key_part(data,request.headers.get('content-type','')))
                phase_id=request.query_params.get('request_id')
                header_id=request.headers.get('X-Finix-Request-Id')
                if phase_id is not None and header_id is not None and phase_id!=header_id:
                    fail('INVALID_REQUEST_ID',422)
                return operations.key(operation_service(profile),identifier,
                    request_id=phase_id if phase_id is not None else header_id,identity=identity)
            finally:
                data[:]=b'\x00'*len(data)
                if identity is not None:identity[:]=b'\x00'*len(identity)

    @router.get('/capabilities')
    def capabilities(request: Request,response: Response):
        profile = guard(request,response)
        with closed_errors():
            from .recovery_shared_host import check_bootstrap_compatibility
            condition = check_bootstrap_compatibility(adapter.service(profile))
        return dict(code_version='recovery-v1',operation_protocol='recovery-operations-v1',profile_id=profile.profile_id,local_only=True,
                    bootstrap_condition=condition,
                    local_request=True,destination_policy='same_volume_only',same_volume_only=True,
                    destination_action='Choose an empty folder on the same volume as the local profile.',
                    max_key_bytes=MAX_KEY,
                    source_limits={'drive':'committed_journal_required_no_cloud_scans'},
                    supported_sources=['local','rclone','workspace','drive','connector-drive'])

    @router.get('/catalog')
    def catalog(request: Request,response: Response):
        profile = guard(request,response)
        with closed_errors(),state_lock(adapter),operation_lock(profile):
            service = adapter.service(profile)
            items = []
            from datetime import datetime
            def known_date(row):
                value = row.get('created_at')
                if not isinstance(value,str) or len(value)>64:
                    return None
                try:
                    parsed = datetime.fromisoformat(value.replace('Z','+00:00'))
                    if parsed.tzinfo is None or parsed.utcoffset() is None:
                        return None
                except (ValueError,OverflowError):
                    return None
                return value
            catalog_rows = service.catalog(profile.profile_id,service.activation)
            local_dates = {}
            for source,row in service._catalog.values():
                date = known_date(row)
                if source.connection == 'local' and date:
                    local_dates.setdefault((row['sha256'],row['size']),set()).add(date)
            for item in catalog_rows:
                source,row = service._catalog[item['id']]
                date = known_date(row)
                if date is None and source.connection != 'explicit-local':
                    dates = local_dates.get((row['sha256'],row['size']),set())
                    if len(dates) == 1:
                        date = next(iter(dates))
                item['created_at'] = date
                source,row = service._catalog[item['id']]
                completion = cloud_files.known_completion(row)
                if completion is None:
                    try:
                        completion = cloud_files.completion_record(row,row)
                    except (core.FinixError,KeyError,TypeError):
                        completion = None
                completion = completion or {}
                item['complete'] = completion.get('complete')
                items.append(dict(item,label='Encrypted backup',source=('local' if source.connection == 'explicit-local' else source.connection),
                                  omitted_count=completion.get('omitted_count')))
            return dict(profile_id=profile.profile_id,items=items)

    @router.post('/prepare-auto')
    async def prepare_auto(request: Request,response: Response):
        profile = guard(request,response)
        with closed_errors():
            raw = await bounded_body(request,4096)
            try:
                body = json.loads(raw)
            except (ValueError,UnicodeError):
                fail('INVALID_REQUEST',422)
            required={'profile_id','candidate_id','consent_restore'}
            if (not isinstance(body,dict) or not required<=set(body)
                    or set(body)-required-{'request_id','acknowledge_incomplete'}
                    or body.get('consent_restore') is not True or not isinstance(body.get('candidate_id'),str)):
                fail('INVALID_REQUEST',422)
            if body['profile_id'] != profile.profile_id:fail('STALE_PROFILE')
            if not body.get('request_id'):fail('OPERATION_PROTOCOL_REQUIRED')
            return operations.begin(operation_service(profile),request_id=body['request_id'],
                candidate_id=body['candidate_id'],acknowledge_incomplete=body.get('acknowledge_incomplete',False))

    def prepare_auto_sync(profile,body,default_only=False):
        from hermes_cli import profiles as host
        with closed_errors(),state_lock(adapter),operation_lock(profile):
            if default_only:
                from .default_recovery_api import new_service
                service = new_service(profile)
            else:
                service = adapter.service(profile)
            for job in adapter.jobs.values():
                if job['profile_id'] == profile.profile_id and adapter.view(job)['state'] in {'awaiting_key','running'}:
                    fail('BUSY')
            rows = service.catalog(profile.profile_id,service.activation)
            candidate = next((r for r in rows if r['id'] == body['candidate_id']),None)
            if candidate is None:
                fail('INVALID_CANDIDATE')
            name = None
            for _ in range(32):
                proposed = 'recovered-' + uuid.uuid4().hex[:16]
                host.validate_profile_name(proposed)
                if (not core.checked_path(host.get_profile_dir(proposed)).exists()
                        and not any(j.get('auto_name') == proposed for j in adapter.jobs.values())):
                    name = proposed
                    break
            if name is None:
                fail('PROFILE_EXISTS')
            identifier = uuid.uuid4().hex
            while len(adapter.jobs) >= 32:
                oldest = next(iter(adapter.jobs))
                if adapter.view(adapter.jobs[oldest])['state'] in {'awaiting_key','running'}:
                    fail('BUSY')
                del adapter.jobs[oldest]
            destination = service.allocate_destination(profile.profile_id,service.activation)
            adapter.jobs[identifier] = dict(id=identifier,profile_id=profile.profile_id,service=service,
                service_id=None,state='awaiting_key',candidate=candidate['id'],destination=destination,
                auto_name=name,catalog_complete=candidate['complete'],created=time.monotonic())
            return dict(id=identifier,profile_id=profile.profile_id,state='awaiting_key',name=name)

    @router.post('/prepare')
    async def prepare(request: Request,response: Response):
        profile = guard(request,response)
        # Path-based extraction is not the whole Restore authority protocol.
        # Retain a closed compatibility refusal rather than an alternate owner.
        fail('OPERATION_PROTOCOL_REQUIRED')
        with closed_errors():
            raw = await bounded_body(request,16384)
            try:
                body = json.loads(raw)
            except (ValueError,UnicodeError):
                fail('INVALID_REQUEST',422)
            allowed = {'profile_id','candidate_id','archive_path','destination_path','consent_local','activation'}
            if (not isinstance(body,dict) or set(body)-allowed or body.get('consent_local') is not True
                    or not isinstance(body.get('destination_path'),str)
                    or bool(body.get('candidate_id')) == bool(body.get('archive_path'))):
                fail('INVALID_REQUEST',422)
            if body.get('profile_id') != profile.profile_id:
                fail('STALE_PROFILE')
            return await run_in_threadpool(prepare_sync,profile,body)

    def prepare_sync(profile,body):
        with closed_errors(),state_lock(adapter),operation_lock(profile):
            service = adapter.service(profile)
            if body.get('activation') is not None and body['activation'] != service.activation:
                fail('STALE_PROFILE')
            for j in adapter.jobs.values():
                if j['profile_id'] == profile.profile_id and adapter.view(j)['state'] in {'awaiting_key','running'}:
                    fail('BUSY')
            destination = service._destination(body['destination_path'])
            if body.get('archive_path'):
                candidate = add_explicit_archive(service,body['archive_path'])
            else:
                service.catalog(profile.profile_id,service.activation)
                candidate = body['candidate_id']
                if not isinstance(candidate,str) or candidate not in service._catalog:
                    fail('INVALID_CANDIDATE')
            identifier = uuid.uuid4().hex
            while len(adapter.jobs) >= 32:
                oldest = next(iter(adapter.jobs))
                if adapter.view(adapter.jobs[oldest])['state'] in {'awaiting_key','running'}:
                    fail('BUSY')
                del adapter.jobs[oldest]
            job = dict(id=identifier,profile_id=profile.profile_id,service=service,service_id=None,
                       state='awaiting_key',candidate=candidate,destination=destination,created=time.monotonic())
            adapter.jobs[identifier] = job
            return adapter.view(job)

    @router.get('/jobs/{identifier}')
    def status(identifier: str,request: Request,response: Response):
        profile = guard(request,response)
        with closed_errors(),state_lock(adapter):
            if identifier not in adapter.jobs:
                return operations.status(adapter.service(profile),identifier)
            return adapter.view(adapter.lookup(profile,identifier))

    @router.post('/jobs/{identifier}/cancel')
    def cancel(identifier: str,request: Request,response: Response):
        profile = guard(request,response)
        with closed_errors(),state_lock(adapter):
            if identifier not in adapter.jobs:
                return operations.cancel(adapter.service(profile),identifier)
            job = adapter.lookup(profile,identifier)
            if job.get('pipeline_state'):
                fail('NOT_CANCELLABLE')
            if job['service_id']:
                job['service'].cancel(job['service_id'])
            else:
                job['state'] = 'cancelled'
            return adapter.view(job)

    @router.post('/jobs/{identifier}/key')
    async def key(identifier: str,request: Request,response: Response):
        profile = guard(request,response)
        if identifier not in adapter.jobs:
            return await operation_key(identifier,request,response)
        with closed_errors():
            with state_lock(adapter):
                job = adapter.lookup(profile,identifier)
                if adapter.view(job)['state'] != 'awaiting_key':
                    fail('INVALID_STATE')
            with operation_lock(profile):
                pass
            data = await bounded_body(request,MAX_BODY)
            try:
                identity = key_part(data,request.headers.get('content-type',''))
                with state_lock(adapter):
                    job = adapter.lookup(guard(request,response),identifier)
                    if adapter.view(job)['state'] != 'awaiting_key':
                        fail('INVALID_STATE')
                    job['service_id'] = job['service'].start(job['candidate'],identity=io.BytesIO(identity),
                        destination=job['destination'],profile_id=profile.profile_id,activation=job['service'].activation)
                    return adapter.view(job)
            finally:
                data[:] = b'\x00'*len(data)
                identity = None
    @router.post('/jobs/{identifier}/import-auto')
    async def import_auto(identifier: str,request: Request,response: Response):
        profile = guard(request,response)
        with closed_errors():
            raw = await bounded_body(request,4096)
            try:
                body = json.loads(raw)
            except (ValueError,UnicodeError):
                fail('INVALID_REQUEST',422)
            if (not isinstance(body,dict) or not {'profile_id','consent_restore'} <= set(body)
                    or set(body)-{'profile_id','consent_restore','acknowledge_incomplete'}
                    or body.get('consent_restore') is not True
                    or type(body.get('acknowledge_incomplete',False)) is not bool):
                fail('INVALID_REQUEST',422)
            if body['profile_id'] != profile.profile_id:
                fail('STALE_PROFILE')
            state=operations.status(operation_service(profile),identifier)
            return state.get('receipt') if state['state']=='succeeded' else state

    def import_auto_sync(profile,identifier,acknowledge_incomplete):
        from .recovery_pipeline import complete_restore
        # Short ownership admission only. No adapter state lock across SDK work.
        with closed_errors(),state_lock(adapter):
            job = adapter.lookup(profile,identifier)
            if job.get('backend_receipt'):
                return job['backend_receipt']
            if not job.get('auto_name') or job.get('pipeline_state'):
                fail('RECOVERY_INTERRUPTED' if job.get('pipeline_state') == 'interrupted' else 'BUSY')
            view = adapter.view(job)
            if not job['service_id'] or view['state'] != 'succeeded':
                fail('CONFIG_NOT_READY')
            if job['catalog_complete'] is True and view['complete'] is not True:
                fail('COMPLETENESS_CHANGED')
            if job['catalog_complete'] is not True and not acknowledge_incomplete:
                fail('INCOMPLETE_ACK_REQUIRED')
            job.update(pipeline_state='running',pipeline_phase='importing')
        def phase(value):
            with adapter.lock:
                job['pipeline_phase'] = value
        try:
            with closed_errors(),operation_lock(profile):
                result = complete_restore(job['service'],job['service_id'],
                    profile_id=profile.profile_id,activation=binding(profile),
                    name=job['auto_name'],acknowledge_incomplete=acknowledge_incomplete,phase=phase)
            with adapter.lock:
                job.update(backend_receipt=result,pipeline_state='succeeded',pipeline_phase='backend_target_ready')
            return result
        except BaseException:
            with adapter.lock:
                job.update(pipeline_state='interrupted',pipeline_phase=job.get('pipeline_phase','importing'))
            raise

    @router.post('/jobs/{identifier}/import')
    async def import_profile(identifier: str,request: Request,response: Response):
        profile = guard(request,response)
        fail('OPERATION_PROTOCOL_REQUIRED')
        with closed_errors():
            raw = await bounded_body(request,4096)
            try:
                body = json.loads(raw)
            except (ValueError,UnicodeError):
                fail('INVALID_REQUEST',422)
            if (not isinstance(body,dict) or not {'profile_id','name','consent_import'} <= set(body)
                    or set(body) - {'profile_id','name','consent_import','acknowledge_incomplete'}
                    or type(body.get('acknowledge_incomplete',False)) is not bool
                    or body.get('consent_import') is not True or not isinstance(body.get('name'),str)):
                fail('INVALID_REQUEST',422)
            if body['profile_id'] != profile.profile_id:
                fail('STALE_PROFILE')
            return await run_in_threadpool(import_sync,profile,identifier,body['name'],body.get('acknowledge_incomplete',False))

    def import_sync(profile,identifier,name,acknowledge_incomplete,automatic=False):
        with closed_errors(),state_lock(adapter),operation_lock(profile):
            job = adapter.lookup(profile,identifier)
            if job.get('auto_name') and (not automatic or name != job['auto_name']):
                fail('INVALID_STATE')
            if not job['service_id'] or not adapter.view(job)['can_import']:
                fail('CONFIG_NOT_READY')
            # Authority comes only from the authenticated owner and current server
            # binding. Neither request paths nor a client activation fence are used.
            result = RecoveryImporter(job['service']).import_job(job['service_id'],
                profile_id=profile.profile_id,activation=binding(profile),name=name,
                consent_import=True,acknowledge_incomplete=acknowledge_incomplete)
            from hermes_cli import profiles as host
            from .recovery_activation import profile_digest
            target = core.checked_path(host.get_profile_dir(name))
            job['imported'] = dict(name=name,target=target,digest=profile_digest(target),started=False)
            if automatic:
                # Keep the handoff until start has checked it. Failed/abandoned
                # starts get ten minutes to retry while this process is alive.
                def expire():
                    with adapter.lock:
                        try:
                            job['service'].cleanup_managed_result(job['service_id'])
                        except Exception:
                            # Unsafe identity changes must never authorize deletion.
                            job['cleanup_failed'] = True
                timer = threading.Timer(600,expire)
                timer.daemon = True
                job['cleanup_timer'] = timer
                timer.start()
            return result

    @router.post('/jobs/{identifier}/start')
    async def start_profile(identifier: str,request: Request,response: Response):
        profile=guard(request,response)
        fail('OPERATION_PROTOCOL_REQUIRED')
        with closed_errors():
            raw=await bounded_body(request,4096)
            try:
                body=json.loads(raw)
            except (ValueError,UnicodeError):
                fail('INVALID_REQUEST',422)
            if (not isinstance(body,dict) or set(body)!={'profile_id','name','consent_start'}
                    or body['consent_start'] is not True or not isinstance(body['name'],str)):
                fail('INVALID_REQUEST',422)
            if body['profile_id']!=profile.profile_id:
                fail('STALE_PROFILE')
            return await run_in_threadpool(start_sync,profile,identifier,body['name'])

    def start_sync(profile,identifier,name):
        from .recovery_activation import start_imported
        with closed_errors(),state_lock(adapter),operation_lock(profile):
            job=adapter.lookup(profile,identifier)
            service=job['service']
            if job.get('backend_receipt'):
                if name != job['auto_name']:
                    fail('INVALID_NAME')
                from .recovery_pipeline import _OWNED_TARGETS
                owned=_OWNED_TARGETS.get((id(service),job['service_id']))
                if owned is None:
                    fail('RECOVERY_INTERRUPTED')
                owned.ready(timeout=3)
                return job['backend_receipt']
            with service._lock:
                if not job.get('imported'):
                    fail('IMPORT_REQUIRED')
                service.handoff(job['service_id'],profile.profile_id,binding(profile))
                answer = start_imported(service,job['imported'],name)
                if job.get('auto_name'):
                    service.cleanup_managed_result(job['service_id'])
                    job.get('cleanup_timer',None) and job['cleanup_timer'].cancel()
                return answer

    # Default-only maintenance is headless, host-authenticated and review-gated.
    from .default_recovery_api import DefaultRecoveryAPI, authority, public_error
    default_api = DefaultRecoveryAPI()

    @contextmanager
    def default_errors():
        try:
            yield
        except HTTPException:
            raise
        except Exception as exc:
            fail(public_error(exc))

    async def default_body(request,profile,fields):
        raw = await bounded_body(request,4096)
        try:
            body=json.loads(raw,object_pairs_hook=__import__('importlib').import_module(__package__+'.default_recovery')._pairs)
        except Exception:
            fail('INVALID_REQUEST',422)
        if not isinstance(body,dict) or set(body) != {'profile_id'} | set(fields):
            fail('INVALID_REQUEST',422)
        if body['profile_id'] != profile.profile_id:
            fail('STALE_PROFILE')
        return body

    @router.get('/default/capabilities')
    def default_capabilities(request: Request,response: Response):
        profile=guard(request,response)
        refusal=None
        current_root_token=None
        from . import default_recovery_api as maintenance
        try:
            from . import default_recovery_lifecycle as life
            root=authority(profile)
            life.preflight(root,trusted_finix=True)
            if not life._exact(life.identify(root),root):
                refusal='OWNER_NOT_VERIFIED'
            else:
                current_root_token=maintenance.token({'root_identity': maintenance.cold._identity(root)})
        except Exception as exc:
            refusal=public_error(exc)
        # Read the single reviewed gate dynamically; never capture a second flag.
        return dict(code_version='default-recovery-v1',supported=refusal is None,refusal=refusal,
                    serverRootToken=current_root_token,
                    apply_enabled=maintenance.DEFAULT_APPLY_REVIEWED,active_shared_autoruns_supported=False,
                    explanation='Active shared autoruns, supervisors and sibling-serving multiplexers are unsupported; refusal occurs before shutdown.')

    @router.post('/default/prepare-auto')
    async def default_prepare_auto(request: Request,response: Response):
        profile=guard(request,response)
        with default_errors():
            authority(profile)
            body=await default_body(request,profile,{'candidate_id','consent_restore'})
            if body['consent_restore'] is not True or not isinstance(body['candidate_id'],str) or len(body['candidate_id'])>128:
                fail('INVALID_REQUEST',422)
            return await run_in_threadpool(prepare_auto_sync,profile,body,True)

    @router.post('/default/prepare')
    async def default_prepare(request: Request,response: Response):
        profile=guard(request,response)
        with default_errors():
            body=await default_body(request,profile,{'recovery_job','consent_prepare'})
            from .default_recovery_api import identifier as valid_id
            valid_id(body['recovery_job'])
            if body['consent_prepare'] is not True:
                fail('CONSENT_REQUIRED')
            def execute():
                with state_lock(adapter),operation_lock(profile):
                    job=adapter.lookup(profile,body['recovery_job'])
                    if not job['service_id'] or adapter.view(job)['state'] != 'succeeded':
                        fail('CONFIG_NOT_READY')
                    if adapter.view(job)['complete'] is not True:
                        fail('INCOMPLETE_ACK_REQUIRED')
                    return default_api.prepare(profile,job['service'],job['service_id'],consent=True)
            return await run_in_threadpool(execute)

    @router.post('/default/apply')
    async def default_apply(request: Request,response: Response):
        profile=guard(request,response)
        with default_errors():
            body=await default_body(request,profile,{'id','confirmation','consent_apply'})
            return await run_in_threadpool(default_api.apply,profile,body['id'],body['confirmation'],consent=body['consent_apply'])

    @router.get('/default/status/{identifier}')
    def default_status(identifier: str,request: Request,response: Response):
        profile=guard(request,response)
        with default_errors():
            return default_api.status(profile,identifier)

    @router.post('/default/reopen')
    async def default_reopen(request: Request,response: Response):
        profile=guard(request,response)
        with default_errors():
            body=await default_body(request,profile,{'id','consent_reopen'})
            return await run_in_threadpool(default_api.reopen,profile,body['id'],consent=body['consent_reopen'])

    router.recovery_adapter = adapter
    return router
