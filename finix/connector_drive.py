"""Fail-closed Hermes managed googledrive adapter, distinct from plugin OAuth.

No vendor names are guessed. A code-reviewed codec must pin the discovered input
schemas AND output semantics before execution is admitted. The production codec
registry is intentionally empty until authenticated live schema review is possible.
"""
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from . import core, cloud_files

MAX_CIPHER = 20 * 1024 * 1024  # gateway wire.CONNECTOR_FILE_MAX_BYTES
MAX_RECORDS = 1000
REVIEWED_CONTRACTS = ()
ACTIONS = {
    'NO_TOKEN': 'No managed token available in this profile. Use the owner Hermes Connectors UI/owner terminal to authorize explicitly, then recheck. No login or identity was started.',
    'AUTH_POLICY_UNSUPPORTED': 'This SDK can refresh/replace guest identities and fall back to root auth. Finix refuses that path. A profile-local managed token scope or a future no-create/no-fallback SDK is required; do not send tokens in chat.',
    'ACCOUNT_REQUIRED': 'Explicitly configure connector_account (connection ID), connector_alias and connector_folder from the selected profile owner Connectors UI.',
    'ACCOUNT_MISMATCH': 'Selected googledrive account is missing, disabled, inactive or its alias changed. Select and authorize that exact account in the owner Connectors UI; no other account will be used.',
    'UNSUPPORTED_SCHEMA': 'Live schema/output mapping is not reviewed for this SDK. Storage remains blocked; authenticated discovery and a reviewed codec are required. No vendor tool was executed.',
    'CONNECTOR_UNAVAILABLE': 'The active managed gateway catalogue does not advertise enabled googledrive. A source-code hint is not availability; choose another configured storage backend explicitly.',
    'NO_SCHEMA': 'Discovery returned no usable googledrive schemas. Check the selected account and connector availability in the owner Connectors UI.',
    'SDK_UNAVAILABLE': 'Run inside a compatible Hermes runtime with the built-in managed ConnectorClient SDK.',
    'MANAGED_ERROR': 'Managed gateway request failed. Check the owner Connectors UI and network; no fallback/login is attempted.',
}


class ConnectorError(core.FinixError):
    def __init__(self, code):
        self.code = code if code in ACTIONS else 'MANAGED_ERROR'
        super().__init__(f'{self.code}: {ACTIONS[self.code]}')


@contextmanager
def profile_scope(home):
    """Bind all three SDK selectors; a scope miss cannot borrow launch secrets."""
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from agent.secret_scope import (build_profile_secret_scope, set_secret_scope, reset_secret_scope,
                                    set_multiplex_context, reset_multiplex_context)
    home = core.checked_path(home)
    h = set_hermes_home_override(home)
    m = set_multiplex_context(True)
    s = None
    try:
        s = set_secret_scope(build_profile_secret_scope(home), profile_home=str(home))
        yield
    finally:
        if s is not None:
            reset_secret_scope(s)
        reset_multiplex_context(m)
        reset_hermes_home_override(h)


class StorageTransport:
    """SDK transport seam: no ambient proxy/netrc, redirects or gateway auth."""
    def request(self, method, url, **kwargs):
        import requests
        session = requests.Session()
        session.trust_env = False
        # Response owns the connection until SDK get_file closes it.
        return session.request(method, url, allow_redirects=False, **kwargs)


def sdk_client(home):
    from tools.connectors.gateway.client import ConnectorClient
    from tools.managed_gateway_auth import managed_gateway_auth_headers
    from tools.managed_tool_gateway import peek_nous_access_token
    from agent.secret_scope import current_secret_scope

    def headers(url):
        # Delegate credential handling to SDK; never extract/store bearer values.
        # The SDK's default reader may mint a replacement guest. Its cheap reader
        # is read-only, but can borrow root auth. Permit only the scoped override,
        # which short-circuits auth-store resolution inside that public reader.
        scope = current_secret_scope() or {}
        if not scope.get('TOOL_GATEWAY_USER_TOKEN'):
            if (Path(home) / 'auth.json').exists():
                raise ConnectorError('AUTH_POLICY_UNSUPPORTED')
            return {}
        return managed_gateway_auth_headers(url, token_reader=peek_nous_access_token)

    return ConnectorClient(header_provider=headers, storage=StorageTransport())


