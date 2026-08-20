from __future__ import annotations

import inspect
import os

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from agent_sdk.errors import AgentSdkError
from agent_sdk.identity import ClientTlsIdentityStore, embedded_masque_root_ca_pem
from agent_sdk.sdk import AgentSdk


def test_client_tls_identity_is_generated_once_and_protected(tmp_path):
    store = ClientTlsIdentityStore(tmp_path / "identity")
    first = store.ensure()
    second = store.ensure()

    assert first.public_key_sha256 == second.public_key_sha256
    assert first.certificate_path.stat().st_mode & 0o777 == 0o600
    assert first.private_key_path.stat().st_mode & 0o777 == 0o600
    private_key = serialization.load_pem_private_key(
        first.private_key_path.read_bytes(), password=None
    )
    certificate = x509.load_pem_x509_certificate(first.certificate_path.read_bytes())
    assert isinstance(private_key, ed25519.Ed25519PrivateKey)
    assert (
        private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        == certificate.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    )


def test_partial_client_tls_identity_is_rejected(tmp_path):
    directory = tmp_path / "identity"
    directory.mkdir()
    (directory / "client-key.pem").write_text("broken", encoding="utf-8")
    with pytest.raises(AgentSdkError, match="incomplete"):
        ClientTlsIdentityStore(directory).ensure()


def test_embedded_masque_root_is_a_ca_certificate():
    certificate = x509.load_pem_x509_certificate(embedded_masque_root_ca_pem())
    assert certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission semantics required")
def test_identity_directory_is_private(tmp_path):
    directory = tmp_path / "identity"
    ClientTlsIdentityStore(directory).ensure()
    assert directory.stat().st_mode & 0o777 == 0o700


def test_sdk_init_does_not_expose_tls_certificate_or_private_key_parameters():
    parameters = inspect.signature(AgentSdk.init).parameters
    assert "masque_server_name" not in parameters
    assert "masque_ca_certificate_pem" not in parameters
    assert "client_certificate" not in parameters
    assert "client_private_key" not in parameters
