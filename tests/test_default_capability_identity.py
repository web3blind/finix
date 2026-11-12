"""Closed default identity projection; native lifecycle is modeled, never invoked."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from contextlib import ExitStack
from starlette.responses import Response
from finix import core, recovery_http, default_recovery_api as api


class DefaultIdentityTests(unittest.TestCase):
    def test_actual_sdk_root_geometry_admits_only_physical_default(self):
        import os
        import hermes_constants as constants
        from hermes_constants import get_default_hermes_root
        # Model the OS root in private scratch; public SDK root/profile geometry
        # remains real. Scratch itself lies under the account's real Windows root.
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'HERMES_HOME':tmp}),patch.object(constants,'_get_platform_default_hermes_home',return_value=Path(tmp)):
            root=get_default_hermes_root()
            self.assertEqual(root.resolve(),Path(tmp).resolve(),'refuse non-fixture SDK root')
            self._check_sdk_root_geometry(core.private_dir(root))

    def _check_sdk_root_geometry(self,root):
        for home in (root,core.private_dir(root/'profiles/wrong-root')):
            with self.subTest(default=home==root),ExitStack() as stack:
                profile=SimpleNamespace(profile_id='a'*64,home=home,root=home,default_root=root)
                core.atomic_json(home/'backend.json',{'profile_id':profile.profile_id})
                stack.enter_context(patch.object(recovery_http,'binding',return_value='epoch'))
                preflight=stack.enter_context(patch.object(api.lifecycle,'preflight'))
                stack.enter_context(patch.object(api.lifecycle,'identify',return_value={'modeled':True}))
                stack.enter_context(patch.object(api.lifecycle,'_exact',return_value=True))
                router=recovery_http.create_router(lambda:profile)
                endpoint=next(r.endpoint for r in router.routes if r.path=='/recovery/default/capabilities')
                result=endpoint(SimpleNamespace(client=SimpleNamespace(host='127.0.0.1')),Response())
                self.assertEqual(result['supported'],home==root)
                self.assertEqual(result['profile_id'],profile.profile_id if home==root else None)
                self.assertEqual(preflight.call_count,1 if home==root else 0)

    def test_identity_is_withheld_on_stale_source_epoch_root_or_native_refusal(self):
        for failure in ('source', 'epoch', 'root', 'native', 'unknown'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                root=Path(tmp)
                profile=SimpleNamespace(profile_id='a'*64,home=root,root=root)
                core.atomic_json(root/'backend.json',{'profile_id':profile.profile_id})
                epoch=['first']; identity=[[1,2]]
                stack.enter_context(patch.object(api,'authority',return_value=root))
                stack.enter_context(patch.object(recovery_http,'binding',side_effect=lambda p:epoch[0]))
                stack.enter_context(patch.object(api.cold,'_identity',side_effect=lambda r:identity[0]))
                def preflight(*args,**kwargs):
                    self.assertIs(kwargs['trusted_finix'],True)
                    if failure=='source':core.atomic_json(root/'backend.json',{'profile_id':'b'*64})
                    elif failure=='epoch':epoch[0]='second'
                    elif failure=='root':identity[0]=[3,4]
                    elif failure=='unknown':raise RuntimeError('private unknown')
                stack.enter_context(patch.object(api.lifecycle,'preflight',side_effect=preflight))
                stack.enter_context(patch.object(api.lifecycle,'identify',return_value={'modeled':True}))
                stack.enter_context(patch.object(api.lifecycle,'_exact',return_value=failure!='native'))
                router=recovery_http.create_router(lambda:profile)
                endpoint=next(r.endpoint for r in router.routes if r.path=='/recovery/default/capabilities')
                request=SimpleNamespace(client=SimpleNamespace(host='127.0.0.1'))
                result=endpoint(request,Response())
                self.assertIs(result['supported'],False)
                self.assertIsNone(result['profile_id'])
                self.assertIsNone(result['serverRootToken'])
                self.assertIs(result['local_only'],False)
                self.assertNotIn('private unknown',json.dumps(result))

    def test_remote_caller_never_reaches_native_projection(self):
        from fastapi import HTTPException
        with patch.object(api,'authority') as authority:
            router=recovery_http.create_router(lambda:SimpleNamespace(profile_id='a'*64))
            endpoint=next(r.endpoint for r in router.routes if r.path=='/recovery/default/capabilities')
            with self.assertRaises(HTTPException) as caught:
                endpoint(SimpleNamespace(client=SimpleNamespace(host='192.0.2.1')),Response())
            self.assertEqual(caught.exception.status_code,403)
            authority.assert_not_called()
