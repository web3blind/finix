"""Catalog recovery capability: offline data is not live runtime authority."""
CODE = 'PUBLIC_RECOVERY_CONTRACT_UNAVAILABLE'
MESSAGE = ('Automatic profile import/activation and physical-default replacement are unavailable: '
           'Hermes has no supported public contract for the required atomic publication and writer quiescence. '
           'Use offline inert restore to an empty directory; retained operations require manual review. '
           'Stock Hermes import is a manual merge/overwrite fallback, may auto-start services and restore external files, '
           'and is not exact transactional replacement.')


def capability():
    return dict(supported=False, apply_enabled=False, error=CODE, message=MESSAGE,
                offline_restore_available=True, catalog_available=True,
                retained_state_policy='preserve-no-retry-no-adoption')


def require_public_contract():
    from .recovery_import import RecoveryImportError
    raise RecoveryImportError(CODE)
