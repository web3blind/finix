"""Private subprocess entry point; credentials remain owned by Workspace."""
import importlib.util
import json
from pathlib import Path
import resource
import sys
import os
import re


MEDIA_CHUNK = 8 * 1024 * 1024
READ_CHUNK = 64 * 1024


def _download(module, file_id, output, expected_size):
    """Stream only validated ranges; never let a buffered SDK consume media.

    The existing Workspace module owns credential loading/refresh. Its credential
    object stays in this private process, including when used by AuthorizedSession.
    The caller enforces the total transfer deadline and removes failed downloads.
    """
    from google.auth.transport.requests import AuthorizedSession

    if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', file_id) or expected_size <= 0:
        raise ValueError('Invalid download descriptor')
    url = 'https://www.googleapis.com/drive/v3/files/' + file_id
    with AuthorizedSession(module.get_credentials()) as session:
        session.trust_env = False
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as sink:
            start = 0
            while start < expected_size:
                end = min(start + MEDIA_CHUNK, expected_size) - 1
                headers = {'Range': f'bytes={start}-{end}', 'Accept-Encoding': 'identity'}
                with session.get(url, params={'alt': 'media'}, headers=headers,
                                 stream=True, allow_redirects=False, timeout=(15, 60)) as response:
                    # Reject ignored ranges/redirects before touching the body,
                    # even when Content-Length advertises an enormous response.
                    if (response.status_code != 206
                            or response.headers.get('Content-Range') != f'bytes {start}-{end}/{expected_size}'
                            or response.headers.get('Content-Encoding', 'identity').lower() != 'identity'):
                        raise ValueError('Invalid media range response')
                    length = end - start + 1
                    declared = response.headers.get('Content-Length')
                    if declared is not None and declared != str(length):
                        raise ValueError('Invalid media range length')
                    received = 0
                    for block in response.iter_content(chunk_size=READ_CHUNK):
                        if len(block) > READ_CHUNK or received + len(block) > length:
                            raise ValueError('Media range overflow')
                        sink.write(block)
                        received += len(block)
                    if received != length:
                        raise ValueError('Incomplete media range')
                start = end + 1


def main():
    script, limit, operation, *args = sys.argv[1:]
    resource.setrlimit(resource.RLIMIT_FSIZE, (int(limit), int(limit)))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    # A fresh interpreter binds the wrapper's import-time HERMES_HOME correctly.
    sys.path.insert(0, str(Path(script).parent))
    spec = importlib.util.spec_from_file_location('finix_workspace_api', script)
    if spec is None or spec.loader is None:
        raise RuntimeError('Workspace helper cannot be loaded')
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        # Python is required for media even when gws exists. Use that same
        # credential-owning path for metadata/trash; no gws token export/copy.
        module._gws_binary = lambda: None
        if operation == 'metadata':
            result = module.build_service('drive', 'v3').files().get(
                fileId=args[0], fields='id,name,mimeType,size,parents,owners(emailAddress),trashed').execute()
            print(json.dumps(result))
        elif operation == 'download':
            output = args[args.index('--output') + 1]
            _download(module, args[0], output, int(limit))
            print(json.dumps({'id': args[0], 'status': 'downloaded'}))
        elif operation == 'upload':
            from googleapiclient.http import MediaFileUpload
            media = MediaFileUpload(args[0], mimetype='application/octet-stream',
                                    chunksize=8 * 1024 * 1024, resumable=True)
            try:
                result = module.build_service('drive', 'v3').files().create(
                    body={'name': args[args.index('--name') + 1], 'parents': [args[args.index('--parent') + 1]]},
                    media_body=media, fields='id,mimeType').execute()
            finally:
                media.stream().close()
            print(json.dumps(dict(result, status='uploaded')))
        else:
            sys.argv = [script, 'drive', operation, *args]
            module.main()
    except Exception as exc:
        status = getattr(getattr(exc, 'resp', None), 'status', None)
        code = 'NOT_FOUND' if status == 404 else 'PERMISSION' if status == 403 else 'PROVIDER_ERROR'
        print(json.dumps({'finix_error': code}))
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
