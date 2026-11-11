"""Host-authenticated Desktop-local recovery adapter.

Mount ONLY below Hermes' authenticated, profile-scoped plugin router. Loopback
is defense in depth, NOT proof against SSH/proxy routing. The renderer MUST
verify the reserved local connection synchronously before every sensitive REST
call. Folder.path is untrusted explicit consent input, never a native grant.
"""
from .recovery_http import create_router as create_router
