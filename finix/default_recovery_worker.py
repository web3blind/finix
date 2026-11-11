"""Pinned detached entry. SDK source is supplied only by the server launcher."""
import os
from pathlib import Path
import sys


def main():
    sdk,job=sys.argv[1:]
    package=Path(__file__).resolve().parent.parent
    sys.path[:0]=[str(package),sdk]
    def guard(event,args):
        # Controller has no provider/installer/network role. Gateway children
        # still use the normal SDK invocation and independent admission.
        if event in ('socket.connect','socket.bind','socket.sendto'):
            import socket
            if args[0].family != socket.AF_UNIX:
                raise PermissionError('CONTROLLER_EXTERNAL_NETWORK_FORBIDDEN')
        if event=='socket.getaddrinfo': raise PermissionError('CONTROLLER_DNS_FORBIDDEN')
        if event in ('os.exec','os.posix_spawn','os.system'):
            raise PermissionError('CONTROLLER_REEXEC_FORBIDDEN')
        if event=='subprocess.Popen':
            import shlex
            executable=args[0] or (args[1][0] if isinstance(args[1],(list,tuple)) else shlex.split(args[1],posix=False)[0].strip('"'))
            name=Path(executable).name.lower()
            if name not in ('python','python3','python.exe','pythonw.exe'):
                raise PermissionError('CONTROLLER_PROVIDER_FORBIDDEN')
    sys.addaudithook(guard)
    from hermes_constants import get_default_hermes_root
    from hermes_cli.profiles import get_profile_dir
    root=get_default_hermes_root()
    if not os.path.samefile(root,get_profile_dir('default')):
        raise RuntimeError('WRONG_DEFAULT_ROOT')
    from finix.default_recovery_lifecycle import run_job,_job,_read_status,_write_status
    try:
        run_job(root,job)
    except Exception as error:
        from finix import default_recovery as cold
        directory=_job(root,job)
        status=_read_status(directory)
        if status.get('state') not in ('ready','rolled_back_ready','rollback_not_ready','manual_required','reconciliation_required'):
            prior=status.get('state')
            _write_status(directory,{'state':'refused' if prior in ('registered','acknowledged') else 'interrupted',
                                                'error':error.code if isinstance(error,cold.RecoveryError) else 'CONTROLLER_FAILED'})
        raise


if __name__=='__main__': main()
