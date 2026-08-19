from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from agent_sdk.masque import AioquicConnectIpTransport
from agent_sdk.logging_utils import close_logger, configure_local_logger
from agent_sdk.proxy import (
    MasqueProxyServer,
    ProxySessionPolicy,
    TokenSessionResolver,
)
from test_proxy import MemoryUeAdapter, ipv4_packet


def certificate_pair(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost")]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_pem = certificate.public_bytes(serialization.Encoding.PEM)
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    cert_path.write_bytes(cert_pem)
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path, cert_pem


async def test_real_http3_connect_ip_datagrams_round_trip(tmp_path):
    cert_path, key_path, cert_pem = certificate_pair(tmp_path)
    log_path = tmp_path / "connect-ip.log"
    logger = configure_local_logger(
        name=f"test.connect_ip.{id(tmp_path)}",
        file_path=str(log_path),
        level="INFO",
        max_bytes=1024 * 1024,
        backup_count=1,
    )
    adapter = MemoryUeAdapter()
    policy = ProxySessionPolicy(
        "8.8.8.7",
        ("8.8.8.8/32",),
        1280,
        lambda: adapter,
    )
    server = MasqueProxyServer(
        "127.0.0.1",
        0,
        str(cert_path),
        str(key_path),
        TokenSessionResolver({"secret-a": policy}),
        logger=logger,
    )
    await server.start()
    downlink = asyncio.Queue()
    client = AioquicConnectIpTransport(
        server_url=f"https://127.0.0.1:{server.bound_port}",
        server_name="localhost",
        ca_certificate_pem=cert_pem,
        authorization="Bearer secret-a",
        local_address="127.0.0.1",
        logger=logger,
    )
    try:
        await client.start(downlink.put)
        uplink = ipv4_packet("8.8.8.7", "8.8.8.8")
        await client.send_packet(uplink)
        for _ in range(100):
            if adapter.uplink:
                break
            await asyncio.sleep(0.01)
        assert adapter.uplink == [uplink]

        expected = ipv4_packet("8.8.8.8", "8.8.8.7")
        await adapter.downlink.put(expected)
        assert await asyncio.wait_for(downlink.get(), timeout=2) == expected
    finally:
        await client.close()
        await server.close()
        close_logger(logger)

    text = log_path.read_text(encoding="utf-8")
    assert text.count('"event":"http_request"') >= 2
    assert text.count('"event":"http_response"') >= 2
    assert '"protocol":"HTTP/3 CONNECT-IP"' in text
    assert '"status_code":200' in text
    assert "secret-a" not in text
