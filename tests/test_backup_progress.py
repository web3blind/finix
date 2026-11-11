import unittest
from unittest.mock import Mock, patch
from finix.setup_flow import SetupFlow
from finix.integration import Profile

class ProgressContracts(unittest.TestCase):
    def test_optional_callbacks_preserve_cli_signatures(self):
        import inspect
        self.assertIn('progress', inspect.signature(SetupFlow.check_and_run).parameters)
        self.assertIn('cancel', inspect.signature(SetupFlow.check_and_run).parameters)
        self.assertIn('progress', inspect.signature(Profile.backup).parameters)
        self.assertIn('cancel', inspect.signature(Profile.backup).parameters)

if __name__ == '__main__': unittest.main()
