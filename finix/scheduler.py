"""Explicit script-only service; schedule is durable, execution requires this process.

Run under the intended profile using HERMES_HOME and python -m finix.scheduler.
No Hermes cron records are created, no model is invoked, no service is installed.
"""
import argparse
import json
import time
from .integration import current_profile
from .core import FinixError, locked


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true', help='One due check; exit')
    args = parser.parse_args()
    profile = current_profile()  # Bound once, never ambiently reread from a worker.
    from .core import private_dir
    private_dir(profile.root / 'service')
    with locked(profile.root / 'service'):
        while True:
            try:
                result = profile.backup(scheduled=True)
                print(json.dumps({'profile_id': profile.profile_id, 'result': result}), flush=True)
            except (FinixError, OSError, RuntimeError):
                print(json.dumps({'profile_id': profile.profile_id, 'error': 'scheduled backup failed'}), flush=True)
            if args.once:
                return
            time.sleep(60)


if __name__ == '__main__':
    main()
