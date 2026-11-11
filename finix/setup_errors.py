"""Closed setup diagnostics. Never serialize exception text or provider output."""
from .core import FinixError
from .rclone_storage import RcloneError
import sqlite3


def setup_error_code(error):
    if isinstance(error, sqlite3.Error):
        return 'SETUP_SQLITE_FAILED'
    if isinstance(error, RcloneError):
        return {
            'BINARY_MISSING': 'SETUP_DEPENDENCY_UNAVAILABLE',
            'UNTRUSTED_BINARY': 'SETUP_DEPENDENCY_UNAVAILABLE',
            'SHORT_DOWNLOAD': 'SETUP_READBACK_FAILED',
            'CONFIG_UNAVAILABLE': 'SETUP_CLOUD_CONFIG_UNAVAILABLE',
            'CONFIG_PERMISSIONS': 'SETUP_CLOUD_CONFIG_UNAVAILABLE',
            'AUTH_REQUIRED': 'SETUP_CLOUD_AUTH_REQUIRED',
            'ACCESS_DENIED': 'SETUP_CLOUD_ACCESS_DENIED',
            'QUOTA_EXCEEDED': 'SETUP_CLOUD_QUOTA_EXCEEDED',
            'RATE_LIMITED': 'SETUP_CLOUD_RATE_LIMITED',
            'CONNECTION_FAILED': 'SETUP_CLOUD_CONNECTION_FAILED',
            'PENDING_RECONCILIATION': 'SETUP_CLOUD_RECONCILIATION_REQUIRED',
            'TIMEOUT': 'SETUP_CLOUD_TIMEOUT',
            'PROVIDER_ERROR': 'SETUP_CLOUD_FAILED',
            'NOT_FOUND': 'SETUP_CLOUD_FAILED',
            'OUTPUT_LIMIT': 'SETUP_CLOUD_FAILED',
        }.get(error.code, 'SETUP_CHECK_FAILED')
    if isinstance(error, PermissionError):
        return 'SETUP_ACCESS_DENIED'
    if isinstance(error, FinixError):
        # Exact internal messages only: no substring matching untrusted text.
        groups = {
            'SETUP_SOURCE_CHANGED': {
                'Source changed during discovery', 'Source changed before read',
                'Source changed during snapshot', 'Source replaced during snapshot',
                'SQLite source replaced', 'SQLite companion replaced during snapshot',
                'Profile changed during snapshot; retry when quiescent',
                'Source changed during multi-root snapshot',
                'SOURCE_UNAVAILABLE_BEFORE_CAPTURE', 'SOURCE_UNSTABLE_DURING_CAPTURE',
                'SOURCE_UNSAFE_DURING_CAPTURE', 'SOURCE_REPLACED_DURING_CAPTURE',
                'SOURCE_CLASSIFICATION_CHANGED_DURING_CAPTURE',
                'Setup changed during backup; check and run again',
                'Setup changed during verification; check and run again',
            },
            'SETUP_READBACK_FAILED': {
                'Ciphertext readback mismatch', 'Ciphertext readback mismatch; retention not run',
                'Managed readback mismatch; no retention',
                'Local ciphertext readback mismatch',
                'Drive commit readback mismatch; retention not run',
                'Encrypted output readback verification failed',
                'Completed archive changed; check and run again',
            },
            'SETUP_SQLITE_FAILED': {'SQLite source validation failed',
                'SQLite consistent snapshot failed; check temporary storage free space and database health'},
            'SETUP_DEPENDENCY_UNAVAILABLE': {'DEPENDENCY_UNAVAILABLE'},
            'SETUP_SQLITE_TEMP_REQUIRED': {'NEED_SQLITE_TEMP_DIRECTORY'},
            'SETUP_SQLITE_TEMP_SPACE': {'NOT_ENOUGH_SQLITE_TEMP_SPACE'},
        }
        for code, messages in groups.items():
            if str(error) in messages:
                return code
    return 'SETUP_CHECK_FAILED'
