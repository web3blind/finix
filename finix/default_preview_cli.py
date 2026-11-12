"""Native CLI dispatch. No key contents, capabilities or live handles persist."""
import os
from pathlib import Path
import re
import subprocess
from . import core
from .default_preview_jobs import PreviewJobs
from .default_recovery_api import authority, identifier, RecoveryError


def start(profile, request_id, copy_id, identity):
    identifier(request_id)
    if not isinstance(copy_id,str) or not re.fullmatch('[a-f0-9]{64}',copy_id):
        raise RecoveryError('INVALID_CANDIDATE')
    from .recovery_http import explicit_path
    explicit_path(identity)  # Lexical/native ancestry only, never opens the key.
    owner=PreviewJobs()
    intent=dict(copy_id=copy_id,identity=identity)
    return owner.dispatch(profile,request_id,intent,
                          lambda job,nonce:_launch(profile,job,nonce))


def _launch(profile, job, nonce):
    """Reuse the reviewed native runtime pin and private package-copy pattern."""
    from . import default_recovery as cold
    root=authority(profile)
    directory=core.private_dir(root/'cache/finix/default-preview/dispatch'/identifier(job))
    package=core.private_dir(directory/'package/finix')
    source=Path(__file__).resolve().parent
    def copy(origin,target):
        if target.exists():
            if core.digest_file(origin)!=core.digest_file(target):raise RecoveryError('PINNED_PACKAGE_CHANGED')
        else:cold._copy(origin,target)
    for path in sorted(source.glob('*.py')):copy(path,package/path.name)
    for name in ('__init__.py','plugin.yaml','dashboard/plugin_api.py'):
        copy(source.parent/name,package.parent/name)
    from .workspace_drive import trusted_runtime
    import hermes_constants
    sdk=Path(hermes_constants.__file__).resolve().parent
    env=dict(os.environ)
    env.pop('PYTHONPATH',None);env.pop('HERMES_PROFILE',None)
    env.update(HERMES_HOME=str(root),PYTHONDONTWRITEBYTECODE='1')
    kwargs={'start_new_session':True} if os.name!='nt' else {'creationflags':subprocess.DETACHED_PROCESS|subprocess.CREATE_NEW_PROCESS_GROUP}
    worker=package/'default_preview_worker.py'
    with trusted_runtime(worker,entry=worker) as (prefix,entry):
        return subprocess.Popen([*prefix,entry,str(sdk),job,nonce],cwd=sdk,env=env,
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            close_fds=True,**kwargs)


def execute(profile, job):
    """Called ONLY inside the detached owner's kernel operation reservation."""
    from .default_recovery_api import DefaultRecoveryAPI,new_service,activation
    from .recovery_http import explicit_path,trusted_file,MAX_KEY
    from .preview_budget import check
    owner=PreviewJobs();root,base,path=owner._paths(profile,job)
    from .backup_jobs import state_lease
    with state_lease(base/'state.lock'):
        intent=owner._read(profile,root,path)['intent']
    check()
    identity=explicit_path(intent['identity'])
    with trusted_file(identity) as fd:
        if not 0<os.fstat(fd).st_size<=MAX_KEY:raise RecoveryError('INVALID_IDENTITY')
    check()
    service=new_service(profile)
    try:
        generation=activation(profile)
        service.catalog(profile.profile_id,generation)
        destination=service.allocate_destination(profile.profile_id,generation)
        data_job=service.start(intent['copy_id'],identity=identity,destination=destination,
                               profile_id=profile.profile_id,activation=generation)
        try:
            while True:
                state=service.wait(data_job,timeout=.1)
                check()
                if state['status']!='running':break
        except BaseException:
            service.cancel(data_job);service.wait(data_job)
            raise
        if state['status']!='succeeded':raise RecoveryError('DECRYPT_FAILED')
        # The data worker has joined and released its backend lease. The global
        # operation reservation stays held, so no second Restore can begin.
        with core.locked(profile.directories()):
            result=DefaultRecoveryAPI().prepare(profile,service,data_job,consent=True)
        # Keep the genuine handoff/staging on uncertainty; no unreviewed cleanup
        # of the cold engine's source or consent authority after publication.
        return result
    finally:service.close()
