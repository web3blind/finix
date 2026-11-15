"""Google-issued upload session credentials stay opaque and in memory only."""
import unittest
import traceback
from finix import core
from finix.drive import session_target

BASE='https://www.googleapis.com/upload/drive/v3/files'
QUERY='uploadType=resumable&upload_id=synthetic-upload-id'

class SessionCredentialTests(unittest.TestCase):
    def test_google_optional_session_credential_is_preserved_exactly(self):
        for query in (QUERY+'&session_crd=synthetic-session-credential',
                      'session_crd=synthetic-session-credential&'+QUERY,
                      QUERY+'&session_crd=synthetic%2Dsession%2Dcredential',
                      'session%5Fcrd=synthetic-session-credential&'+QUERY,
                      QUERY):
            with self.subTest(query=query):
                self.assertEqual(session_target(BASE+'?'+query),
                                 '/upload/drive/v3/files?'+query)

    def test_new_credential_does_not_widen_other_uri_boundaries(self):
        suffix=QUERY+'&session_crd=synthetic-session-credential'
        bad=(BASE+'?'+suffix+'&session_crd=duplicate',
             BASE+'?'+QUERY+'&session_crd=',
             BASE+'?'+QUERY+'&session_crd=bad%2Fvalue',
             BASE+'?'+QUERY+'&session_crd=bad%0Avalue',
             BASE+'?'+QUERY+'&session_crd='+('a'*4097),
             BASE+'?'+suffix+'&other=value',
             BASE+'?'+suffix+'#fragment',
             BASE.replace('https:','http:')+'?'+suffix,
             BASE.replace('www.googleapis.com','evil.example')+'?'+suffix,
             BASE.replace('www.googleapis.com','user@www.googleapis.com')+'?'+suffix,
             BASE.replace('www.googleapis.com','www.googleapis.com:444')+'?'+suffix)
        for uri in bad:
            with self.subTest(kind=bad.index(uri)):
                try:
                    session_target(uri)
                except core.FinixError as exc:
                    message=''.join(traceback.format_exception(exc))
                    self.assertNotIn('synthetic-session-credential',message)
                    self.assertNotIn('synthetic-upload-id',message)
                    self.assertIsNone(exc.__cause__)
                    self.assertTrue(exc.__suppress_context__)
                else:
                    self.fail('Unsafe upload session accepted')

if __name__=='__main__':
    unittest.main()
