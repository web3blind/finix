"""Exact native process-death seam for opaque DATA transaction tests only."""
import os,sys
from pathlib import Path
from unittest.mock import patch
from hermes_constants import get_default_hermes_root,get_hermes_home
from hermes_cli.profiles import get_profile_dir
root=Path(os.environ['FINIX_DEFAULT_TEST_SCOPE'])/'sdk-root'
assert root==get_default_hermes_root()==get_hermes_home()==get_profile_dir('default')
from finix import default_recovery as cold,runtime_attempt as custody,sqlite_family as family
engine=cold.DefaultRecovery(Path(sys.argv[1]),sys.argv[2]);label,boundary=sys.argv[3:]
original=family._move

def die(this,source,target,leaf):
    current=('copy-current' if target.parent.name=='copies' else
             'move-current' if target.parent.name=='custody-originals' else
             'restore-old-main' if source.parent.name=='sqlite-quarantine' and source.name=='state.db' else None)
    if current==label and boundary=='before':os._exit(73)
    original(this,source,target,leaf)
    if current==label and boundary=='after':os._exit(73)

copy_original=custody._copy_blob
def copy_die(this,source,target,**kwargs):
    matched=label=='copy-current' and target.parent.name=='copies'
    if matched and boundary=='before':os._exit(73)
    copy_original(this,source,target,**kwargs)
    if matched and boundary=='after':os._exit(73)

with patch.object(custody,'require_stopped_attempt',return_value=None),patch.object(family,'_move',die),patch.object(custody,'_copy_blob',copy_die):
    engine.rollback_attempt(engine.consent(),None)
raise AssertionError('DEATH_SEAM_NOT_REACHED')
