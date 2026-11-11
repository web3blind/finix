"""OWNER_RIGHTS compatibility; only new owned fixtures and synthetic descriptors."""
import os
import unittest
from pathlib import Path
from contextlib import contextmanager
from unittest.mock import patch

@unittest.skipUnless(os.name == 'nt', 'native Windows only')
class OwnerRightsTests(unittest.TestCase):
    @contextmanager
    def descriptor(self, sddl):
        from finix import windows_native as w
        sd = w.C.c_void_p()
        convert = w.api(w.A, 'ConvertStringSecurityDescriptorToSecurityDescriptorW', w.W.BOOL,
                        w.W.LPCWSTR, w.W.DWORD, w.C.POINTER(w.C.c_void_p), w.C.c_void_p)
        self.assertTrue(bool(convert(sddl, 1, w.C.byref(sd), None)))
        try:
            yield sd
        finally:
            w.free(sd)

    def test_stock_sdk_private_directory_and_inherited_children(self):
        from finix import windows_native as w
        from hermes_constants import get_default_hermes_root, get_hermes_home
        from hermes_cli.profiles import get_profile_dir
        root = Path(os.environ['FINIX_OWNER_FIXTURE'])
        self.assertEqual(Path(get_default_hermes_root()).resolve(), root.resolve())
        self.assertEqual(Path(get_hermes_home()).resolve(), root.resolve())
        self.assertEqual(Path(get_profile_dir('fixture')).resolve(), (root / 'profiles/fixture').resolve())
        from hermes_dir_acl import make_user_dir
        path = root / 'stock-private'
        self.assertFalse(path.exists())
        make_user_dir(path, mode=0o700, elevated=False)
        (path / 'child').mkdir()
        (path / 'file').write_bytes(b'synthetic')
        # Verify actual native producer, not merely a hand-built approximation.
        get = w.api(w.A, 'GetSecurityInfo', w.W.DWORD, w.W.HANDLE, w.C.c_int, w.W.DWORD,
                    w.C.POINTER(w.C.c_void_p), w.C.c_void_p, w.C.POINTER(w.C.c_void_p), w.C.c_void_p, w.C.POINTER(w.C.c_void_p))
        for name, directory in [(path, True), (path/'child', True), (path/'file', False)]:
            with w.directory(name if directory else name.parent) as anchor:
                h = anchor.handle if directory else w.relative_open(anchor.handle, name.name)
                try:
                    owner, dacl, sd = w.C.c_void_p(), w.C.c_void_p(), w.C.c_void_p()
                    self.assertEqual(get(h,1,5,w.C.byref(owner),None,w.C.byref(dacl),None,w.C.byref(sd)),0)
                    try:
                        self.assertTrue(w.sid_text(owner) == w.current_sid())
                        class ACL(w.C.Structure):
                            _fields_=[('revision',w.W.BYTE),('reserved',w.W.BYTE),('size',w.W.WORD),('count',w.W.WORD),('reserved2',w.W.WORD)]
                        ace=w.C.c_void_p(); principals=[]
                        getace=w.api(w.A,'GetAce',w.W.BOOL,w.C.c_void_p,w.W.DWORD,w.C.POINTER(w.C.c_void_p))
                        for i in range(w.C.cast(dacl,w.C.POINTER(ACL)).contents.count):
                            self.assertTrue(bool(getace(dacl,i,w.C.byref(ace))))
                            principals.append(w.sid_text(ace.value+8))
                            self.assertTrue(w.C.string_at(ace, 1) == b'\x00')
                            self.assertEqual(w.C.cast(ace.value+4,w.C.POINTER(w.W.DWORD)).contents.value, 0x1f01ff)
                        self.assertEqual(len(principals), 3)
                        self.assertTrue(set(principals)=={'S-1-5-18','S-1-5-32-544','S-1-3-4'})
                    finally:
                        w.free(sd)
                    self.assertTrue(w.trusted_handle(h, private=True))
                    self.assertTrue(w.trusted_handle(h))
                finally:
                    if not directory: w.close(h)

    def test_native_authz_owner_relative_data_access(self):
        from finix import windows_native as w
        C,W=w.C,w.W
        z=C.WinDLL('authz',use_last_error=True)
        init=w.api(z,'AuthzInitializeResourceManager',W.BOOL,W.DWORD,C.c_void_p,C.c_void_p,C.c_void_p,W.LPCWSTR,C.POINTER(W.HANDLE))
        context=w.api(z,'AuthzInitializeContextFromSid',W.BOOL,W.DWORD,C.c_void_p,W.HANDLE,C.c_void_p,C.c_ulonglong,C.c_void_p,C.POINTER(W.HANDLE))
        class Request(C.Structure):
            _fields_=[('desired',W.DWORD),('principal',C.c_void_p),('types',C.c_void_p),('count',W.DWORD),('optional',C.c_void_p)]
        class Reply(C.Structure):
            _fields_=[('count',W.DWORD),('granted',C.POINTER(W.DWORD)),('sacl',C.POINTER(W.DWORD)),('error',C.POINTER(W.DWORD))]
        check=w.api(z,'AuthzAccessCheck',W.BOOL,W.DWORD,W.HANDLE,C.POINTER(Request),C.c_void_p,C.c_void_p,C.c_void_p,W.DWORD,C.POINTER(Reply),C.c_void_p)
        free_context=w.api(z,'AuthzFreeContext',W.BOOL,W.HANDLE)
        free_manager=w.api(z,'AuthzFreeResourceManager',W.BOOL,W.HANDLE)
        convert=w.api(w.A,'ConvertStringSidToSidW',W.BOOL,W.LPCWSTR,C.POINTER(C.c_void_p))
        manager=W.HANDLE();self.assertTrue(bool(init(1,None,None,None,'fixture',C.byref(manager))))
        try:
            # No logon, password, accounts or group lookup: synthetic contexts.
            for principal, expected in [('S-1-5-21-111-222-333-1001',True),('S-1-5-21-111-222-333-1002',False)]:
                sid=C.c_void_p();ctx=W.HANDLE();self.assertTrue(bool(convert(principal,C.byref(sid))))
                try:
                    self.assertTrue(bool(context(2,sid,manager,None,0,None,C.byref(ctx))))
                    for rights in ['FA','RC']:
                        with self.descriptor('O:S-1-5-21-111-222-333-1001G:SYD:P(A;;'+rights+';;;OW)') as sd:
                            granted,err=W.DWORD(),W.DWORD();req=Request(1,None,None,0,None);reply=Reply(1,C.pointer(granted),None,C.pointer(err))
                            self.assertTrue(bool(check(0,ctx,C.byref(req),None,sd,None,0,C.byref(reply),None)))
                            self.assertEqual(bool(granted.value & 1), expected if rights=='FA' else False)
                finally:
                    if ctx: free_context(ctx)
                    w.free(sid)
        finally:
            free_manager(manager)

    def test_synthetic_owner_and_grant_controls(self):
        from finix import windows_native as w
        C=w.C
        original=w.api
        # Feed a real Windows-parsed SD to the unmodified native ACE walker.
        for owner, grants, private, expected in [
            ('CURRENT','(A;;FA;;;OW)',True,True),
            ('SY','(A;;FA;;;OW)',False,True),
            ('SY','(A;;FA;;;OW)',True,False),
            ('S-1-5-21-111-222-333-1001','(A;;FA;;;OW)',False,False),
            ('CURRENT','(A;;FA;;;CO)',True,False),
            *[('CURRENT','(A;;FA;;;OW)(A;;FA;;;'+sid+')',p,False) for sid in ['WD','BU','AU'] for p in [False,True]],
            ('CURRENT','(A;;FA;;;CURRENT)',True,True),
            ('CURRENT','(D;;FA;;;WD)(A;;FA;;;OW)',True,True),
            ('CURRENT','NO_ACCESS_CONTROL',True,False),
            ('CURRENT','(XA;;FA;;;WD;(@User.X == 1))',True,False),
            ('CURRENT','(OA;;FA;00000000-0000-0000-0000-000000000001;;OW)',True,False),
            ('BA','(A;;FA;;;OW)',False,True),
            ('BA','(A;;FA;;;OW)',True,False),
        ]:
            with self.subTest(owner_current=owner=='CURRENT', private=private, expected=expected):
                sddl=('O:'+owner+'G:SYD:P'+grants).replace('CURRENT',w.current_sid())
                with self.descriptor(sddl) as sd:
                    owner_ptr,dacl=C.c_void_p(),C.c_void_p();present,default=w.W.BOOL(),w.W.BOOL()
                    getowner=original(w.A,'GetSecurityDescriptorOwner',w.W.BOOL,C.c_void_p,C.POINTER(C.c_void_p),C.POINTER(w.W.BOOL))
                    getdacl=original(w.A,'GetSecurityDescriptorDacl',w.W.BOOL,C.c_void_p,C.POINTER(w.W.BOOL),C.POINTER(C.c_void_p),C.POINTER(w.W.BOOL))
                    self.assertTrue(bool(getowner(sd,C.byref(owner_ptr),C.byref(default))))
                    self.assertTrue(bool(getdacl(sd,C.byref(present),C.byref(dacl),C.byref(default))))
                    def get_security(h,t,flags,outowner,group,outdacl,sacl,outsd):
                        C.cast(outowner,C.POINTER(C.c_void_p))[0]=owner_ptr
                        C.cast(outdacl,C.POINTER(C.c_void_p))[0]=dacl
                        C.cast(outsd,C.POINTER(C.c_void_p))[0]=None
                        return 0
                    def api(dll,name,*args):
                        return get_security if name=='GetSecurityInfo' else original(dll,name,*args)
                    with patch.object(w,'api',api), patch.object(w,'information') as info:
                        info.return_value.attributes=0x10
                        self.assertEqual(w.trusted_handle(123,private=private),expected)
