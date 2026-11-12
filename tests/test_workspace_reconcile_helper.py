"""Preallocated IDs/identity now travel through public credentialed requests."""
from test_reviewer_provider_contract import ProviderContract
import unittest

class PublicRestProtocol(unittest.TestCase):
    def test_identity_ids_and_metadata_use_public_transport(self):
        ProviderContract('test_public_transport_identity_id_and_metadata').test_public_transport_identity_id_and_metadata()

    def test_preallocated_ids_survive_journal_restart(self):
        ProviderContract('test_journal_composes_public_rest_transport_with_full_cipher_readback').test_journal_composes_public_rest_transport_with_full_cipher_readback()
