"""RED contract probe: clean SDK close must precede physical replacement.

No engine bypass, simulated lifecycle or fabricated handoff. Run only under the
adjacent isolated-root runner. This probe distinguishes digest from membership.
"""
import os
import unittest
from pathlib import Path
from finix import core, default_recovery as engine


class QuiescenceContractTests(unittest.TestCase):
    def test_clean_sdk_close_preserves_digest_but_changes_sealed_companion_scope(self):
        from hermes_constants import get_default_hermes_root, get_hermes_home
        from hermes_cli.profiles import get_profile_dir
        from hermes_state import SessionDB
        scope=Path(os.environ['FINIX_DEFAULT_TEST_SCOPE'])
        root=get_default_hermes_root()
        self.assertEqual(root,scope/'sdk-root')
        self.assertEqual(root,get_hermes_home())
        self.assertEqual(root,get_profile_dir('default'))
        directory=core.private_dir(root/'cache/finix/close-contract')
        path=directory/'state.db'
        temp=core.private_dir(directory/'snapshots')
        db=SessionDB(path)
        try:
            db.create_session('synthetic-close-contract','cli')
            db.append_message('synthetic-close-contract','assistant','committed synthetic data')
            sealed=engine._sqlite_generation(path,temp)
            raw_before=core.digest_file(path)
            # Keep unlinked POSIX inodes alive so reuse cannot masquerade as
            # identity equality. Native Windows file IDs already include reuse
            # generation; retaining deny-delete handles there changes cleanup.
            if os.name!='nt':
                for suffix in ('-wal','-shm'):
                    fd=os.open(str(path)+suffix,os.O_RDONLY|os.O_NOFOLLOW)
                    self.addCleanup(os.close,fd)
        finally:
            db.close()
        scope_after_close={s:engine._leaf(Path(str(path)+s))['identity']
            if os.path.lexists(str(path)+s) else None
            for s in ('-wal','-shm','-journal')}
        print('SCOPE_IMMEDIATELY_AFTER_SDK_CLOSE',scope_after_close,flush=True)
        self.assertEqual(sealed['kind'],'sqlite-consistent-v3-sdk-heartbeat-rows')
        self.assertIn('runtime',sealed['producer_pins'])
        self.assertEqual(sealed['family_names'],['state.db','state.db-wal','state.db-shm','state.db-journal'])
        self.assertIsNone(scope_after_close['-wal'])
        self.assertIsNone(scope_after_close['-shm'])
        # v2 explicitly seals canonical auxiliaries, not their pre-close inodes.
        after=engine._sqlite_generation(path,temp)
        self.assertTrue(Path(str(path)+'-wal').exists())
        print('CLOSE_CONTRACT',{'sealed':sealed,'after':after,
            'scope_after_close':scope_after_close,
            'raw_before':raw_before,'raw_after':core.digest_file(path)},flush=True)
        self.assertEqual(sealed['snapshot_sha256'],after['snapshot_sha256'])
        self.assertEqual(sealed['native_identity'],after['native_identity'])
        # The requested GREEN cannot use the existing exact-scope descriptor:
        # the clean producer close legitimately removed both sealed companions.
        engine._verify_sqlite_generation(path,temp,sealed)
