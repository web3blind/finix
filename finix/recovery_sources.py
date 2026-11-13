"""Read-only recovery capabilities from existing exact committed journals.
No OAuth, folder creation, remote enumeration, retention or adoption by name.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
from . import core
from .recovery import RecoveryError, committed_journal_source


def read_journal(path):
    try:
        with core.regular_input(path) as fd:
            value = json.loads(core.read_regular(fd,1024*1024))
    except FileNotFoundError:
        return None
    if not isinstance(value,dict) or not isinstance(value.get('records'),list):
        raise RecoveryError('INVALID_SOURCE')
    return value


def generation(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def register_cloud(service,profile):
    cfg = profile.config()
    kind = cfg.get('storage')
    if kind == 'rclone':
        adapter = profile.rclone(cfg)
        path = adapter.state/'rclone.json'
        saved = read_journal(path)
        if saved is None:
            return
        expected = dict(profile_id=profile.profile_id,binding=adapter.binding(),namespace=saved.get('namespace'))
        def validate(row):
            adapter.descriptor(row)
            return row['namespace'] == expected['namespace'] and row['type'] == expected['binding']['type']
        def download(row,output,cancel):
            if adapter.binding() != expected['binding']:
                raise RecoveryError('INVALID_SOURCE')
            metadata = json.loads(adapter.runner('lsjson',adapter.object_path(row),'--stat'))
            if (metadata.get('IsDir') is not False or metadata.get('Size') != row['size']
                    or metadata.get('Name') != row['id']):
                raise RecoveryError('INTEGRITY_FAILED')
            # Runner caps bytes and kills on sink failure. Service's sink checks
            # cancellation per write and verifies the ENTIRE digest before age.
            adapter.runner('cat',adapter.object_path(row),output=output,expected_size=row['size'])
            if adapter.binding() != expected['binding']:
                raise RecoveryError('INVALID_SOURCE')
    elif kind == 'workspace':
        adapter = profile.workspace()
        path = adapter.state/'workspace.json'
        saved = read_journal(path)
        if saved is None:
            return
        transport = cfg.get('workspace_transport', 'finix')
        authority = {'finix': 'finix:google', 'skill': 'google-workspace-skill:drive'}.get(transport)
        if (authority is None or saved.get('credential_authority') != authority
                or getattr(adapter, 'credential_authority', 'finix:google') != authority):
            return  # Different transports never adopt each other's journals.
        selected_account = cfg.get('workspace_account')
        if not selected_account or saved.get('account') != selected_account:
            return  # An old journal cannot select or replace the applied account.
        expected = {key:saved.get(key) for key in
                    ('profile_id','folder_id','owners','account','binding','credential_authority')}
        if expected['profile_id'] != profile.profile_id or not expected['owners']:
            raise RecoveryError('INVALID_SOURCE')
        if expected['binding'] != adapter._binding():
            raise RecoveryError('INVALID_SOURCE')
        from .workspace_drive import identifier
        identifier(expected['folder_id'])
        def selected():
            current = profile.config()
            if (current.get('storage') != 'workspace'
                    or current.get('workspace_transport', 'finix') != transport
                    or current.get('workspace_account') != selected_account
                    or adapter._binding() != expected['binding']):
                raise RecoveryError('INVALID_SOURCE')
        def validate(row):
            selected()
            adapter.descriptor(row)
            return row['folder_id'] == expected['folder_id'] and row.get('owners') == expected['owners']
        def download(row,output,cancel):
            selected()
            # Ciphertext only; all staging explicitly under the excluded cache.
            core.private_dir(service.stage_root)
            with tempfile.TemporaryDirectory(dir=service.stage_root,prefix='cipher-') as directory:
                target = Path(directory)/'cipher.age'
                adapter.download(row, target, cancel=cancel)
                selected()
                adapter._account(cancel=cancel)
                with core.regular_input(target) as fd:
                    while chunk := os.read(fd,1024*1024):
                        if cancel():
                            raise RecoveryError('CANCELLED')
                        output.write(chunk)
    elif kind == 'connector-drive':
        return  # unsupported backend; preserve its existing settings/journal
    elif kind == 'drive':
        # Legacy Drive upload does not persist an exact local commit journal.
        # Do NOT scan Drive or manufacture candidates from names/metadata. New
        # host-reviewed commits may use this bound journal; old copies remain
        # recoverable via the explicitly selected local ciphertext workflow.
        from .drive import Drive
        adapter = Drive(profile.profile_id, home=profile.home)
        path = profile.root/'drive-index/drive.json'
        saved = read_journal(path)
        if saved is None:
            return
        folder = adapter.identifier(cfg.get('folder_id'))
        expected = dict(profile_id=profile.profile_id,folder_id=folder)
        def validate(row):
            adapter.identifier(row.get('id'))
            return (row.get('profile_id') == profile.profile_id and row.get('folder_id') == folder
                    and isinstance(row.get('snapshot'),str) and len(row['snapshot']) == 32)
        def download(row,output,cancel):
            from .drive import BASE, MEDIA_CHUNK
            adapter.checked(folder,'folder')
            obj = adapter.checked(row['id'],'snapshot',folder,row['snapshot'])
            if adapter.descriptor(obj) != {k:row[k] for k in ('size','sha256')}:
                raise RecoveryError('INTEGRITY_FAILED')
            for start in range(0,row['size'],MEDIA_CHUNK):
                if cancel():
                    raise RecoveryError('CANCELLED')
                end = min(start+MEDIA_CHUNK,row['size'])-1
                response = adapter.response('GET',BASE+'/'+row['id']+'?alt=media',
                    headers={'Range':f'bytes={start}-{end}','Accept-Encoding':'identity'},limit=end-start+1)
                headers = {k.lower():v for k,v in response.headers.items()}
                if (response.status != 206 or headers.get('content-range') != f"bytes {start}-{end}/{row['size']}"
                        or len(response.body) != end-start+1):
                    raise RecoveryError('INTEGRITY_FAILED')
                output.write(response.body)
    else:
        return
    # Binding includes namespace/account/root; service re-reads exact records
    # before and after transfer. Missing journals never cause provider calls.
    validate_provider = validate
    def bounded_validate(row):
        if type(row.get('size')) is not int or not 0 < row['size'] <= 1024**4:
            return False
        return validate_provider(row)
    if kind == 'drive':
        transfer = download
        def download(row,output,cancel):
            # A new worker thread must not inherit ambient/default OAuth scope.
            from .drive import profile_scope
            with profile_scope(profile.home):
                transfer(row,output,cancel)
    source = committed_journal_source(connection=kind,generation=generation([expected,service.activation]),journal=path,
        expected_binding=expected,validate_record=bounded_validate,download=download)
    service.add_source(source)
