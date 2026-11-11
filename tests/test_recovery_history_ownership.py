"""Real SDK stores; runner must fence default/effective roots before collection."""
from contextlib import closing
import hashlib
import sqlite3
from pathlib import Path
import pytest


@pytest.fixture(autouse=True)
def fenced_root(tmp_path, monkeypatch, request):
    from finix import core
    import uuid
    root=core.private_dir(tmp_path.parent/('h'+uuid.uuid4().hex[:8]))
    import os,json,hashlib
    import hermes_constants
    sdk=Path(hermes_constants.__file__).parent
    key=hashlib.sha256(str(sdk.resolve()).encode()).hexdigest()[:16]
    borrowed=Path(os.environ['FINIX_BOUND_SCOPE'])/'sdk-root/installs'/key/'facts.json'
    # A root SOURCE captures installs too. Do not place a fixture-only tooling
    # junction in its data; the already selected immutable interpreter supplies
    # dependencies under HERMES_DISABLE_LAZY_INSTALLS, without new PM selection.
    if borrowed.exists() and getattr(request.node,'callspec',None) and request.node.callspec.params.get('variant')=='root-multiplex':
        borrowed=borrowed.with_name('not-materialized-for-root-source')
    if borrowed.exists():
        import shutil
        facts=json.loads(borrowed.read_text())
        original=Path(facts['packages']['venv']['environment'])
        # SDK _recorded_venv resolves BOTH environment and generations before
        # admission. Link the exact owned generations directory, not binaries
        # or pyvenv.cfg; original cached PM remains untouched. Sequential cases
        # share immutable tooling but retain independent profile/data roots.
        generations=root/'installs'/key/'environments'
        core.private_dir(generations.parent)
        shared=original.parent.parent
        if os.name=='nt':
            import subprocess
            subprocess.run(['cmd.exe','/c','mklink','/J',str(generations),str(shared)],check=True,capture_output=True)
        else:
            generations.symlink_to(shared,target_is_directory=True)
        (root/'installs'/key/'facts.json').write_text(json.dumps(facts))
    monkeypatch.setenv('HERMES_HOME',str(root))
    from hermes_constants import get_default_hermes_root, get_hermes_home
    from hermes_cli.profiles import get_profile_dir
    assert Path(get_default_hermes_root()).resolve()==root.resolve()
    assert Path(get_hermes_home()).resolve()==root.resolve()
    assert Path(get_profile_dir('source')).resolve()==(root/'profiles/source').resolve()
    yield
    # Reap only retained runtimes, then remove this fixture's immutable PM copy.
    import finix.recovery_pipeline as pipeline
    cleanup=[]
    for k,owned_runtime in list(pipeline._OWNED_TARGETS.items()):
        pid=owned_runtime.child.pid
        runtime_pids=list(owned_runtime.native.runtime_pids) if owned_runtime.native else [pid]
        owned_runtime.cleanup();pipeline._OWNED_TARGETS.pop(k,None)
        cleanup.append({'launcher_pid':pid,'runtime_pids':runtime_pids,'launcher_reaped':owned_runtime.child.poll() is not None})
    (root/'fixture-cleanup.json').write_text(json.dumps(cleanup))
    # Native extension modules may be mapped in this pytest interpreter too.
    # The external controller removes copies only after this interpreter exits.


def seed(root):
    from hermes_state import SessionDB
    from hermes_cli.profiles import get_profile_dir
    from finix import core
    home = core.private_dir(Path(get_profile_dir('source')))
    db = SessionDB(home/'state.db')
    for i in range(3):
        db.create_session('history-'+str(i), source='desktop')
        for j in range(4):
            db.append_message('history-'+str(i),role='assistant',content='synthetic '+str(j))
    db.close()
    return home/'state.db'


