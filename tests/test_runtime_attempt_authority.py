"""Pure modeled authority negatives: NO SDK imports, fixtures or process claims."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sys
import unittest
from finix import default_recovery as cold,runtime_attempt as custody

class AuthorityTests(unittest.TestCase):
    def engine(self):
        return SimpleNamespace(plan_digest='a'*64,_plan=lambda:{'root_identity':[1,2]})
    def runtime(self,**changes):
        values=dict(root=Path('never-opened'),_stopped=True,child=SimpleNamespace(poll=lambda:0),
            native=SimpleNamespace(runtime_pids={1}),_spawn_failed=False,
            _never_spawned=False,_ready_observed=False,attempt_plan_digest='a'*64)
        values.update(changes);return SimpleNamespace(**values)
    def test_spawn_exception_without_returned_handle_is_not_no_spawn_proof(self):
        runtime=self.runtime(child=None,_spawn_failed=True,_never_spawned=False)
        with patch.object(cold,'_identity',return_value=[1,2]):
            with self.assertRaises(cold.RecoveryError):custody.stopped_attempt(self.engine(),runtime)
    def test_observed_ready_runtime_cannot_supply_failed_start_authority(self):
        with patch.object(cold,'_identity',return_value=[1,2]):
            with self.assertRaises(cold.RecoveryError):custody.stopped_attempt(self.engine(),self.runtime(_ready_observed=True))
    def test_other_plan_runtime_cannot_supply_failed_start_authority(self):
        with patch.object(cold,'_identity',return_value=[1,2]):
            with self.assertRaises(cold.RecoveryError):custody.stopped_attempt(self.engine(),self.runtime(attempt_plan_digest='b'*64))
    def test_exact_bound_failed_or_never_invoked_attempt_positive_controls(self):
        for runtime in (self.runtime(),self.runtime(child=None,_never_spawned=True)):
            with self.subTest(never_spawned=runtime._never_spawned),patch.object(cold,'_identity',return_value=[1,2]):
                proof=custody.stopped_attempt(self.engine(),runtime)
                self.assertEqual(custody._PROOFS[proof],'a'*64)

    @unittest.skipUnless(__import__('os').name=='nt','Windows scan fallback model')
    def test_disappearing_unknown_companion_cannot_open_an_opaque_blob(self):
        from contextlib import nullcontext
        from finix import core,windows_native as win
        path=Path('never-opened');owner='a'*64
        entries=[SimpleNamespace(name=owner,path=str(path/owner),is_file=lambda **kwargs:True),
                 SimpleNamespace(name=owner+'-wal',path=str(path/(owner+'-wal')),is_file=lambda **kwargs:True)]
        anchor=SimpleNamespace(path=path)
        def metadata(p,*args,**kwargs):
            if p.name.endswith('-wal'):raise FileNotFoundError()
            return SimpleNamespace(st_file_attributes=0)
        for sqlite in (False,True):
            with self.subTest(genuine_sqlite=sqlite):
                captured=core.SourceFingerprint((1,2,0o100600,100,1,1),sqlite, None if sqlite else {'sha256':'b'*64,'size':100})
                with patch.object(win,'directory',side_effect=lambda *a,**k:nullcontext(anchor)),patch.object(win.os,'scandir',side_effect=lambda *a,**k:nullcontext(entries)),patch.object(Path,'lstat',metadata),patch.object(win,'snapshot_regular',side_effect=lambda *a,**k:nullcontext(123)),patch.object(core,'discovered_source',return_value=captured),patch.object(win,'source_file',side_effect=lambda *a,**k:nullcontext((123,anchor,owner))),patch.object(core,'peek',return_value=b'SQLite format 3\x00'),patch.object(win,'connect_source',return_value=SimpleNamespace(close=lambda:None)) as opening:
                    if sqlite:
                        self.assertEqual(win.scan(anchor),{owner:captured});self.assertEqual(opening.call_count,1)
                    else:
                        with self.assertRaises(core.FinixError):win.scan(anchor)
                        self.assertEqual(opening.call_count,0)

    def test_fixture_does_not_import_sdk(self):
        self.assertNotIn('hermes_constants',sys.modules)
        self.assertNotIn('hermes_state',sys.modules)

if __name__=='__main__':unittest.main()
