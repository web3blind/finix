"""Runner tooling must be shared, while profile data remains isolated."""
import hashlib,json,os
from pathlib import Path
from test_recovery_history_ownership import fenced_root

def test_fixture_uses_one_owned_pm_generation():
    import hermes_constants
    from pm.environments import selected_venv,venv_command
    sdk=Path(hermes_constants.__file__).parent
    key=hashlib.sha256(str(sdk.resolve()).encode()).hexdigest()[:16]
    facts=Path(os.environ['FINIX_BOUND_SCOPE'])/'sdk-root/installs'/key/'facts.json'
    expected=Path(json.loads(facts.read_text())['packages']['venv']['environment']).resolve()
    actual=selected_venv(sdk)
    assert actual.resolve()==expected,'per-case full PM copies consume host backing VHD'
    assert Path(venv_command(sdk,actual)[0]).parent.parent.resolve()==expected
