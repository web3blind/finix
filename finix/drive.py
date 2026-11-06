"""Standard Drive media files using Hermes-managed OAuth and resumable sessions.

Only initiation/metadata/media GETs use managed credentials. The provider-issued
upload URI is a secret capability, used in memory for one raw unauthenticated PUT.
Never persist it or include it (or provider response text) in diagnostics.
"""
import argparse

import http.client
import json
import os
from pathlib import Path
import re
import tempfile
import time
import uuid
from urllib.parse import parse_qs, urlencode, urlsplit

from .core import FinixError, checked_path
from . import cloud_files

BASE = 'https://www.googleapis.com/drive/v3/files'
UPLOAD = 'https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable'
MEDIA_CHUNK = 8 * 1024 * 1024
ID = re.compile(r'[A-Za-z0-9_-]{1,200}')
JSON_LIMIT = 1024 * 1024


def managed_request(method, url, **kwargs):
    from hermes_cli.plugin_provider_requests import credentialed_provider_request
    return credentialed_provider_request('google', method, url, **kwargs)


def session_target(uri):
    """Accept only the documented Google upload endpoint, never arbitrary URLs."""
    try:
        if not isinstance(uri, str) or not 1 <= len(uri) <= 8192 or any(
                ord(c) <= 32 or ord(c) >= 127 for c in uri):
            raise ValueError
        url = urlsplit(uri)
        query = parse_qs(url.query, keep_blank_values=True, strict_parsing=True)
        if (url.scheme != 'https' or url.netloc not in {'www.googleapis.com', 'www.googleapis.com:443'}
                or url.path != '/upload/drive/v3/files' or url.fragment or '#' in uri
                or set(query) != {'uploadType', 'upload_id'}
                or query['uploadType'] != ['resumable'] or len(query['upload_id']) != 1
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,4096}', query['upload_id'][0])):
            raise ValueError
        return url.path + '?' + url.query
    except (ValueError, TypeError):
        raise FinixError('Unsafe Drive upload session; upload aborted') from None


def upload_media(uri, path, size):
    target = session_target(uri)
    # http.client neither redirects nor uses environment proxies/netrc, and has
    # no HTTPX request logger that might print the secret session query string.
    connection = http.client.HTTPSConnection('www.googleapis.com', port=443, timeout=120)
    connection.set_debuglevel(0)
    try:
        with path.open('rb') as stream:
            offset = 0
            while offset < size:
                data = stream.read(min(MEDIA_CHUNK, size - offset))
                if not data:
                    raise ValueError
                end = offset + len(data) - 1
                connection.request('PUT', target, body=data, headers={
                    'Content-Type': 'application/octet-stream', 'Content-Length': str(len(data)),
                    'Content-Range': f'bytes {offset}-{end}/{size}'})
                response = connection.getresponse()
                body = response.read(JSON_LIMIT + 1)
                if len(body) > JSON_LIMIT:
                    raise ValueError
                if end + 1 < size:
                    if response.status != 308 or response.getheader('Range') != f'bytes=0-{end}':
                        raise ValueError
                else:
                    if response.status not in {200, 201}:
                        raise ValueError
                    result = json.loads(body)
                    if not isinstance(result, dict) or stream.read(1):
                        raise ValueError
                    return result
                offset = end + 1
    except Exception:
        raise FinixError('Drive media upload failed; local backup preserved') from None
    finally:
        connection.close()


class Drive:
    def __init__(self, profile_id, request=None, sleep=time.sleep):
        if not re.fullmatch(r'[a-f0-9]{64}', profile_id):
            raise FinixError('Invalid profile binding')
        self.owner = profile_id
        self.request = request or managed_request
        self.sleep = sleep

    def response(self, method, url, payload=None, headers=None, limit=JSON_LIMIT, missing_ok=False):
        for attempt in range(3):
            try:
                response = self.request(method, url, json=payload, headers=headers, timeout=120 if '?alt=media' in url else 20)
            except Exception:
                raise FinixError('Drive request unavailable; check owner-terminal sign-in/network') from None
            if response.status not in {429, 500, 502, 503, 504}:
                break
            # No retry of ambiguous creation or upload sessions.
            if method == 'POST' or attempt == 2:
                break
            self.sleep(0.25 * (2 ** attempt))
        if missing_ok and response.status == 404:
            return None
        if not 200 <= response.status < 300:
            raise FinixError(f'Drive request failed ({response.status})')
        if len(response.body) > limit:
            raise FinixError('Drive response exceeds limit')
        return response

    def call(self, method, suffix='', payload=None, missing_ok=False):
        response = self.response(method, BASE + suffix, payload, missing_ok=missing_ok)
        if response is None:
            return None
        try:
            result = response.json() if response.body else {}
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (ValueError, TypeError):
            raise FinixError('Invalid Drive JSON response') from None

    @staticmethod
    def identifier(value):
        if not isinstance(value, str) or not ID.fullmatch(value):
            raise FinixError('Invalid Drive identifier')
        return value

    def properties(self, kind, snapshot=''):
        return {'finix': '1', 'owner': self.owner, 'kind': kind, 'snapshot': snapshot}

    def create(self, name, kind, folder=None, description='', snapshot=''):
        data = {'name': name, 'mimeType': 'application/octet-stream',
                'appProperties': self.properties(kind, snapshot)}
        if description:
            data['description'] = description
        if folder:
            data['parents'] = [self.identifier(folder)]
        if kind == 'folder':
            data['mimeType'] = 'application/vnd.google-apps.folder'
        return self.identifier(self.call('POST', '?fields=id', data).get('id'))

    def create_folder(self):
        return self.create('Finix encrypted backups', 'folder')

    def get(self, identifier, missing_ok=False):
        return self.call('GET', '/' + self.identifier(identifier) +
                         '?fields=id,name,mimeType,parents,appProperties,createdTime', missing_ok=missing_ok)

    def checked(self, identifier, kind, folder=None, snapshot=None, missing_ok=False):
        obj = self.get(identifier, missing_ok=missing_ok)
        if obj is None and missing_ok:
            return None
        props = obj.get('appProperties', {})
        if (obj.get('id') != identifier or props.get('finix') != '1' or props.get('owner') != self.owner
                or props.get('kind') != kind or (folder and folder not in obj.get('parents', []))
                or (snapshot is not None and props.get('snapshot') != snapshot)):
            raise FinixError('Drive object ownership mismatch')
        mime = 'application/vnd.google-apps.folder' if kind == 'folder' else 'application/octet-stream'
        if obj.get('mimeType') != mime:
            raise FinixError('Invalid Finix Drive file type')
        return obj

    def upload(self, path, folder, keep=0):
        if type(keep) is not int or not 0 <= keep <= 1000:
            raise FinixError('Invalid retention')
        self.checked(folder, 'folder')
        path = checked_path(path)
        descriptor = cloud_files.describe(path, native=True)
        snapshot = uuid.uuid4().hex
        props = self.properties('pending', snapshot)
        props.update(sha256=descriptor['sha256'], size=str(descriptor['size']))
        metadata = {'name': path.name, 'mimeType': 'application/octet-stream',
                    'parents': [self.identifier(folder)], 'appProperties': props}
        initiated = self.response('POST', UPLOAD, metadata, {
            'X-Upload-Content-Type': 'application/octet-stream', 'X-Upload-Content-Length': props['size']})
        uri = next((v for k, v in initiated.headers.items() if k.lower() == 'location'), None)
        with tempfile.TemporaryDirectory(prefix='finix-drive-upload-', dir=os.environ.get('TMPDIR')) as stage:
            staged = Path(stage) / 'cipher.age'
            cloud_files.copy(path, staged, descriptor)
            completed = upload_media(uri, staged, descriptor['size'])
            cloud_files.verified(staged, descriptor)
        identifier = self.identifier(completed.get('id'))
        obj = self.checked(identifier, 'pending', folder, snapshot)
        if obj['appProperties'] != props:
            raise FinixError('Drive uploaded metadata mismatch; retention not run')
        self.read_media(identifier, self.descriptor(obj))
        props = dict(props, kind='snapshot')
        self.call('PATCH', '/' + identifier, {'appProperties': props})
        committed = self.checked(identifier, 'snapshot', folder, snapshot)
        if committed['appProperties'] != props:
            raise FinixError('Drive commit readback mismatch; retention not run')
        if keep:
            self.prune(folder, identifier, keep)
        return {'id': identifier, 'folder_id': folder, 'profile_id': self.owner,
                'sha256': props['sha256'], 'verified': True}

    def descriptor(self, obj):
        try:
            props = obj['appProperties']
            size = int(props['size'])
            if (size <= 0 or str(size) != props['size']
                    or not re.fullmatch(r'[a-f0-9]{64}', props['sha256'])
                    or not re.fullmatch(r'[a-f0-9]{32}', props['snapshot'])):
                raise ValueError
            return {'size': size, 'sha256': props['sha256']}
        except (ValueError, KeyError, TypeError):
            raise FinixError('Invalid Drive descriptor') from None

    def read_media(self, identifier, manifest, output=None):
        with tempfile.TemporaryDirectory(prefix='finix-drive-read-', dir=os.environ.get('TMPDIR')) as stage:
            path = Path(stage) / 'cipher.age'
            with path.open('xb') as stream:
                os.fchmod(stream.fileno(), 0o600)
                for offset in range(0, manifest['size'], MEDIA_CHUNK):
                    end = min(offset + MEDIA_CHUNK, manifest['size']) - 1
                    response = self.response('GET', BASE + '/' + self.identifier(identifier) + '?alt=media',
                        headers={'Range': f'bytes={offset}-{end}', 'Accept-Encoding': 'identity'}, limit=end-offset+1)
                    headers = {k.lower(): v for k, v in response.headers.items()}
                    if (response.status != 206 or headers.get('content-range') != f"bytes {offset}-{end}/{manifest['size']}"
                            or len(response.body) != end-offset+1):
                        raise FinixError('Drive range response mismatch; retention not run')
                    stream.write(response.body)
            return cloud_files.verified(path, manifest, output)

    def download(self, folder, identifier, output=None):
        self.checked(folder, 'folder')
        obj = self.checked(identifier, 'snapshot', folder)
        return self.read_media(identifier, self.descriptor(obj), output)

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
            if not token and len(result) <= 1000:
                return result
        raise FinixError('Drive inventory exceeds 1000 objects; retention aborted')

    def prune(self, folder, current, keep):
        # Pin current regardless of Drive ordering/time lag; check every deletion
        # against actual metadata, never trust search results as ownership proof.
        others = [o for o in self.snapshots(folder) if o['id'] != current]
        for record in others[max(0, keep - 1):]:
            obj = self.checked(record['id'], 'snapshot', folder, missing_ok=True)
            if obj is None:
                continue
            self.descriptor(obj)
            self.call('DELETE', '/' + self.identifier(record['id']), missing_ok=True)
            if self.get(record['id'], missing_ok=True) is not None:
                raise FinixError('Drive deletion readback failed; retry retention later')


def main():
    parser = argparse.ArgumentParser(description='Download a Finix Drive ciphertext; no private key needed')
    parser.add_argument('--profile-id', required=True, help='Original Finix profile_id from status')
    parser.add_argument('--folder', required=True)
    parser.add_argument('--object', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    path = checked_path(args.output)
    result = Drive(args.profile_id).download(args.folder, args.object, path)
    print(json.dumps(dict(result, output=str(path))))


if __name__ == '__main__':
    main()
