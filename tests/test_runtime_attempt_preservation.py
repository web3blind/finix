"""Opaque DATA transaction tests; mocked stop proof is NOT daemon acceptance."""
import os
from pathlib import Path
from unittest.mock import patch
from test_sqlite_family_transaction import FamilyTests as _FamilyTests
from finix import core, default_recovery as cold, sqlite_family

POLICY='runtime-attempt-preserve-v1'

class AttemptTests(_FamilyTests):
    def prepare(self,cap):
        return cold.prepare(cap,self.root,journal_root=self.root/'cache/finix/default-recovery',
                            intent='replace-real-default',runtime_attempt_policy=POLICY)

    def test_opaque_family_and_atomic_ordinary_data_preserved_before_old_restore(self):
        self.old_database();(self.root/'config.yaml').write_bytes(b'{}\n')
        plan=self.prepare(self.capability())
        self.assertEqual(plan._plan()['version'],4)
        self.assertIn('preserv',plan.public_plan()['runtime_attempt']['consent_notice'].lower())
        plan.apply(plan.consent())
        for name,data in [('state.db-wal',b'opaque-wal'),('state.db-shm',b'opaque-shm'),('state.db-journal',b'opaque-hot-journal')]:
            (self.root/name).write_bytes(data)
        changed=self.directory/'atomic-config';changed.write_bytes(b'changed ordinary DATA')
        os.replace(changed,self.root/'config.yaml')
        names=['config.yaml',*sqlite_family.NAMES]
        before={n:cold._leaf(self.root/n) for n in names}
        from finix import runtime_attempt as custody
        with patch.object(custody,'require_stopped_attempt',return_value=None):
            result=plan.rollback_attempt(plan.consent(),None)
        self.assertEqual(result['state'],'rolled_back')
        escrow=Path(plan._plan()['runtime_attempt']['custody'])
        manifest=cold._load(escrow/'execution-manifest.json')
        self.assertEqual({n:manifest['current'][n] for n in names},before)
        for n in names:
            token=custody.token(n)
            self.assertEqual(cold._leaf(plan.directory/'custody-originals'/token),before[n])
            for kind in ('objects','copies'):
                with core.regular_input(escrow/kind/token) as fd:
                    logical, stored = __import__('finix.custody_payload',fromlist=['decode']).decode(fd)
                self.assertEqual(logical,{k:before[n][k] for k in ('size','sha256')})
                self.assertEqual(stored,manifest['stored'][kind+'/'+token])
        self.check_old()
        self.assertEqual((self.root/'config.yaml').read_bytes(),b'{}\n')
        self.assertEqual(cold._load(escrow/'progress.json')['state'],'restored')

    def test_old_storage_policy_cannot_reuse_consent(self):
        self.old_database();plan=self.prepare(self.capability())
        p=cold._load(plan.directory/'plan.json')
        self.assertEqual(p['runtime_attempt']['storage_format'],'opaque-container-v2')
        p['runtime_attempt'].pop('storage_format')
        from finix.runtime_attempt import validate_policy
        with self.assertRaisesRegex(cold.RecoveryError,'PINNED_PACKAGE_CHANGED'):validate_policy(p)

    def test_corrupt_committed_container_refuses_reopen(self):
        self.old_database();plan=self.prepare(self.capability());plan.apply(plan.consent())
        from finix import runtime_attempt as custody
        with patch.object(custody,'require_stopped_attempt',return_value=None):plan.rollback_attempt(plan.consent(),None)
        escrow=Path(plan._plan()['runtime_attempt']['custody'])
        blob=escrow/'objects'/custody.token('state.db')
        data=blob.read_bytes();blob.write_bytes(data[:-1]+bytes([data[-1]^1]))
        with self.assertRaises(cold.RecoveryError):plan.rollback_attempt(plan.consent(),None)
        self.check_old()

    def test_missing_stop_proof_and_unknown_auxiliary_preserve_everything(self):
        self.old_database();plan=self.prepare(self.capability());plan.apply(plan.consent())
        from finix import runtime_attempt as custody
        with self.assertRaises(cold.RecoveryError): plan.rollback_attempt(plan.consent(),None)
        extra=self.root/'state.db-unknown';extra.write_bytes(b'unknown')
        before={n:cold._leaf(self.root/n) for n in ['state.db','state.db-unknown']}
        with patch.object(custody,'require_stopped_attempt',return_value=None):
            with self.assertRaises(cold.RecoveryError):plan.rollback_attempt(plan.consent(),None)
        for n,leaf in before.items():self.assertEqual(cold._leaf(self.root/n),leaf)
        extra.unlink()

    def test_main_identity_substitution_is_not_a_runtime_preimage(self):
        self.old_database();plan=self.prepare(self.capability());plan.apply(plan.consent())
        replacement=self.directory/'foreign-main';replacement.write_bytes((self.root/'state.db').read_bytes())
        os.replace(replacement,self.root/'state.db');before=cold._leaf(self.root/'state.db')
        from finix import runtime_attempt as custody
        with patch.object(custody,'require_stopped_attempt',return_value=None):
            with self.assertRaises(cold.RecoveryError):plan.rollback_attempt(plan.consent(),None)
        self.assertEqual(cold._leaf(self.root/'state.db'),before)

    def test_crash_after_current_move_reopens_without_clobber(self):
        self.old_database();plan=self.prepare(self.capability());plan.apply(plan.consent())
        (self.root/'state.db-wal').write_bytes(b'opaque-attempt')
        from finix import runtime_attempt as custody
        real=sqlite_family._move;fired=[]
        def crash(engine,source,target,leaf):
            real(engine,source,target,leaf)
            if target.parent.name=='custody-originals' and not fired:
                fired.append(target);raise SystemExit(73)
        with patch.object(custody,'require_stopped_attempt',return_value=None),patch.object(sqlite_family,'_move',crash):
            with self.assertRaises(SystemExit):plan.rollback_attempt(plan.consent(),None)
        # Newly occupied original basename after crash must survive reopen intact.
        escrow=Path(plan._plan()['runtime_attempt']['custody'])
        manifest=cold._load(escrow/'execution-manifest.json')
        moved=next(n for n in manifest['current'] if custody.token(n)==fired[0].name)
        (self.root/moved).write_bytes(b'new occupied basename')
        occupied=cold._leaf(self.root/moved)
        reopened=cold.DefaultRecovery(plan.directory,plan.plan_digest)
        with self.assertRaises(cold.RecoveryError):reopened.rollback_attempt(reopened.consent(),None)
        self.assertEqual(cold._leaf(self.root/moved),occupied)
        # Remove only the test-owned conflict, preserving it in the fixture.
        os.replace(self.root/moved,self.directory/'retained-conflict')
        self.assertEqual(reopened.rollback_attempt(reopened.consent(),None)['state'],'rolled_back')
        self.check_old()

    def test_future_backup_preserves_custody_sqlite_bytes_without_opening_blobs(self):
        import shutil
        self.old_database();plan=self.prepare(self.capability());plan.apply(plan.consent())
        from finix import runtime_attempt as custody,streaming
        with patch.object(custody,'require_stopped_attempt',return_value=None):plan.rollback_attempt(plan.consent(),None)
        escrow=Path(plan._plan()['runtime_attempt']['custody'])
        export=core.private_dir(self.directory/'future-source')
        for path in escrow.rglob('*'):
            if path.is_file():
                target=export/path.relative_to(self.root);core.private_dir(target.parent);shutil.copyfile(path,target)
        key=self.directory/'future.key';recipient=core.keygen(key)
        with patch.object(streaming,'_connect_source',side_effect=AssertionError('OPAQUE_CUSTODY_SQLITE_OPEN')):
            core.backup(export,self.directory/'future-cipher',self.directory/'future-index',recipient,sqlite_temp_dir=core.private_dir(self.directory/'future-sqlite'))
        row=core.load_state(self.directory/'future-index')['backups'][0];restored=core.private_dir(self.directory/'future-restored')
        core.restore(self.directory/'future-cipher'/row['name'],key,restored)
        for path in export.rglob('*'):
            if path.is_file():self.assertEqual(core.digest_file(path),core.digest_file(restored/path.relative_to(export)))
        self.assertFalse(any(p.name.startswith('recovery-custody') for p in (escrow/'objects').iterdir()))

    def test_incoming_flat_custody_is_preserved_without_recursive_preimages(self):
        from finix.recovery_activation import profile_digest
        self.old_database();cap=self.capability();binding=cold._CAPABILITIES[cap]
        # Complete portable DATA namespace, with historical IDs as provenance.
        from finix import custody_payload as container
        import hashlib,json,io
        base=core.private_dir(binding['source']/'recovery-custody'/('a'*32))
        raw=self.directory/'prior-raw';raw.write_bytes(b'prior opaque bytes')
        leaf=dict(identity=[1,2],size=raw.stat().st_size,sha256=core.digest_file(raw)); stored={}
        for kind in ('copies','objects'):
            target=core.private_dir(base/kind)/hashlib.sha256(b'config.yaml').hexdigest()
            out=io.BytesIO()
            with core.regular_input(raw) as fd:stored[kind+'/'+target.name]=container.encode(fd,leaf,out)
            target.write_bytes(out.getvalue())
        m=dict(version=2,policy=POLICY,storage_format=container.STORAGE_FORMAT,namespace=base.name,
               stored=stored,plan_digest='b'*64,root_identity=[1,3],parents={'/historical':[1,3]},
               current={'config.yaml':leaf},old={'config.yaml':None})
        progress=dict(version=2,manifest_digest=container._digest(m),state='restored',pending=None,restore={})
        (base/'execution-manifest.json').write_text(json.dumps(m));(base/'progress.json').write_text(json.dumps(progress))
        original={p.relative_to(binding['source']).as_posix():p.read_bytes() for p in base.rglob('*') if p.is_file()}
        binding['digest']=profile_digest(binding['source'])
        plan=self.prepare(cap);plan.apply(plan.consent())
        self.assertEqual(plan._plan()['runtime_attempt']['incoming_custody'],{n:hashlib.sha256(data).hexdigest() for n,data in original.items()})
        for n,data in original.items(): self.assertEqual((self.root/n).read_bytes(),data)
        plan.rollback(plan.consent());self.check_old()
        for n,data in original.items(): self.assertEqual((self.root/n).read_bytes(),data)
        self.assertFalse(any('recovery-custody' in p.parts for p in (plan.directory/'preimages').rglob('*')))

    def test_custody_invalid_manifest_has_no_opaque_sqlite_bypass(self):
        self.old_database();plan=self.prepare(self.capability());plan.apply(plan.consent())
        from finix import runtime_attempt as custody,streaming
        with patch.object(custody,'require_stopped_attempt',return_value=None):plan.rollback_attempt(plan.consent(),None)
        escrow=Path(plan._plan()['runtime_attempt']['custody'])
        blob=escrow/'copies'/custody.token('state.db')
        # Valid exact namespace without its manifest must refuse, not open SQL.
        manifest=escrow/'execution-manifest.json';saved=self.directory/'retained-manifest'
        os.replace(manifest,saved)
        with patch.object(streaming,'_connect_source',side_effect=AssertionError('UNADMITTED_SQLITE_OPEN')) as opening:
            with self.assertRaises(core.FinixError):
                with core.regular_input(blob) as fd:
                    if os.name=='nt':
                        from finix.windows_native import directory
                        with directory(blob.parent) as parent:core.discovered_source(fd,parent,blob.name,relative=blob.relative_to(self.root).as_posix())
                    else:
                        parent=os.open(blob.parent,os.O_RDONLY|os.O_DIRECTORY)
                        try:core.discovered_source(fd,parent,blob.name,relative=blob.relative_to(self.root).as_posix())
                        finally:os.close(parent)
            self.assertEqual(opening.call_count,0)
        os.replace(saved,manifest)
        # A container is ordinary DATA even outside the reserved namespace.
        with core.regular_input(blob) as fd,patch.object(streaming,'_connect_source',side_effect=AssertionError('SQLITE_OPEN')) as opening:
            self.assertFalse(core.discovered_source(fd,None,blob.name,relative='not-'+blob.relative_to(self.root).as_posix()).sqlite)
            self.assertEqual(opening.call_count,0)

    def test_native_child_death_custody_copy_move_and_old_restore_boundaries(self):
        import subprocess,sys
        self.old_database();cap=self.capability()
        child=Path(__file__).with_name('runtime_attempt_death_child.py')
        for label in ('copy-current','move-current','restore-old-main'):
            for boundary in ('before','after'):
                plan=self.prepare(cap);plan.apply(plan.consent())
                (self.root/'state.db-wal').write_bytes(b'opaque-attempt-wal')
                result=subprocess.run([sys.executable,'-B',str(child),str(plan.directory),plan.plan_digest,label,boundary],timeout=45)
                self.assertEqual(result.returncode,73,(label,boundary))
                reopened=cold.DefaultRecovery(plan.directory,plan.plan_digest)
                self.assertEqual(reopened.rollback_attempt(reopened.consent(),None)['state'],'rolled_back')
                self.check_old()
                print('CUSTODY_NATIVE_EXIT73_REOPEN',label,boundary,flush=True)

    def test_hardlinked_same_bytes_replacement_at_custody_move_is_preserved(self):
        self.old_database();plan=self.prepare(self.capability());plan.apply(plan.consent())
        from finix import runtime_attempt as custody
        spare=self.directory/'same-bytes';spare.write_bytes((self.root/'config.yaml').read_bytes())
        replacement=self.directory/'linked-replacement';os.link(spare,replacement)
        real=sqlite_family._move;fired=[]
        def swap(engine,source,target,leaf):
            if target.parent.name=='custody-originals' and source.name=='config.yaml' and not fired:
                os.replace(replacement,source);fired.append(cold._identity(source))
            return real(engine,source,target,leaf)
        with patch.object(custody,'require_stopped_attempt',return_value=None),patch.object(sqlite_family,'_move',swap):
            with self.assertRaises((core.FinixError,OSError)):plan.rollback_attempt(plan.consent(),None)
        self.assertEqual(cold._identity(self.root/'config.yaml'),fired[0])
        self.assertEqual((self.root/'config.yaml').stat().st_nlink,2)
        # Retain the injected conflict outside canonical before subsequent cases.
        os.replace(self.root/'config.yaml',self.directory/'retained-linked-conflict')

    def test_scope_census_is_boundary_bounded_not_quadratic_per_ordinary_leaf(self):
        self.old_database();core.private_dir(self.root/'memories')
        for i in range(24):(self.root/'memories'/('old-'+str(i))).write_bytes(b'old')
        plan=self.prepare(self.capability());plan.apply(plan.consent())
        from finix import runtime_attempt as custody
        with patch.object(custody,'require_stopped_attempt',return_value=None),patch.object(custody,'_unknown',wraps=custody._unknown) as census:
            plan.rollback_attempt(plan.consent(),None)
        self.assertLessEqual(census.call_count,10)
        for i in range(24):self.assertEqual((self.root/'memories'/('old-'+str(i))).read_bytes(),b'old')

    @__import__('unittest').skipUnless(os.name=='nt','Windows redirector-specific ownership boundary')
    def test_absent_native_redirector_capture_cannot_issue_stopped_authority(self):
        from types import SimpleNamespace
        self.old_database();plan=self.prepare(self.capability())
        from finix import runtime_attempt as custody
        unknown=SimpleNamespace(root=self.root,_stopped=True,child=SimpleNamespace(poll=lambda:0),native=None,
                                attempt_plan_digest=plan.plan_digest,_ready_observed=False)
        with self.assertRaises(cold.RecoveryError):custody.stopped_attempt(plan,unknown)

    def test_ready_commit_is_separate_durable_cutoff(self):
        self.old_database();plan=self.prepare(self.capability());plan.apply(plan.consent())
        from finix import runtime_attempt as custody
        ready={'fixture':'modeled-not-daemon'}
        custody.commit_success(plan,ready)
        self.assertEqual(cold._load(plan.directory/'success-commit.json')['identity'],ready)
        self.assertTrue((plan.directory/'ready-observed.json').exists())
        with self.assertRaises(cold.RecoveryError):plan.rollback_attempt(plan.consent(),None)

    def test_success_cutoff_forbids_old_consent_rollback(self):
        self.old_database();plan=self.prepare(self.capability());plan.apply(plan.consent())
        cold._write(plan.directory/'success-commit.json',{'test':'durable-cutoff'})
        before=cold._leaf(self.root/'state.db')
        with self.assertRaises(cold.RecoveryError):plan.rollback_attempt(plan.consent(),None)
        with self.assertRaises(cold.RecoveryError):plan.rollback(plan.consent())
        self.assertEqual(cold._leaf(self.root/'state.db'),before)
