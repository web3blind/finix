"""Blank model recovery preserves data without enabling integrations."""
import unittest
import yaml
from finix.recovery_import import _safe_config, RecoveryImportError

class BlankModelTests(unittest.TestCase):
    def test_supported_blank_model_preserved_and_integrations_parked(self):
        for fields in [{}, {'model':''}, {'model':None}, {'model':{}}, {'model':{'default':''}}, {'model':{'default':None,'provider':None,'base_url':None}}]:
            with self.subTest(fields=fields):
                config=dict(fields, _config_version=12, plugins={'enabled':['never']}, platforms={'telegram':{'enabled':True}}, mcp_servers={'never':{'command':'never','enabled':True}}, secrets={'sources':['env'],'never':{'enabled':True}}, hooks={'never':{}}, PYTHONPATH='/never')
                out=yaml.safe_load(_safe_config(yaml.safe_dump(config)))
                self.assertEqual('model' in out,'model' in fields)
                if 'model' in fields: self.assertEqual(out['model'],fields['model'])
                self.assertEqual(out['_config_version'],12)
                self.assertEqual(out['plugins']['enabled'],[])
                self.assertEqual(out['hooks'],{})
                for section in ['platforms','mcp_servers','secrets']:
                    self.assertFalse(out[section]['telegram' if section=='platforms' else 'never']['enabled'])
                self.assertEqual(out['secrets']['sources'],['env'])
                self.assertNotIn('PYTHONPATH',out)

    def test_malformed_model_not_blank_compatibility(self):
        for model in [[], ['x'], 1, True, {'default':[]}, {'default':4}, {'default':{'arbitrary':'object'}}]:
            with self.subTest(model=model):
                with self.assertRaises(RecoveryImportError) as error:
                    _safe_config(yaml.safe_dump({'model':model}))
                self.assertEqual(error.exception.code,'INVALID_CONFIG')

if __name__=='__main__': unittest.main()
