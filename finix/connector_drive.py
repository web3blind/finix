"""Removed unsupported Managed Drive backend; legacy settings/data are preserved."""
from .core import FinixError

ACTIONS = {"BACKEND_REMOVED": "Managed Drive is unsupported. Explicitly select Google Drive or rclone; existing data and settings are preserved."}

class ConnectorError(FinixError):
    def __init__(self, code="BACKEND_REMOVED"):
        self.code = "BACKEND_REMOVED"
        super().__init__(ACTIONS[self.code])
