"""Pinned detached CLI entry. No serialized key/service/capability is admitted."""
from pathlib import Path
import sys


def main():
    sdk,job,nonce=sys.argv[1:]
    sys.path[:0]=[str(Path(__file__).resolve().parent.parent),sdk]
    from finix.integration import current_profile
    from finix.default_recovery_api import authority,identifier
    from finix.default_preview_jobs import PreviewJobs
    from finix.default_preview_cli import execute
    profile=current_profile()
    authority(profile);identifier(job);identifier(nonce)
    PreviewJobs().claim(profile,job,nonce,lambda:execute(profile,job))


if __name__=='__main__':main()
