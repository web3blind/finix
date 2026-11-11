"""Mode decision tests retain the stock SDK selector (not a mocked registry)."""
from contextlib import ExitStack
from types import SimpleNamespace
import pytest
from test_recovery_history_ownership import fenced_root


def test_cooperative_root_bootstrap_uses_stock_selector_without_owned_child(tmp_path,monkeypatch):
    from finix import core,recovery_pipeline as p
    from hermes_cli import profiles
    root=profiles.get_profile_dir('default');target=core.private_dir(profiles.get_profile_dir('recovered-new'))
    core.atomic_json(target/'config.yaml',{'model':{}})
    (target/'gateway.parked').write_text('parked')
    record={'target':target,'root':root,'name':'recovered-new','service':object(),'job':'a'*32,'digest':p.profile_digest(target)}
    monkeypatch.setattr(p,'_admit',lambda cap:record)
    class Shared:
        def check(self):return None
        def ready(self,timeout=60):return {'shared':True}
        def ensure(self):assert 'recovered-new' in dict(profiles.profiles_to_serve(True))
    shared=Shared()
    monkeypatch.setattr(p,'_shared_host',lambda record:shared,raising=False)
    def forbidden(*a,**k):
        assert 'recovered-new' in dict(profiles.profiles_to_serve(True)), 'actual SDK root selector excludes bootstrapped target'
        raise AssertionError('root-multiplex MUST NOT launch standalone')
    monkeypatch.setattr(p,'_TargetRuntime',forbidden)
    result=p._bootstrap(object())
    assert result['runtime'] is shared
    assert not (target/'gateway.parked').exists()
    assert 'recovered-new' in dict(profiles.profiles_to_serve(True))
    assert not p._OWNED_TARGETS


def test_shared_cleanup_never_owns_root_child():
    from finix.recovery_shared_host import SharedTargetRuntime
    assert not hasattr(SharedTargetRuntime,'child')
    assert not hasattr(SharedTargetRuntime,'cleanup')
