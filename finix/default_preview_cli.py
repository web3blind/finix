"""Physical-default preview creation retired with the private lifecycle."""
from .recovery_availability import require_public_contract


def start(profile, request_id, copy_id, identity):
    require_public_contract()


def execute(profile, job):
    require_public_contract()
