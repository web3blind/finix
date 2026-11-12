"""Closed locale projection: fake SDK config only, no real profile reads."""
import importlib.util
from pathlib import Path
import sys
from types import ModuleType
from unittest.mock import patch
import unittest
from fastapi import FastAPI
from fastapi.testclient import TestClient

class UiLanguageTests(unittest.TestCase):
    def test_locale_projection_is_closed_readonly_and_request_scoped(self):
        spec=importlib.util.spec_from_file_location('finix_locale_test_api',Path(__file__).parents[1]/'dashboard'/'plugin_api.py')
        api=importlib.util.module_from_spec(spec);spec.loader.exec_module(api)
        app=FastAPI();app.include_router(api.router)
        config=ModuleType('hermes_cli.config')
        active={'value':{'display':{'language':'en'},'secret':'PRIVATE-SENTINEL'}}
        def read():return active['value']
        config.load_config_readonly=read
        with patch.dict(sys.modules,{'hermes_cli.config':config}),TestClient(app) as client:
            for raw,expected in [('en','en'),('en-US','en'),('ru','ru'),('ru-RU','ru'),('English','en'),(None,'en'),({'secret':'PRIVATE-SENTINEL'},'en')]:
                active['value']={'display':{'language':raw},'secret':'PRIVATE-SENTINEL'}
                before=repr(active['value'])
                response=client.get('/ui-language')
                self.assertEqual(response.status_code,200)
                self.assertEqual(response.json(),{'ui_language':expected})
                self.assertEqual(response.headers['cache-control'],'no-store')
                self.assertEqual(repr(active['value']),before)
            config.load_config_readonly=lambda:(_ for _ in ()).throw(RuntimeError('PRIVATE-SENTINEL'))
            self.assertEqual(client.get('/ui-language').json(),{'ui_language':'en'})
