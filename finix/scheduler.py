"""Model-free scheduling owned by plugin lifecycle, or an optional standalone CLI."""
import argparse
import hashlib
import json
import logging
from pathlib import Path
import threading
import time

from .integration import Profile, current_profile
from .core import FinixError, locked, private_dir

log = logging.getLogger(__name__)
POLL_SECONDS = 60


class Worker:
    """One worker per plugin load; profile scope never comes from a later caller.

    Stop prevents further checks. An admitted backup drains nonpreemptively: killing
    a thread during publication/retention is unsafe. Unload reports a bounded drain
    timeout explicitly; source locks fence a replacement worker meanwhile.
    """
    def __init__(self, home):
        self.home = Path(home).expanduser().resolve()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, name='finix-scheduler', daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=1)
        if self.thread.is_alive():
            log.warning('Finix scheduler stopping: current backup is still draining; '
                        'no further backups will start from this worker')
        return not self.thread.is_alive()

    def check(self):
        # Do not initialize a backend or scan profile data for an unconfigured
        # plugin. Profile.backup rechecks durable due state under its own lock.
        binding = hashlib.sha256(str(self.home).encode()).hexdigest()
        settings = self.home.parent / 'finix-data' / binding / 'settings.json'
        if not settings.exists() or self.stop_event.is_set():
            return
        profile = Profile(self.home)
        cfg = profile.config()
        if (not cfg['interval_hours'] or cfg['next_run'] > time.time()
                or self.stop_event.is_set()):
            return
        profile.backup(scheduled=True)

    def run(self):
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        token = set_hermes_home_override(self.home)
        try:
            # No load-time backup, even for overdue schedules. Every failure gets
            # the same bounded retry cadence; never loop through missed intervals.
            while not self.stop_event.wait(POLL_SECONDS):
                try:
                    self.check()
                except Exception:
                    # Provider exceptions may carry credentials or response bodies.
                    log.warning('Finix scheduled backup failed; retry in 60 seconds')
        finally:
            reset_hermes_home_override(token)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true', help='One due check; exit')
    args = parser.parse_args()
    profile = current_profile()
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(profile.home)
    try:
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
                time.sleep(POLL_SECONDS)
    finally:
        reset_hermes_home_override(token)


if __name__ == '__main__':
    main()
