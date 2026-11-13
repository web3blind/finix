"""Isolated Google API operations; authorization belongs entirely to the skill.

This is an OPTIONAL external-skill compatibility contract, not the Hermes SDK
finix:google provider contract. No SDK authorization internals are imported.
"""
import hashlib
import re


class SkillError(Exception):
    """Closed, non-provider diagnostic code (never include Google response text)."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def perform(service, request, *, upload_factory=None, download_factory=None):
    """Use the SAME service for requester preflight and the requested operation."""
    account = service.about().get(fields='user(permissionId)').execute(num_retries=0)['user']['permissionId']
    if not isinstance(account, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', account):
        raise SkillError('INVALID_IDENTITY')
    if request['operation'] == 'identity':
        return {'account_id': account}
    if request.get('account_pin') != hashlib.sha256(account.encode()).hexdigest():
        raise SkillError('ACCOUNT_CHANGED')
    if request['operation'] == 'generate-id':
        value = service.files().generateIds(count=1, space='drive', type='files').execute(num_retries=0)
        return {'id': identifier(value['ids'][0])}
    if request['operation'] == 'create-folder':
        args = request['args']
        body = {'id': identifier(option(args, '--id')), 'name': name(option(args, '--name')),
                'mimeType': FOLDER_MIME, 'parents': ['root']}
        value = mutation(lambda: service.files().create(body=body, fields='id').execute(num_retries=0))
        if not isinstance(value, dict) or value.get('id') != body['id']:
            raise SkillError('UNCERTAIN_ACK')
        return dict(status='created', id=body['id'])
    args = request['args']
    if request['operation'] == 'metadata':
        return service.files().get(fileId=identifier(args[0]), fields=FIELDS).execute(num_retries=0)
    if request['operation'] == 'upload':
        from finix import core
        if option(args, '--mime-type') != MIME:
            raise SkillError('INVALID_REQUEST')
        body = {'id': identifier(option(args, '--id')), 'name': name(option(args, '--name')),
                'parents': [identifier(option(args, '--parent'))], 'mimeType': MIME}
        if upload_factory is None:
            from googleapiclient.http import MediaIoBaseUpload
            upload_factory = MediaIoBaseUpload
        with core.regular_input(args[0]) as fd:
            import os
            with os.fdopen(os.dup(fd), 'rb') as stream:
                media = upload_factory(stream, mimetype=MIME, chunksize=CHUNK, resumable=True)
                upload = service.files().create(body=body, media_body=media, fields='id,mimeType')
                result = None
                while result is None:
                    _, result = mutation(lambda: upload.next_chunk(num_retries=0))
        if not isinstance(result, dict) or result.get('id') != body['id'] or result.get('mimeType') != MIME:
            raise SkillError('UNCERTAIN_ACK')
        return dict(result, status='uploaded')
    if request['operation'] == 'download':
        import os
        from finix import core
        size = request.get('expected_size')
        if type(size) is not int or size <= 0:
            raise SkillError('INVALID_REQUEST')
        if download_factory is None:
            from googleapiclient.http import MediaIoBaseDownload
            download_factory = MediaIoBaseDownload
        output = core.checked_path(option(args, '--output'))
        # Existing output is never overwritten, including after uncertain failure.
        with core.regular_output(output) as stream:
            sink = BoundedSink(stream, size)
            downloader = download_factory(sink, service.files().get_media(fileId=identifier(args[0])), chunksize=CHUNK)
            if isinstance(service, BoundService):
                downloader = BoundService(downloader, service.check)
            done = False
            while not done:
                _, done = downloader.next_chunk(num_retries=0)
            if sink.count != size:
                raise SkillError('INVALID_RESPONSE')
        return dict(status='downloaded', id=args[0])
    if request['operation'] == 'delete':
        obj = identifier(args[0])
        result = mutation(lambda: service.files().update(fileId=obj, body={'trashed': True}, fields='id,trashed').execute(num_retries=0))
        if not isinstance(result, dict) or result.get('id') != obj or result.get('trashed') is not True:
            raise SkillError('UNCERTAIN_ACK')
        return dict(status='trashed', fileId=obj, permanent=False)
    if request['operation'] == 'list':
        # Inventory is informational, never authority to adopt or prune a file.
        folder = identifier(args[0])
        rows, token, seen, ids = [], None, set(), set()
        for _ in range(100):
            options = dict(q=f"'{folder}' in parents and trashed = false", spaces='drive',
                           pageSize=100, fields='nextPageToken,incompleteSearch,files(' + FIELDS + ')')
            if token is not None:
                options['pageToken'] = token
            result = service.files().list(**options).execute(num_retries=0)
            page = result.get('files')
            if result.get('incompleteSearch') is True or not isinstance(page, list) or len(page) > 100:
                raise SkillError('INVALID_RESPONSE')
            for row in page:
                obj = identifier(row.get('id'))
                if obj in ids:
                    raise SkillError('INVALID_RESPONSE')
                ids.add(obj)
            rows.extend(page)
            if len(rows) > 1000:
                raise SkillError('RESPONSE_LIMIT')
            token = result.get('nextPageToken')
            if not token:
                return {'files': rows}
            if not isinstance(token, str) or len(token) > 4096 or token in seen:
                raise SkillError('INVALID_RESPONSE')
            seen.add(token)
        raise SkillError('RESPONSE_LIMIT')
    raise SkillError('UNSUPPORTED_OPERATION')


def mutation(call):
    try:
        return call()
    except Exception:
        raise SkillError('UNCERTAIN_ACK') from None


class BoundedSink:
    """Disk bound, NOT a promise of a pre-buffer network response cap."""
    def __init__(self, stream, limit):
        self.stream, self.limit, self.count = stream, limit, 0

    def write(self, data):
        if self.count + len(data) > self.limit:
            raise SkillError('RESPONSE_LIMIT')
        import os
        view = memoryview(data)
        while view:
            written = os.write(self.stream, view)
            if written <= 0:
                raise SkillError('INVALID_RESPONSE')
            view = view[written:]
        self.count += len(data)
        return len(data)


MIME = 'application/octet-stream'
FOLDER_MIME = 'application/vnd.google-apps.folder'
FIELDS = 'id,name,mimeType,size,parents,owners(emailAddress),trashed'
CHUNK = 8 * 1024 * 1024


def option(args, key):
    if args.count(key) != 1:
        raise SkillError('INVALID_REQUEST')
    return args[args.index(key) + 1]


def name(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 255 or any(ord(c) < 32 for c in value):
        raise SkillError('INVALID_REQUEST')
    return value


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', value):
        raise SkillError('INVALID_RESPONSE')
    return value


class BoundService:
    """Fence each service/request method, including every media chunk.

    A change during a remote mutation remains uncertain; no retry/adoption is
    authorized by a response from a call whose post-check failed.
    """
    def __init__(self, target, check):
        self.target, self.check = target, check

    def __getattr__(self, key):
        value = getattr(self.target, key)
        if not callable(value):
            return value
        def call(*args, **kwargs):
            self.check()
            result = value(*args, **kwargs)
            self.check()
            if not isinstance(result, (dict, list, tuple, str, bytes, int, float, bool, type(None))) and any(
                    callable(getattr(result, method, None)) for method in ('execute', 'next_chunk', 'files', 'get')):
                return BoundService(result, self.check)
            return result
        return call


def main(*, service_factory=None):
    """Single request, closed JSON response, zero provider diagnostic output."""
    import contextlib
    import json
    import logging
    import os
    from pathlib import Path
    import socket
    import sys
    import threading
    timer = threading.Timer(1805, lambda: os._exit(124))
    timer.daemon = True
    timer.start()
    service = None
    binding = None
    reply = {'error': 'PROVIDER_FAILED'}
    try:
        line = sys.stdin.buffer.readline(65537)
        if len(line) > 65536 or not line.endswith(b'\n'):
            raise SkillError('INVALID_REQUEST')
        request = json.loads(line)
        if os.name != 'nt':
            import resource
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            resource.setrlimit(resource.RLIMIT_AS, (768*1024*1024, 768*1024*1024))
            resource.setrlimit(resource.RLIMIT_FSIZE, (max(request.get('expected_size') or 0, 1048576),)*2)
        socket.setdefaulttimeout(30)
        logging.disable(logging.CRITICAL)
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from finix.workspace_skill_transport import load_skill, NativeBinding
        if Path(request['home']).resolve(strict=True) != Path(os.environ['HERMES_HOME']).resolve(strict=True):
            raise SkillError('PROFILE_CHANGED')
        binding = NativeBinding(request['home'], request.get('binding'))
        binding.__enter__()  # Validate parent-selected native identities BEFORE imports/credentials.
        os.environ['HERMES_HOME'] = str(binding.credential_home())
        with open(os.devnull, 'w') as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
            binding.check()
            if service_factory is None:
                module = load_skill(request['skill'])
                binding.check()
                service = module.build_service('drive', 'v3')
            else:
                service = service_factory(request['skill'])
            binding.check()
            value = perform(BoundService(service, binding.check), request)
            binding.check()
            if len(json.dumps(value).encode()) > 1000000:
                raise SkillError('RESPONSE_LIMIT')
            reply = {'result': value}
    except BaseException as exc:
        if isinstance(exc, SkillError):
            reply = {'error': exc.code}
        elif isinstance(exc, (ModuleNotFoundError, SystemExit)):
            reply = {'error': 'SKILL_SETUP_REQUIRED'}
        elif getattr(exc, 'code', None) in {'SKILL_CONTRACT_CHANGED', 'PROFILE_CHANGED', 'BINDING_CHANGED'}:
            reply = {'error': exc.code}
        elif getattr(getattr(exc, 'resp', None), 'status', None) == 404:
            reply = {'error': 'NOT_FOUND'}
    finally:
        if service is not None:
            try:
                with open(os.devnull, 'w') as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
                    service.close()
            except BaseException:
                pass
        if binding is not None:
            binding.__exit__(None, None, None)
        timer.cancel()
    sys.stdout.write(json.dumps(reply) + '\n')
    sys.stdout.flush()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
