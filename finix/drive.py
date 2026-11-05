"""Google Drive JSON-only encrypted object protocol via Hermes-managed OAuth.

The public host helper accepts JSON, not raw media bodies. Consequently Finix
stores bounded base64 ciphertext chunks in Drive file descriptions, plus a
commit descriptor. This is NOT Drive's media upload API. Limit: 1 MiB encrypted.
No refresh/access tokens are handled by Finix. No automatic login or redirects.
"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import time
import uuid
from urllib.parse import urlencode

from .core import FinixError, checked_path

BASE = 'https://www.googleapis.com/drive/v3/files'
CHUNK = 8192
MAX_PARTS = 128
MAX_CIPHER = CHUNK * MAX_PARTS
ID = re.compile(r'[A-Za-z0-9_-]{1,200}')


def managed_request(method, url, **kwargs):
    from hermes_cli.plugin_provider_requests import credentialed_provider_request
    return credentialed_provider_request('google', method, url, **kwargs)


class Drive:
    def __init__(self, profile_id, request=None, sleep=time.sleep):
        if not re.fullmatch(r'[a-f0-9]{64}', profile_id):
            raise FinixError('Invalid profile binding')
        self.owner = profile_id
        self.request = request or managed_request
        self.sleep = sleep

    def call(self, method, suffix='', payload=None):
        for attempt in range(3):
            try:
                response = self.request(method, BASE + suffix, json=payload, timeout=20)
            except Exception:
                # The host can raise transport/auth errors; never echo token/provider
                # response text through chat, scheduler logs or CLI diagnostics.
                raise FinixError('Drive request unavailable; check owner-terminal sign-in/network') from None
            if response.status not in {429, 500, 502, 503, 504}:
                break
            # Creation is never retried: an ambiguous POST can create duplicates.
            if method == 'POST' or attempt == 2:
                break
            self.sleep(0.25 * (2 ** attempt))
        if not 200 <= response.status < 300:
            raise FinixError(f'Drive request failed ({response.status})')
        if len(response.body) > 1024 * 1024:
            raise FinixError('Drive response exceeds limit')
        return response.json() if response.body else {}

    @staticmethod
    def identifier(value):
        if not isinstance(value, str) or not ID.fullmatch(value):
            raise FinixError('Invalid Drive identifier')
        return value

    def properties(self, kind, snapshot=''):
        return {'finix': '1', 'owner': self.owner, 'kind': kind, 'snapshot': snapshot}

    def create(self, name, kind, folder=None, description='', snapshot=''):
        data = {'name': name, 'mimeType': 'application/octet-stream',
                'description': description, 'appProperties': self.properties(kind, snapshot)}
        if folder:
            data['parents'] = [self.identifier(folder)]
        if kind == 'folder':
            data['mimeType'] = 'application/vnd.google-apps.folder'
        return self.identifier(self.call('POST', '?fields=id', data)['id'])

    def create_folder(self):
        return self.create('Finix encrypted backups', 'folder')

    def get(self, identifier):
        return self.call('GET', '/' + self.identifier(identifier) +
                         '?fields=id,name,mimeType,parents,appProperties,description,createdTime')

    def checked(self, identifier, kind, folder=None, snapshot=None):
        obj = self.get(identifier)
        props = obj.get('appProperties', {})
        if (props.get('finix') != '1' or props.get('owner') != self.owner
                or props.get('kind') != kind or (folder and folder not in obj.get('parents', []))
                or (snapshot is not None and props.get('snapshot') != snapshot)):
            raise FinixError('Drive object ownership mismatch')
        if kind == 'folder' and obj.get('mimeType') != 'application/vnd.google-apps.folder':
            raise FinixError('Not a Finix folder')
        return obj

    def upload(self, path, folder, keep=0):
        if type(keep) is not int or not 0 <= keep <= 1000:
            raise FinixError('Invalid retention')
        self.checked(folder, 'folder')
        path = checked_path(path)
        with path.open('rb') as stream:
            data = stream.read(MAX_CIPHER + 1)
        if not data or len(data) > MAX_CIPHER:
            raise FinixError('Drive JSON protocol supports at most 1 MiB encrypted per snapshot')
        snapshot = uuid.uuid4().hex
        parts = []
        for start in range(0, len(data), CHUNK):
            encoded = base64.b64encode(data[start:start + CHUNK]).decode('ascii')
            parts.append(self.create(f'finix-{snapshot}-part-{len(parts):03}', 'part', folder,
                                     encoded, snapshot))
        manifest = {'format': 'finix-drive-1', 'sha256': hashlib.sha256(data).hexdigest(),
                    'size': len(data), 'parts': parts}
        description = json.dumps(manifest, separators=(',', ':'))
        if len(description) > 16000:
            raise FinixError('Drive descriptor exceeds limit')
        identifier = self.create(path.name, 'snapshot', folder, description, snapshot)
        verified = self.download(folder, identifier)
        if verified != data:
            raise FinixError('Drive readback mismatch; retention not run')
        if keep:
            self.prune(folder, identifier, keep)
        return {'id': identifier, 'folder_id': folder, 'profile_id': self.owner,
                'sha256': manifest['sha256'], 'verified': True}

    def descriptor(self, obj):
        try:
            manifest = json.loads(obj['description'])
            parts = manifest['parts']
            if (manifest['format'] != 'finix-drive-1' or not isinstance(parts, list)
                    or not 1 <= len(parts) <= MAX_PARTS or len(set(parts)) != len(parts)
                    or type(manifest['size']) is not int or not 0 < manifest['size'] <= MAX_CIPHER
                    or not re.fullmatch(r'[a-f0-9]{64}', manifest['sha256'])):
                raise ValueError
            for part in parts:
                self.identifier(part)
            return manifest
        except (ValueError, KeyError, TypeError):
            raise FinixError('Invalid Drive descriptor') from None

    def download(self, folder, identifier):
        self.checked(folder, 'folder')
        obj = self.checked(identifier, 'snapshot', folder)
        manifest = self.descriptor(obj)
        data = bytearray()
        for part in manifest['parts']:
            chunk = self.checked(part, 'part', folder, obj['appProperties']['snapshot'])
            try:
                encoded = chunk['description']
                if len(encoded) > 11000:
                    raise ValueError
                decoded = base64.b64decode(encoded, validate=True)
            except (ValueError, KeyError, TypeError):
                raise FinixError('Invalid Drive chunk') from None
            if not 0 < len(decoded) <= CHUNK:
                raise FinixError('Drive chunk exceeds limit')
            data.extend(decoded)
        if len(data) != manifest['size'] or hashlib.sha256(data).hexdigest() != manifest['sha256']:
            raise FinixError('Drive readback hash mismatch; retention not run')
        return bytes(data)

    def snapshots(self, folder):
        query = (f"'{self.identifier(folder)}' in parents and trashed = false and "
                 "appProperties has { key='finix' and value='1' } and "
                 f"appProperties has {{ key='owner' and value='{self.owner}' }} and "
                 "appProperties has { key='kind' and value='snapshot' }")
        result, token = [], ''
        for _ in range(10):
            params = {'q': query, 'pageSize': 100, 'fields': 'nextPageToken,files(id,createdTime)',
                      'orderBy': 'createdTime desc', 'pageToken': token}
            response = self.call('GET', '?' + urlencode(params))
            result.extend(response.get('files', []))
            token = response.get('nextPageToken', '')
            if not token:
                return result
        raise FinixError('Drive inventory exceeds 1000 objects; retention aborted')

    def prune(self, folder, current, keep):
        # Pin current as retained regardless of Drive ordering/time lag.
        others = [o for o in self.snapshots(folder) if o['id'] != current]
        for record in others[max(0, keep - 1):]:
            obj = self.checked(record['id'], 'snapshot', folder)
            manifest = self.descriptor(obj)
            snapshot = obj['appProperties']['snapshot']
            # Validate every candidate before deleting even the first chunk.
            for part in manifest['parts']:
                self.checked(part, 'part', folder, snapshot)
            for identifier in [*manifest['parts'], record['id']]:
                self.call('DELETE', '/' + self.identifier(identifier))


def main():
    parser = argparse.ArgumentParser(description='Download a Finix Drive ciphertext; no private key needed')
    parser.add_argument('--profile-id', required=True, help='Original Finix profile_id from status')
    parser.add_argument('--folder', required=True)
    parser.add_argument('--object', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    data = Drive(args.profile_id).download(args.folder, args.object)
    path = checked_path(args.output)
    with path.open('xb') as stream:
        import os
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data)
    print(json.dumps({'output': str(path), 'sha256': hashlib.sha256(data).hexdigest()}))


if __name__ == '__main__':
    main()
