"""Private subprocess entry point; credentials remain owned by Workspace."""
import importlib.util
import json
from pathlib import Path
import resource
import sys


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
            import os
            from googleapiclient.http import MediaIoBaseDownload
            output = args[args.index('--output') + 1]
            request = module.build_service('drive', 'v3').files().get_media(fileId=args[0])
            with open(output, 'xb') as stream:
                os.fchmod(stream.fileno(), 0o600)
                downloader = MediaIoBaseDownload(stream, request, chunksize=8 * 1024 * 1024)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
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