class ManagedDrive:
    def __init__(self, home, profile_id, account, alias, folder, *, client=None):
        if not re.fullmatch(r'[a-f0-9]{64}', profile_id):
            raise core.FinixError('Invalid Finix profile binding')
        if not all(isinstance(x, str) and re.fullmatch(r'[A-Za-z0-9_.@-]{1,200}', x)
                   for x in (account, alias, folder)):
            raise ConnectorError('ACCOUNT_REQUIRED')
        self.home, self.owner = core.checked_path(home), profile_id
        self.account, self.alias, self.folder = account, alias, folder
        self.client, self.contract = client, None

    @contextmanager
    def session(self):
        try:
            with profile_scope(self.home):
                if self.client is None:
                    self.client = sdk_client(self.home)
                yield
        except ConnectorError:
            raise
        except ImportError:
            raise ConnectorError('SDK_UNAVAILABLE') from None
        except core.FinixError:
            raise
        except Exception as exc:
            raise ConnectorError('NO_TOKEN' if getattr(exc, 'code', '') == 'NO_TOKEN' else 'MANAGED_ERROR') from None

    def check_account(self):
        row = self.client.account_status(self.account, timeout=20)
        if (not row or row.get('connectionId') != self.account or row.get('connector') != 'googledrive'
                or row.get('alias') != self.alias or row.get('status') != 'active'
                or row.get('disabled')):
            raise ConnectorError('ACCOUNT_MISMATCH')

    def discover(self):
        catalogue = self.client.list_connectors(timeout=20)
        available = [row for row in catalogue if row.get('connector') == 'googledrive' and row.get('enabled')]
        if len(available) != 1:
            raise ConnectorError('CONNECTOR_UNAVAILABLE')
        self.disabled_tools = set(available[0].get('disabledTools', []))
        self.check_account()
        result = self.client.search([{'use_case': 'googledrive: upload binary file, read metadata, download binary file and delete an owned file',
                                      'known_fields': 'connector googledrive; explicit connector_alias'}])
        names = list(dict.fromkeys(name for row in result.get('results', []) for name in row.get('tools', [])))
        if not names or len(names) > 25 or not all(isinstance(n, str) and len(n) <= 250 for n in names):
            raise ConnectorError('NO_SCHEMA')
        schemas = self.client.schemas(names).get('schemas', {})
        self.schemas = {name: schema for name, schema in schemas.items()
                        if schema.get('connector') == 'googledrive' and schema.get('input_schema')}
        if not self.schemas:
            raise ConnectorError('NO_SCHEMA')
        self.contract = next((c for c in REVIEWED_CONTRACTS if c.schemas == self.schemas), None)
        if self.contract is None:
            raise ConnectorError('UNSUPPORTED_SCHEMA')

    def status(self):
        code = 'READY'
        try:
            with self.session():
                self.discover()
        except ConnectorError as exc:
            code = exc.code
        return {'ready': code == 'READY', 'code': code, 'action': ACTIONS.get(code, 'Reviewed contract admitted.'),
                'connector': 'googledrive', 'account': self.account, 'alias': self.alias,
                'schema_count': len(getattr(self, 'schemas', {})), 'live_google_verified': False,
                'max_cipher_bytes': MAX_CIPHER, 'large_archives_supported': False,
                'production_codec_available': bool(REVIEWED_CONTRACTS),
                'limitation': 'Experimental unavailable backend: actual managed gateway cap is 20 MiB; choose workspace, drive or rclone for large archives.'}

    def execute(self, operation, payload):
        from tools.connectors.gateway.merge import PlannedCall
        self.check_account()
        tool, arguments = self.contract.encode(operation, payload)
        if (tool not in {s['tool'] for s in self.schemas.values()} or tool in self.disabled_tools
                or 'connector_alias' in arguments):
            raise ConnectorError('UNSUPPORTED_SCHEMA')
        arguments = dict(arguments, connector_alias=self.alias)
        result = self.client.execute([PlannedCall(0, f'connectors__googledrive__{tool}', 'googledrive', tool, arguments)])
        if len(result) != 1 or result[0].get('error') or result[0].get('account') != self.alias:
            raise ConnectorError('ACCOUNT_MISMATCH' if result and result[0].get('account') != self.alias else 'MANAGED_ERROR')
        return self.contract.decode(operation, result[0].get('data'))

    def storage_url(self, url):
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or parsed.hostname not in self.contract.storage_hosts
                or parsed.username or parsed.password or parsed.fragment or parsed.port not in {None, 443}
                or len(url) > 8192 or any(ord(c) <= 32 for c in url)):
            raise core.FinixError('Unsafe connector storage URL')
        return url

    def metadata(self, identifier, descriptor):
        obj = self.execute('metadata', {'id': identifier})
        if obj != dict(descriptor, id=identifier):
            raise core.FinixError('Managed object ownership/metadata mismatch')
        return obj

    def read_media(self, identifier, descriptor):
        obj = self.execute('download', {'id': identifier})
        buffer = io.BytesIO()
        self.client.get_file(self.storage_url(obj['url']), buffer.write, max_bytes=MAX_CIPHER)
        data = buffer.getvalue()
        if len(data) != descriptor['size'] or hashlib.sha256(data).hexdigest() != descriptor['sha256']:
            raise core.FinixError('Managed readback mismatch; no retention')
        return data

    def checked_descriptor(self, descriptor):
        fields = {'owner', 'folder', 'snapshot', 'name', 'size', 'sha256'}
        obj = {key: descriptor[key] for key in fields}
        if (obj['owner'] != self.owner or obj['folder'] != self.folder
                or type(obj['size']) is not int or not 0 < obj['size'] <= MAX_CIPHER
                or not re.fullmatch(r'[a-f0-9]{64}', obj['sha256'])
                or not re.fullmatch(r'[a-f0-9]{32}', obj['snapshot'])
                or not re.fullmatch(r'[A-Za-z0-9_.-]{1,200}', obj['name'])):
            raise core.FinixError('Invalid managed descriptor')
        return obj

    def download(self, identifier, descriptor):
        with self.session():
            self.discover()
            descriptor = self.checked_descriptor(descriptor)
            self.metadata(identifier, descriptor)
            return self.read_media(identifier, descriptor)

    def upload(self, path, state, keep=0, *, completion=None):
        import uuid
        if type(keep) is not int or not 0 <= keep <= MAX_RECORDS:
            raise core.FinixError('Invalid managed retention')
        path, state = core.checked_path(path), core.private_dir(state)
        with self.session(), core.locked(state):
            self.discover()  # no reading ciphertext/staging on unsupported contract
            with core.regular_input(path) as fd:
                data = core.read_regular(fd, MAX_CIPHER)
            if not data.startswith(b'age-encryption.org/v1\n'):
                raise core.FinixError('Only native age ciphertext may be uploaded')
            binding = {'owner': self.owner, 'account': self.account, 'alias': self.alias, 'folder': self.folder}
            journal_path = state / 'connector.json'
            journal = dict(binding, records=[])
            if journal_path.exists():
                with core.regular_input(journal_path) as fd:
                    journal = json.loads(core.read_regular(fd, 1024 * 1024))
                if any(journal.get(k) != v for k, v in binding.items()):
                    raise core.FinixError('Managed ledger belongs to a different account/profile/folder')
            if not isinstance(journal.get('records'), list) or len(journal['records']) >= MAX_RECORDS:
                raise core.FinixError('Managed retention ledger limit; no upload')
            descriptor = dict(owner=self.owner, folder=self.folder, snapshot=uuid.uuid4().hex,
                              name=path.name, size=len(data), sha256=hashlib.sha256(data).hexdigest())
            self.checked_descriptor(descriptor)
            staged = self.client.stage_file(toolkit='googledrive', tool=self.contract.upload_tool,
                filename=path.name, mimetype='application/octet-stream', size=len(data))
            self.client.put_file(self.storage_url(staged['upload_url']), staged['headers'], data)
            obj = self.execute('upload', {'metadata': descriptor, 'file': staged['descriptor']})
            identifier = obj['id']
            self.metadata(identifier, descriptor)
            self.read_media(identifier, descriptor)
            record = dict(descriptor, id=identifier,
                          completion=cloud_files.completion_record(descriptor, completion))
            journal['records'].append(record)
            core.atomic_json(journal_path, journal)
            if keep:
                for old in cloud_files.retention_candidates(journal['records'], keep):
                    previous = self.checked_descriptor(old)
                    obj = self.execute('metadata', {'id': old['id']})
                    if obj is not None:
                        if obj != dict(previous, id=old['id']):
                            raise core.FinixError('Managed retention ownership mismatch')
                        self.read_media(old['id'], previous)
                        self.execute('delete', {'id': old['id']})
                        if self.execute('metadata', {'id': old['id']}) is not None:
                            raise core.FinixError('Managed deletion readback failed')
                    journal['records'].remove(old)
                    core.atomic_json(journal_path, journal)
            return dict(record, verified=True, account=self.account, alias=self.alias)


def main(argv=None):
    """Independent ciphertext download; never accepts a recovery identity."""
    import argparse
    import sys
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['status', 'download'])
    for name in ('home', 'profile-id', 'account', 'alias', 'folder'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--object')
    parser.add_argument('--descriptor', help='Saved verified upload record JSON (not a key)')
    parser.add_argument('--output', help='New ciphertext file; existing files are never overwritten')
    args = parser.parse_args(argv)
    try:
        drive = ManagedDrive(args.home, args.profile_id, args.account, args.alias, args.folder)
        if args.command == 'status':
            result = drive.status()
            print(json.dumps(result, sort_keys=True))
            return 0 if result['ready'] else 2
        if not all((args.object, args.descriptor, args.output)):
            parser.error('download requires --object, --descriptor and --output')
        fd = os.open(core.checked_path(args.descriptor), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            descriptor = json.loads(core.read_regular(fd, 65536))
        finally:
            os.close(fd)
        if (descriptor.get('id') != args.object or descriptor.get('account') != args.account
                or descriptor.get('alias') != args.alias):
            raise core.FinixError('Recovery descriptor account/object mismatch')
        data = drive.download(args.object, descriptor)
        output = core.checked_path(args.output)
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if core.digest_file(output) != descriptor['sha256']:
                raise core.FinixError('Local ciphertext readback mismatch')
        except BaseException:
            output.unlink(missing_ok=True)
            raise
        print(json.dumps({'output': str(output), 'sha256': descriptor['sha256'], 'verified': True}))
        return 0
    except ConnectorError as exc:
        print(json.dumps({'ready': False, 'code': exc.code, 'action': ACTIONS[exc.code]}), file=sys.stderr)
        return 2
    except (core.FinixError, OSError, ValueError, KeyError, TypeError):
        print('finix managed connector: operation failed; no secret diagnostics emitted', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
