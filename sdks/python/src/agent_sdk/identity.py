from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .errors import AgentSdkError, ErrorCode


@dataclass(frozen=True, slots=True)
class ClientTlsIdentity:
    certificate_path: Path
    private_key_path: Path
    public_key_sha256: str


class ClientTlsIdentityStore:
    """Creates one persistent, SDK-owned TLS client identity per Linux user."""

    def __init__(self, directory: Path | None = None) -> None:
        self._directory = directory or self._default_directory()

    @staticmethod
    def _default_directory() -> Path:
        state_home = os.environ.get("XDG_STATE_HOME")
        base = Path(state_home) if state_home else Path.home() / ".local" / "state"
        return base / "agent-sdk" / "tls"

    def ensure(self) -> ClientTlsIdentity:
        certificate_path = self._directory / "client-cert.pem"
        private_key_path = self._directory / "client-key.pem"
        self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self._directory, 0o700)

        if certificate_path.exists() != private_key_path.exists():
            raise AgentSdkError(
                ErrorCode.MASQUE_CONNECT_FAILED,
                "SDK TLS identity is incomplete; restore or remove both identity files",
            )
        if not certificate_path.exists():
            self._generate(certificate_path, private_key_path)

        try:
            certificate = x509.load_pem_x509_certificate(certificate_path.read_bytes())
            private_key = serialization.load_pem_private_key(
                private_key_path.read_bytes(), password=None
            )
            if not isinstance(private_key, ed25519.Ed25519PrivateKey):
                raise ValueError("client key is not Ed25519")
            public_der = private_key.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            certificate_public_der = certificate.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            if public_der != certificate_public_der:
                raise ValueError("certificate and private key do not match")
        except (OSError, ValueError) as exc:
            raise AgentSdkError(
                ErrorCode.MASQUE_CONNECT_FAILED,
                f"invalid SDK TLS identity: {exc}",
            ) from exc

        os.chmod(certificate_path, 0o600)
        os.chmod(private_key_path, 0o600)
        return ClientTlsIdentity(
            certificate_path=certificate_path,
            private_key_path=private_key_path,
            public_key_sha256=sha256(public_der).hexdigest(),
        )

    @staticmethod
    def _generate(certificate_path: Path, private_key_path: Path) -> None:
        private_key = ed25519.Ed25519PrivateKey.generate()
        public_der = private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        identity_suffix = sha256(public_der).hexdigest()[:16]
        subject = x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, f"agent-sdk-{identity_suffix}")]
        )
        now = datetime.now(timezone.utc)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(private_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), False
            )
            .sign(private_key, algorithm=None)
        )
        key_pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        certificate_pem = certificate.public_bytes(serialization.Encoding.PEM)
        ClientTlsIdentityStore._atomic_write(private_key_path, key_pem, 0o600)
        ClientTlsIdentityStore._atomic_write(certificate_path, certificate_pem, 0o600)

    @staticmethod
    def _atomic_write(path: Path, content: bytes, mode: int) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except BaseException:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise
