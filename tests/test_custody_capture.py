"""Capture regressions use real producer DATA, never live rollback authority."""
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from finix import core, streaming


class CustodyCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = core.private_dir(Path(self.tmp.name) / 'source')

    def crafted(self, mode):
        base = core.private_dir(self.root / 'recovery-custody' / ('a' * 32))
        folder = core.private_dir(base / 'copies')
        name = 'state.db-unknown' if mode == 'unknown' else 'state.db'
        blob = folder / hashlib.sha256(name.encode()).hexdigest()
        db = sqlite3.connect(blob)
        try:
            db.execute('create table example(x)')
            db.commit()
        finally:
            db.close()
        leaf = dict(identity='NOT_A_NATIVE_IDENTITY', size=blob.stat().st_size,
                    sha256=hashlib.sha256(blob.read_bytes()).hexdigest())
        m = dict(version=1, policy='runtime-attempt-preserve-v1',
                 plan_digest='b' * 64, current={name: leaf})
        if mode == 'foreign':
            m.update(root_identity=['foreign', 999], parents={'/foreign': ['wrong']}, old={}, unexpected=True)
        if mode == 'unknown':
            leaf['identity'] = [1, 2]
            m.update(root_identity=[1, 3], parents={'/historical': [1, 3]},
                     current={**{n: None for n in ('state.db', 'state.db-wal', 'state.db-shm', 'state.db-journal')}, name: leaf})
            m['old'] = dict.fromkeys(m['current'])
        (base / 'execution-manifest.json').write_text(json.dumps(m))
        return blob

    def test_complete_handwritten_manifest_raw_sqlite_never_bypasses(self):
        base = core.private_dir(self.root / 'recovery-custody' / ('a' * 32))
        folder = core.private_dir(base / 'copies')
        blob = folder / hashlib.sha256(b'state.db').hexdigest()
        db = sqlite3.connect(blob)
        db.execute('create table example(x)'); db.commit(); db.close()
        objects = core.private_dir(base / 'objects')
        shutil.copyfile(blob, objects / blob.name)
        current = dict.fromkeys(('state.db', 'state.db-wal', 'state.db-shm', 'state.db-journal'))
        current['state.db'] = dict(identity=[1, 2], size=blob.stat().st_size,
                                    sha256=core.digest_file(blob))
        m = dict(version=2, policy='runtime-attempt-preserve-v1', storage_format='opaque-container-v2',
                 namespace='a'*32, plan_digest='b'*64, root_identity=[1, 3], parents={'/historical':[1, 3]},
                 current=current, old=dict.fromkeys(current))
        # Even a fully typed, self-consistent v2 manifest cannot turn raw SQL into DATA.
        m['stored'] = {kind+'/'+blob.name: dict(size=blob.stat().st_size, sha256=core.digest_file(blob))
                       for kind in ('copies','objects')}
        m['current']['state.db']['size'] -= 56
        p = dict(version=2, manifest_digest=hashlib.sha256(json.dumps(m,sort_keys=True,separators=(',', ':')).encode()).hexdigest(),
                 state='restored', pending=None, restore={})
        for mode in ('v2', 'v1'):
            if mode == 'v1':
                m.pop('storage_format'); m.pop('namespace'); m.pop('stored'); m['version']=1; p['version']=1
                m['current']['state.db']['size'] += 56
                p['manifest_digest']=hashlib.sha256(json.dumps(m,sort_keys=True,separators=(',', ':')).encode()).hexdigest()
            (base/'execution-manifest.json').write_text(json.dumps(m))
            (base/'progress.json').write_text(json.dumps(p))
            with self.subTest(mode=mode), patch.object(streaming, '_connect_source', side_effect=AssertionError('SQLITE_OPEN')) as opening:
                with core.locked(self.root) as root, self.assertRaises(core.FinixError):
                    streaming.write_archive(root, io.BytesIO())
                self.assertEqual(opening.call_count, 0)

    def test_handwritten_valid_container_is_only_data(self):
        from finix import custody_payload as container
        base = core.private_dir(self.root/'recovery-custody'/('b'*32))
        raw = self.root/'raw-fixture'; raw.write_bytes(b'SQLite format 3\x00' + b'not a real database')
        leaf = dict(identity=[99, 42], size=raw.stat().st_size, sha256=core.digest_file(raw))
        current = dict.fromkeys(('state.db','state.db-wal','state.db-shm','state.db-journal'))
        current['state.db']=leaf
        stored={}
        for kind in ('copies','objects'):
            blob=core.private_dir(base/kind)/hashlib.sha256(b'state.db').hexdigest()
            out=io.BytesIO()
            with core.regular_input(raw) as fd: stored[kind+'/'+blob.name]=container.encode(fd,leaf,out)
            blob.write_bytes(out.getvalue())
        raw.unlink()
        m=dict(version=2,policy='runtime-attempt-preserve-v1',storage_format=container.STORAGE_FORMAT,
               namespace=base.name,stored=stored,plan_digest='c'*64,root_identity=[1,3],parents={'/historical':[1,3]},
               current=current,old=dict.fromkeys(current))
        p=dict(version=2,manifest_digest=container._digest(m),state='restored',pending=None,restore={})
        (base/'execution-manifest.json').write_text(json.dumps(m));(base/'progress.json').write_text(json.dumps(p))
        with patch.object(streaming,'_connect_source',side_effect=AssertionError('SQLITE_OPEN')) as opening:
            with core.locked(self.root) as root:
                inventory=core.scan(root)
                self.assertTrue(all(not fp.sqlite for fp in inventory.values()))
                streaming.write_archive(root,io.BytesIO())
            self.assertEqual(opening.call_count,0)

    def test_container_bounds_digest_truncation_and_extra_bytes(self):
        from finix import custody_payload as container
        import struct
        raw=self.root/'raw';raw.write_bytes(b'SQLite format 3\x00'+bytes(range(256)))
        logical=dict(size=raw.stat().st_size,sha256=core.digest_file(raw))
        encoded=io.BytesIO()
        with core.regular_input(raw) as fd: stored=container.encode(fd,logical,encoded)
        data=encoded.getvalue(); self.assertEqual(stored['size'],len(data))
        bad=dict(magic=b'X'+data[1:],version=data[:12]+b'\xff'+data[13:],header=data[:55],
                 truncation=data[:-1],extra=data+b'X',digest=data[:24]+bytes([data[24]^1])+data[25:],
                 payload=data[:-1]+bytes([data[-1]^1]),
                 oversize=data[:16]+struct.pack('>Q',container.MAX_PAYLOAD+1)+data[24:])
        blob=self.root/'blob'
        for label,value in bad.items():
            with self.subTest(label=label):
                blob.write_bytes(value)
                with core.regular_input(blob) as fd, self.assertRaises(ValueError):container.decode(fd,io.BytesIO())
        blob.write_bytes(data)
        with core.regular_input(blob) as fd:
            decoded=io.BytesIO(); actual,representation=container.decode(fd,decoded)
        self.assertEqual(decoded.getvalue(),raw.read_bytes());self.assertEqual(actual,logical);self.assertEqual(representation,stored)

    def test_partial_manifest_cannot_bypass_sqlite(self):
        self.crafted('partial')
        with core.locked(self.root) as root:
            with self.assertRaises(core.FinixError):
                core.scan(root)

    def test_foreign_untyped_manifest_cannot_bypass_sqlite(self):
        self.crafted('foreign')
        with core.locked(self.root) as root:
            with self.assertRaises(core.FinixError):
                core.scan(root)

    def test_unknown_auxiliary_cannot_bypass_sqlite(self):
        self.crafted('unknown')
        with core.locked(self.root) as root:
            with self.assertRaises(core.FinixError):
                core.scan(root)


