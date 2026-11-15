"""True-default maintenance. Mount only under host authenticated profile scope."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import uuid
from . import core, default_recovery as cold
from .preview_budget import cancellable
RecoveryError = cold.RecoveryError


def activation(profile):
    from .recovery_http import binding
    return binding(profile)


def authority(profile):
    raise RecoveryError('PUBLIC_RECOVERY_CONTRACT_UNAVAILABLE')


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{32}', value):
        raise RecoveryError('NOT_FOUND')
    return value


def token(challenge):
    return hashlib.sha256(json.dumps(challenge, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def public_error(exc):
    allowed = {'PUBLIC_RECOVERY_CONTRACT_UNAVAILABLE','CONSENT_REQUIRED','LOCAL_OWNER_REQUIRED','NOT_FOUND','STALE_PROFILE',
               'ROOT_EPOCH_CHANGED','WRONG_DEFAULT_ROOT','SDK_VERSION_NOT_SUPPORTED',
               'DEFAULT_AUTORUN_NOT_SUPPORTED','SIBLING_AUTORUN_NOT_SUPPORTED',
               'OWNER_NOT_VERIFIED','REOPEN_LIVE_OWNER_UNVERIFIED','BUSY',
               'INVALID_CAPABILITY','INVALID_CANDIDATE','INVALID_IDENTITY',
               'DECRYPT_FAILED','INCOMPLETE_ACK_REQUIRED','JOB_NOT_PREPARED',
               'DEFAULT_RECOVERY_REVIEW_REQUIRED','PREVIEW_DEADLINE',
               'PREVIEW_CANCELLED','PREVIEW_INTERRUPTED','PREVIEW_ACK_UNCERTAIN'}
    code = getattr(exc,'code',None)
    return code if code in allowed else 'DEFAULT_RECOVERY_FAILED'


DEFAULT_APPLY_REVIEWED = False


def new_service(profile):
    raise RecoveryError('PUBLIC_RECOVERY_CONTRACT_UNAVAILABLE')


class DefaultRecoveryAPI:
    """Explicit retirement gate; never resume, rewrite or delete old journals."""
    def prepare(self, profile, service, job, *, consent=False):
        raise RecoveryError('PUBLIC_RECOVERY_CONTRACT_UNAVAILABLE')

    def apply(self, profile, opaque, confirmation, *, consent=False):
        raise RecoveryError('PUBLIC_RECOVERY_CONTRACT_UNAVAILABLE')

    def status(self, profile, opaque):
        from .recovery_availability import capability
        return dict(capability(), id=identifier(opaque), state='unavailable', historical_only=True)

    def reopen(self, profile, opaque, *, consent=False):
        raise RecoveryError('PUBLIC_RECOVERY_CONTRACT_UNAVAILABLE')
