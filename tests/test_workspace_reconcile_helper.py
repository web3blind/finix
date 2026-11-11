"""Validate helper's actual Google API calls without credentials or sockets."""
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from contextlib import ExitStack, redirect_stdout
from finix import workspace_helper

class HelperProtocol(unittest.TestCase):
    def test_identity_preallocated_folder_and_media_id_are_sent_to_google_api(self):
        class Service:
            def files(self):return self
            def about(self):return self
            def get(self,**kw):self.kw=kw;self.value={'user':{'permissionId':'synthetic-account'}};return self
            def generateIds(self,**kw):self.kw=kw;self.value={'ids':['allocated-id']};return self
            def create(self,**kw):self.kw=kw;self.value={'id':kw['body']['id'],'mimeType':'application/octet-stream'};return self
            def execute(self):return self.value
        service=Service()
        with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as tmp:
            path=Path(tmp)/'cipher.age';path.write_bytes(b'age-encryption.org/v1\nsynthetic')
            for operation,args in [('identity',[]),('generate-id',[]),('create-folder',['--id','folder-id','--name','folder']),('upload',[str(path),'--id','media-id','--name','cipher.age','--parent','folder-id'])]:
                module=SimpleNamespace(build_service=lambda *args:service)
                spec=SimpleNamespace(loader=SimpleNamespace(exec_module=lambda obj:None))
                with ExitStack() as stack:
                    stack.enter_context(patch.object(workspace_helper.importlib.util,'spec_from_file_location',return_value=spec))
                    stack.enter_context(patch.object(workspace_helper.importlib.util,'module_from_spec',return_value=module))
                    stack.enter_context(patch('sys.argv',['helper','synthetic.py','65536',operation,*args]))
                    if os.name!='nt':stack.enter_context(patch('resource.setrlimit'))
                    output=stack.enter_context(redirect_stdout(io.StringIO()))
                    workspace_helper.main()
                response=json.loads(output.getvalue())
                if operation=='identity':self.assertEqual(response,{'account_id':'synthetic-account'})
                elif operation=='generate-id':self.assertEqual(response,{'id':'allocated-id'})
                else:
                    self.assertEqual(service.kw['body']['id'],'folder-id' if operation=='create-folder' else 'media-id')
                    if operation=='upload':self.assertEqual(service.kw['body']['parents'],['folder-id'])