@pytest.mark.parametrize('variant', ['mixed','unknown-principal','blank','malformed','target-owned','binding','collision'])
def test_refuse_unsupported_ownership_without_mutating_source(tmp_path, variant):
    from finix.recovery_import import _prepare_history, RecoveryImportError
    src=seed(tmp_path)
    with closing(sqlite3.connect(src)) as db:
        if variant=='mixed': db.execute("update sessions set profile_name='foreign' where id='history-0'")
        elif variant=='unknown-principal': db.execute("update sessions set user_id='unknown:principal'")
        elif variant=='blank': db.execute("update sessions set profile_name=NULL")
        elif variant=='malformed': db.execute("update sessions set profile_name=' source '")
        elif variant=='target-owned': db.execute("update sessions set profile_name='recovered'")
        elif variant=='binding': db.execute("insert into state_meta values ('gateway.local_policy.v1:history-0', '{}')")
        elif variant=='collision': db.execute("insert into gateway_routing(scope,session_key,entry_json,updated_at) values ('','agent:recovered:local','{}',1)")
        db.commit()
    before=src.read_bytes()
    with pytest.raises(RecoveryImportError,match='HISTORY_OWNERSHIP_UNSUPPORTED'):
        _prepare_history(src,tmp_path/'out.db','recovered',tmp_path)
    assert src.read_bytes()==before


def test_genuine_sdk_canonical_policy_is_refused(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from hermes_cli.profiles import get_profile_dir
    from finix import core
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import initialize_session_authority
    from gateway.session_local import create_local_session
    from gateway.session_contract import SessionActor
    from gateway.owner_identity import host_owner_subject
    from finix.recovery_import import _prepare_history, RecoveryImportError
    home=core.private_dir(Path(get_profile_dir('source')))
    monkeypatch.setattr(run,'_load_gateway_config',lambda: {'platform_toolsets': {'gui': []}})
    store=SessionStore(home/'sessions',GatewayConfig())
    runner=SimpleNamespace(session_store=store,_session_db=store._db,adapters={},_draining=False)
    import asyncio
    authority=asyncio.run(initialize_session_authority(runner,profile_id=str(home),instance_id='synthetic'))
    actor=SessionActor(host_owner_subject(),authority.profile_id,frozenset({'session:create','session:read'}),'native')
    create_local_session(authority,actor,{'request_id':'canonical','source':'gui','model':'synthetic-model','toolsets':[]})
    assert store._db._conn.execute("select count(*) from state_meta where key like 'gateway.local_policy.v1:%'").fetchone()[0]==1
    src=Path(store._db.db_path)
    assert src.is_relative_to(tmp_path)
    store._db.close()
    before=src.read_bytes()
    with pytest.raises(RecoveryImportError,match='HISTORY_OWNERSHIP_UNSUPPORTED'):
        _prepare_history(src,tmp_path/'canonical-copy.db','recovered',tmp_path)
    assert src.read_bytes()==before


@pytest.mark.parametrize('binding',[None,'foreign',' source','SOURCE'])
def test_archive_binding_must_independently_match_exact_owner(tmp_path,binding):
    from finix.recovery_import import _prepare_history,RecoveryImportError
    src=seed(tmp_path)
    with pytest.raises(RecoveryImportError,match='HISTORY_SOURCE_BINDING_REQUIRED'):
        _prepare_history(src,tmp_path/'bound.db','recovered',tmp_path,source_binding=binding)


def test_sdk_rekey_preserves_ids_and_all_content(tmp_path):
    from finix import recovery_import as subject
    src=seed(tmp_path); before=src.read_bytes()
    out=tmp_path/'state.db'
    proof=subject._prepare_history(src,out,'recovered',tmp_path,source_binding='source')
    assert proof['source_name']=='source'
    assert proof['target_name']=='recovered'
    assert src.read_bytes()==before
    with closing(sqlite3.connect(out)) as db:
        assert db.execute('select distinct profile_name from sessions').fetchall()==[('recovered',)]
        assert db.execute('select count(*) from messages').fetchone()==(12,)
    # Full logical row equality with just sessions.profile_name permitted to differ.
    with closing(sqlite3.connect(src)) as a, closing(sqlite3.connect(out)) as b:
        for table in ('sessions','messages'):
            columns=[r[1] for r in a.execute('pragma table_info('+table+')')]
            selected=','.join('"'+c+'"' for c in columns if c!='profile_name')
            assert a.execute('select '+selected+' from '+table+' order by id').fetchall()==b.execute('select '+selected+' from '+table+' order by id').fetchall()
