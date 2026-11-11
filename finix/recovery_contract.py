"""Public recovery projection; internal handoff paths never cross HTTP."""
from pathlib import Path
from .recovery_import import _safe_config, _read, MAX_CONFIG


def public_job(service, identifier, profile_id, state):
    status = state['status']
    result = state.get('result') or {}
    ready = False
    if status == 'succeeded' and type(result.get('complete')) is bool:
        try:
            handoff = service.handoff(identifier, profile_id, service.activation)
            bundle, profile = Path(handoff['bundle_root']), Path(handoff['profile_root'])
            if profile in (bundle, bundle / 'profile'):
                _safe_config(_read(profile / 'config.yaml', MAX_CONFIG))
                ready = True
        except Exception:
            pass  # Extraction remains successful even when not a usable profile.
    return dict(status=status, state=status, phase=state.get('phase', status),
                completion_scope='archive', archive_verified=status=='succeeded',
                content_verified=False, backend_target_ready=False, full_acceptance=False,
                bytes=max(state.get('bytes', 0),state.get('download_bytes',0)),
                progress=100 if status == 'succeeded' else (min(90,int(90*state.get('download_bytes',0)/max(1,state.get('download_total',1)))) if status=='running' else 0),
                error=state.get('error'), code=state.get('error'),
                complete=result.get('complete'), omitted_count=result.get('omitted_count'),
                can_import=ready, extras_policy='separate_manual_import_only')