class ProducerCaptureTests:
    """Mixin for the existing isolated real producer fixture."""
    def custody_export(self):
        from finix import runtime_attempt
        self.old_database()
        plan = self.prepare(self.capability())
        plan.apply(plan.consent())
        with patch.object(runtime_attempt, 'require_stopped_attempt', return_value=None):
            plan.rollback_attempt(plan.consent(), None)
        base = Path(plan._plan()['runtime_attempt']['custody'])
        export = core.private_dir(self.directory / 'capture-export')
        for p in base.rglob('*'):
            if p.is_file():
                target = export / p.relative_to(self.root)
                core.private_dir(target.parent)
                shutil.copyfile(p, target)
        return export, export / base.relative_to(self.root)

    def test_v2_container_is_ordinary_data_and_decodes_exactly(self):
        from finix import custody_payload
        export, base = self.custody_export()
        manifest = json.loads((base/'execution-manifest.json').read_text())
        self.assertEqual(manifest['version'], 2)
        self.assertEqual(manifest['namespace'], base.name)
        for kind in ('old', 'copies', 'objects'):
            for blob in (base/kind).iterdir():
                raw = io.BytesIO()
                with core.regular_input(blob) as fd:
                    logical, stored = custody_payload.decode(fd, raw)
                self.assertNotEqual(blob.read_bytes()[:16], b'SQLite format 3\x00')
                name = next(n for n in manifest['current'] if hashlib.sha256(n.encode()).hexdigest()==blob.name)
                leaf = manifest['old' if kind=='old' else 'current'][name]
                self.assertEqual((logical['size'], logical['sha256']), (leaf['size'], leaf['sha256']))
                self.assertEqual(hashlib.sha256(raw.getvalue()).hexdigest(), leaf['sha256'])
                self.assertEqual(stored, manifest['stored'][kind+'/'+blob.name])
        with patch.object(streaming, '_connect_source', side_effect=AssertionError('SQLITE_OPEN')) as opening:
            with core.locked(export) as root:
                found = core.scan(root)
                self.assertTrue(all(not fp.sqlite for fp in found.values()))
                streaming.write_archive(root, io.BytesIO())
            self.assertEqual(opening.call_count, 0)

    def test_strict_metadata_private_owner_and_link_controls(self):
        export, base = self.custody_export()
        from finix import custody_payload as container
        manifest=base/'execution-manifest.json'; progress=base/'progress.json'
        original_m=manifest.read_bytes();original_p=progress.read_bytes()
        for mode in ('duplicate','nativeid','schema','namespace','plan','unknownaux'):
            m=json.loads(original_m);p=json.loads(original_p)
            if mode=='nativeid':m['current']['state.db']['identity']='historical-not-native'
            elif mode=='schema':m['extra']=True
            elif mode=='namespace':m['namespace']='e'*32
            elif mode=='plan':m['plan_digest']='not-a-digest'
            elif mode=='unknownaux':m['current']['state.db-foreign']=None;m['old']['state.db-foreign']=None
            p['manifest_digest']=container._digest(m)
            data=json.dumps(m)
            if mode=='duplicate':data=data[:-1]+',"version":2}'
            manifest.write_text(data);progress.write_text(json.dumps(p))
            with self.subTest(mode=mode),core.locked(export) as root,self.assertRaises(core.FinixError):core.scan(root)
        manifest.write_bytes(original_m);progress.write_bytes(original_p)
        blob=base/'copies'/hashlib.sha256(b'state.db').hexdigest()
        spare=self.directory/'custody-link';os.link(blob,spare)
        with core.locked(export) as root,self.assertRaises((core.FinixError,OSError)):core.scan(root)
        spare.unlink()
        if os.name=='nt':
            from finix import windows_native as win
            C,W=win.C,win.W;descriptor=C.c_void_p()
            convert=win.api(win.A,'ConvertStringSecurityDescriptorToSecurityDescriptorW',W.BOOL,W.LPCWSTR,W.DWORD,C.POINTER(C.c_void_p),C.c_void_p)
            sddl='D:P(A;;FA;;;'+win.current_sid()+')(A;;FR;;;WD)'
            self.assertTrue(convert(sddl,1,C.byref(descriptor),None))
            try:
                setting=win.api(win.A,'SetFileSecurityW',W.BOOL,W.LPCWSTR,W.DWORD,C.c_void_p)
                self.assertTrue(setting(str(blob),4|0x80000000,descriptor))
            finally:win.free(descriptor)
        else:blob.chmod(0o644)
        with core.locked(export) as root,self.assertRaises((core.FinixError,OSError)):core.scan(root)

    def test_complete_manifest_generation_is_bound(self):
        export, base = self.custody_export()
        blob = base / 'copies' / hashlib.sha256(b'state.db').hexdigest()
        name = blob.relative_to(export).as_posix()
        with core.locked(export) as root:
            inventory = core.scan(root)
            m = json.loads((base / 'execution-manifest.json').read_text())
            m['plan_digest'] = 'd' * 64
            (base / 'execution-manifest.json').write_text(json.dumps(m))
            progress = json.loads((base / 'progress.json').read_text())
            progress['manifest_digest'] = hashlib.sha256(json.dumps(m, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            (base / 'progress.json').write_text(json.dumps(progress))
            with self.assertRaisesRegex(core.FinixError, '^CUSTODY_CHANGED$'):
                with streaming.file_payload(root, name, inventory[name]):
                    pass

    def test_missing_required_blob_before_scan_is_fatal(self):
        export, base = self.custody_export()
        (base / 'copies' / hashlib.sha256(b'state.db').hexdigest()).unlink()
        with core.locked(export) as root:
            with self.assertRaises(core.FinixError):
                core.scan(root)

    def test_missing_required_blob_after_scan_is_fatal(self):
        export, base = self.custody_export()
        blob = base / 'copies' / hashlib.sha256(b'state.db').hexdigest()
        real_scan = core.scan
        def vanish(root, guard=None):
            inventory = real_scan(root, guard)
            blob.unlink()
            return inventory
        with core.locked(export) as root, patch.object(core, 'scan', side_effect=vanish):
            with self.assertRaises(core.FinixError):
                streaming.write_archive(root, io.BytesIO())

    def test_manifest_disappearance_is_fatal_not_omitted(self):
        export, base = self.custody_export()
        real_scan = core.scan
        def vanish(root, guard=None):
            inventory = real_scan(root, guard)
            (base / 'execution-manifest.json').unlink()
            return inventory
        with core.locked(export) as root, patch.object(core, 'scan', side_effect=vanish):
            with self.assertRaises(core.FinixError):
                streaming.write_archive(root, io.BytesIO())

    def test_progress_disappearance_is_fatal_not_omitted(self):
        export, base = self.custody_export()
        real_scan = core.scan
        def vanish(root, guard=None):
            inventory = real_scan(root, guard)
            (base / 'progress.json').unlink()
            return inventory
        with core.locked(export) as root, patch.object(core, 'scan', side_effect=vanish):
            with self.assertRaises(core.FinixError):
                streaming.write_archive(root, io.BytesIO())


class ProducerCustodyCaptureTests(ProducerCaptureTests, unittest.TestCase):
    """Compose the existing fixture without collecting its unrelated tests twice."""
    def setUp(self):
        from test_runtime_attempt_preservation import AttemptTests
        self.fixture = AttemptTests('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)

    def __getattr__(self, name):
        fixture = self.__dict__.get('fixture')
        if fixture is None:
            raise AttributeError(name)
        return getattr(fixture, name)
