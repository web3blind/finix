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
from .recovery_import import RecoveryImportError
from .recovery_contract import public_job
from .setup_flow import SetupFlow
from starlette.concurrency import run_in_threadpool


@contextmanager
def operation_lock(profile,*,backend_lease=True,preview_job=None):
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
        if preview_job is None:
            from .default_preview_jobs import PreviewJobs
            PreviewJobs().assert_available(profile)
        if backend_lease:
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
    from .recovery_availability import capability, CODE
    from .recovery_operations import RecoveryOperations
    router = APIRouter(prefix='/recovery')
    adapter = RecoveryHTTP()
    operations = RecoveryOperations()
    router.recovery_operations = operations

    def guard(request, response):
        response.headers['Cache-Control'] = 'no-store'
        if not local_request(request):
            fail('LOCAL_DESKTOP_REQUIRED', 403)
        profile = current_profile()
        if not re.fullmatch(r'[a-f0-9]{64}', profile.profile_id):
            fail('PROFILE_UNAVAILABLE')
        return profile

    @contextmanager
    def closed_errors():
        try:
            yield
        except HTTPException:
            raise
        except (RecoveryError, RecoveryImportError) as exc:
            fail(exc.code, 404 if exc.code == 'NOT_FOUND' else 409)
        except Exception:
            fail('RESTORE_FAILED')

    @router.get('/capabilities')
    def capabilities(request: Request, response: Response):
        profile = guard(request, response)
        return dict(capability(), code_version='recovery-v1',
                    operation_protocol='recovery-operations-v1',
                    profile_id=profile.profile_id, local_only=True)

    @router.get('/default/capabilities')
    def default_capabilities(request: Request, response: Response):
        profile = guard(request, response)
        return dict(capability(), code_version='default-recovery-v1',
                    profile_id=profile.profile_id, local_only=True)

    @router.get('/operations/current')
    def operation_current(request: Request, response: Response):
        profile = guard(request, response)
        with closed_errors():
            return operations.current(adapter.service(profile))

    @router.get('/operations/{identifier}')
    def operation_status(identifier: str, request: Request, response: Response):
        profile = guard(request, response)
        with closed_errors():
            return operations.status(adapter.service(profile), identifier)

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


    async def unavailable(request: Request, response: Response):
        # Refuse before reading any body, key, path, or persisted operation.
        guard(request, response)
        fail(CODE)

    for path in (
        '/operations', '/operations/{identifier}/key', '/operations/{identifier}/cancel',
        '/prepare', '/prepare-auto', '/jobs/{identifier}/key',
        '/jobs/{identifier}/cancel', '/jobs/{identifier}/import',
        '/jobs/{identifier}/import-auto', '/jobs/{identifier}/start',
        '/default/prepare-auto', '/default/prepare', '/default/apply',
        '/default/reopen', '/default/preview/{identifier}/cancel',
    ):
        router.add_api_route(path, unavailable, methods=['POST'])
    for path in ('/jobs/{identifier}', '/default/status/{identifier}',
                 '/default/preview/{identifier}'):
        router.add_api_route(path, unavailable, methods=['GET'])
    return router
