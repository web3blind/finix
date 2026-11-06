"""Explicit paths only; never discovers or modifies the active Hermes profile."""
import argparse
import json
import sqlite3
import subprocess
import sys
from . import core


def main(argv=None):
    parser = argparse.ArgumentParser(prog='finix')
    commands = parser.add_subparsers(dest='command', required=True)
    from .connect_cli import add_parser
    from .connections import ConnectionError
    add_parser(commands)
    keygen = commands.add_parser('keygen', help='Create an owner-only identity; print public recipient only')
    keygen.add_argument('--identity', required=True)
    backup = commands.add_parser('backup', help='Create and verify a local encrypted snapshot')
    for option in ('source', 'output', 'state', 'recipient'):
        backup.add_argument('--' + option, required=True)
    backup.add_argument('--keep', type=int, help='Explicit retention in this Finix namespace')
    restore = commands.add_parser('restore', help='Restore into an absent or empty directory; cron paused')
    for option in ('archive', 'identity', 'target'):
        restore.add_argument('--' + option, required=True)
    status = commands.add_parser('status', help='Read back checksums of indexed encrypted backups')
    status.add_argument('--output', required=True)
    status.add_argument('--state', required=True)
    args = vars(parser.parse_args(argv))
    command = args.pop('command')
    try:
        if command == 'connect':
            from .connect_cli import interactive
            return interactive(**args)
        if command == 'keygen':
            result = {'recipient': core.keygen(args['identity'])}
        else:
            result = getattr(core, command)(**args)
        print(json.dumps(result, sort_keys=True))
        return 0
    except ConnectionError as exc:
        print('finix connect: ' + exc.code, file=sys.stderr)
        return 1
    except (core.FinixError, OSError, ValueError, sqlite3.Error, subprocess.SubprocessError):
        # Library errors may contain paths or contents: do not echo untrusted diagnostics.
        print('finix: operation failed; no secret diagnostics emitted', file=sys.stderr)
        return 1
