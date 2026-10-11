from __future__ import annotations

import asyncio
import ipaddress
import struct

import pytest
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.connection import QuicConnection
from aioquic.quic.crypto import CryptoPair
from aioquic.quic.packet import QuicFrameType, QuicPacketType, QuicProtocolVersion
from aioquic.quic.packet_builder import QuicPacketBuilder, QuicPacketBuilderStop

import agent_sdk.masque as masque_module
from agent_sdk.identity import ClientTlsIdentityStore
from agent_sdk.masque import AioquicConnectIpTransport, ConnectIpQuicProtocol
from agent_sdk.tun import _internet_checksum, packet_too_big_response, validate_ip_packet


def ipv4_packet(size: int, protocol: int = 6) -> bytes:
    header = struct.pack(
        "!BBHHHBBH4s4s", 0x45, 0, size, 7, 0x4000, 64, protocol, 0,
        ipaddress.ip_address("10.60.0.3").packed,
        ipaddress.ip_address("100.123.44.87").packed,
    )
    header = header[:10] + struct.pack("!H", _internet_checksum(header)) + header[12:]
    return header + bytes(size - 20)


def ipv6_packet(size: int, protocol: int = 17) -> bytes:
    return (
        struct.pack("!IHBB", 6 << 28, size - 40, protocol, 64)
        + ipaddress.ip_address("2001:db8::3").packed
        + ipaddress.ip_address("2001:db8::4").packed
        + bytes(size - 40)
    )


def make_protocol(maximum: int = 1400, mtu: int = 1280) -> ConnectIpQuicProtocol:
    quic = QuicConnection(configuration=QuicConfiguration(
        is_client=True, max_datagram_size=maximum, max_datagram_frame_size=65536,
    ))
    quic._remote_max_datagram_frame_size = 65536
    protocol = ConnectIpQuicProtocol(quic, ip_packet_mtu=mtu)
    protocol.connect_stream_id = 0
    protocol.transmit = lambda: None  # type: ignore[method-assign]
    return protocol


@pytest.mark.parametrize("mtu", [1280, 1500])
async def test_transport_capacity_tracks_inner_mtu(monkeypatch, tmp_path, mtu):
    captured = []
    class FailingContext:
        async def __aenter__(self):
            raise RuntimeError("stop before opening a socket")
        async def __aexit__(self, *args):
            return None
    def connect(*args, **kwargs):
        captured.append(kwargs["configuration"])
        return FailingContext()
    monkeypatch.setattr(masque_module, "connect", connect)
    transport = AioquicConnectIpTransport(
        server_url="https://127.0.0.1:8444", ip_packet_mtu=mtu,
        identity_store=ClientTlsIdentityStore(tmp_path / "tls"),
    )
    with pytest.raises(Exception, match="stop before opening"):
        await transport.start(lambda packet: asyncio.sleep(0))
    configuration = captured[0]
    assert configuration.max_datagram_size == mtu + 120
    assert configuration.max_datagram_frame_size == 65536

    # Use the real aioquic packet builder: reproduce the original failure and
    # prove a complete HTTP Datagram containing a full-MTU IP packet now fits.
    crypto = CryptoPair()
    crypto.setup_initial(cid=b"12345678", is_client=True, version=QuicProtocolVersion.VERSION_1)
    for maximum in (1200, configuration.max_datagram_size):
        builder = QuicPacketBuilder(
            host_cid=b"12345678", peer_cid=b"87654321",
            version=QuicProtocolVersion.VERSION_1, is_client=True,
            max_datagram_size=maximum,
        )
        builder.start_packet(QuicPacketType.ONE_RTT, crypto)
        q = QuicConnection(configuration=configuration)
        if maximum == 1200:
            with pytest.raises(QuicPacketBuilderStop):
                q._write_datagram_frame(builder=builder, data=b"\0\0" + ipv4_packet(mtu),
                                        frame_type=QuicFrameType.DATAGRAM_WITH_LENGTH)
        else:
            assert q._write_datagram_frame(builder=builder, data=b"\0\0" + ipv4_packet(mtu),
                                           frame_type=QuicFrameType.DATAGRAM_WITH_LENGTH)


async def test_full_mtu_packet_is_enqueued_and_oversized_packet_cannot_block_next():
    protocol = make_protocol()
    packet = ipv4_packet(1280)
    assert protocol.send_ip_packet(packet) is None
    assert list(protocol._quic._datagrams_pending) == [b"\0\0" + packet]
    feedback = protocol.send_ip_packet(ipv4_packet(1281))
    assert feedback is not None
    assert len(protocol._quic._datagrams_pending) == 1
    small = ipv4_packet(60)
    protocol.send_ip_packet(small)
    assert list(protocol._quic._datagrams_pending) == [b"\0\0" + packet, b"\0\0" + small]


async def test_peer_frame_limit_generates_feedback_before_enqueueing():
    protocol = make_protocol()
    protocol._quic._remote_max_datagram_frame_size = 1000
    feedback = protocol.send_ip_packet(ipv4_packet(1280))
    assert feedback is not None
    assert int.from_bytes(feedback[26:28], "big") == 980
    assert not protocol._quic._datagrams_pending
    protocol.send_ip_packet(ipv4_packet(60))
    assert len(protocol._quic._datagrams_pending) == 1


async def test_congested_queue_is_bounded_and_can_resume(monkeypatch):
    protocol = make_protocol()
    monkeypatch.setattr(masque_module, "MAX_PENDING_IP_DATAGRAMS", 2)
    for _ in range(3):
        protocol.send_ip_packet(ipv4_packet(60))
    assert len(protocol._quic._datagrams_pending) == 2
    protocol._quic._datagrams_pending.popleft()
    protocol.send_ip_packet(ipv4_packet(60))
    assert len(protocol._quic._datagrams_pending) == 2


def udp_packet(payload: bytes) -> bytes:
    packet = bytearray(ipv4_packet(28 + len(payload), 17))
    packet[20:28] = struct.pack("!HHHH", 5000, 5001, 8 + len(payload), 0)
    packet[28:] = payload
    return bytes(packet)


async def test_consent_rtcp_and_tcp_pass_queued_rtp_in_original_control_order():
    protocol = make_protocol()
    rtp = udp_packet(b"\x80\x60" + bytes(100))
    stun = udp_packet(b"\x00\x01\x00\x00\x21\x12\xa4\x42" + bytes(12))
    rtcp = udp_packet(b"\x80\xc9" + bytes(20))
    tcp = ipv4_packet(80)
    protocol.send_ip_packet(rtp)
    for control in (stun, rtcp, tcp):
        protocol.send_ip_packet(control)
    assert list(protocol._quic._datagrams_pending) == [b"\0\0" + p for p in (stun, rtcp, tcp, rtp)]


async def test_media_age_and_capacity_never_block_control(monkeypatch):
    protocol = make_protocol()
    monkeypatch.setattr(masque_module, "MAX_PENDING_MEDIA_DATAGRAMS", 2)
    monkeypatch.setattr(masque_module, "MAX_PENDING_IP_DATAGRAMS", 2)
    rtp = udp_packet(b"\x80\x60" + bytes(100))
    for _ in range(3):
        protocol.send_ip_packet(rtp)
    assert len(protocol._quic._datagrams_pending) == 2
    tcp = ipv4_packet(80)
    protocol.send_ip_packet(tcp)
    assert protocol._quic._datagrams_pending[0] == b"\0\0" + tcp
    monkeypatch.setattr(masque_module.time, "monotonic", lambda: float("inf"))
    protocol._prune_media_queue()
    assert list(protocol._quic._datagrams_pending) == [b"\0\0" + tcp]


async def test_transport_delivers_pmtu_feedback_to_tun_callback():
    protocol = make_protocol()
    transport = AioquicConnectIpTransport(server_url="https://127.0.0.1:8444")
    received = []
    async def on_packet(packet):
        received.append(packet)
    transport._protocol = protocol
    transport._connected = True
    transport._on_packet = on_packet
    await transport.send_packet(ipv4_packet(1281))
    assert len(received) == 1
    assert validate_ip_packet(received[0], 1280) == ("100.123.44.87", "10.60.0.3")
    assert not protocol._quic._datagrams_pending


async def test_transport_reconnects_before_next_uplink_packet(monkeypatch):
    transport = AioquicConnectIpTransport(server_url="https://127.0.0.1:8444")
    transport._explicitly_closed = False
    called = []

    async def callback(packet):
        called.append(packet)

    class Protocol:
        def send_ip_packet(self, packet):
            return None

    async def fake_close_connection():
        transport._connected = False

    async def fake_start(on_packet):
        transport._protocol = Protocol()
        transport._connected = True
        transport._on_packet = on_packet

    monkeypatch.setattr(transport, "_close_connection", fake_close_connection)
    monkeypatch.setattr(transport, "start", fake_start)
    transport._on_packet_callback = callback
    packet = ipv4_packet(80)
    await transport.send_packet(packet)
    assert transport.connected
    assert called == []


async def test_receive_loop_reconnects_after_peer_idle_close(monkeypatch):
    transport = AioquicConnectIpTransport(server_url="https://127.0.0.1:8444")
    transport._explicitly_closed = False
    transport._connected = True
    protocol = type("Protocol", (), {"packets": asyncio.Queue()})()
    transport._protocol = protocol
    started = asyncio.Event()

    async def reconnect():
        started.set()

    monkeypatch.setattr(transport, "_reconnect", reconnect)
    await protocol.packets.put(None)
    await transport._receive_loop(lambda packet: asyncio.sleep(0))
    await asyncio.wait_for(started.wait(), 1)
    await transport.close()


async def test_explicit_close_does_not_reconnect(monkeypatch):
    transport = AioquicConnectIpTransport(server_url="https://127.0.0.1:8444")
    transport._protocol = type("Protocol", (), {"packets": asyncio.Queue()})()
    await transport._protocol.packets.put(None)
    await transport._receive_loop(lambda packet: asyncio.sleep(0))
    assert transport._recovery_task is None


async def test_failed_reconnect_attempt_does_not_disable_later_attempts(monkeypatch):
    transport = AioquicConnectIpTransport(server_url="https://127.0.0.1:8444")
    transport._explicitly_closed = False
    transport._on_packet_callback = lambda packet: asyncio.sleep(0)
    attempts = []

    async def start(callback):
        attempts.append(callback)
        if len(attempts) == 1:
            await transport._close_connection()
            raise RuntimeError("temporary failure")
        transport._connected = True

    monkeypatch.setattr(transport, "start", start)
    await transport._reconnect()
    assert transport.connected
    assert len(attempts) == 2
    await transport.close()


def test_ipv4_pmtu_feedback_has_valid_checksums_and_bounded_quote():
    original = ipv4_packet(1500)
    response = packet_too_big_response(original, 1280)
    assert response is not None and len(response) == 576
    assert response[20:22] == b"\x03\x04"
    assert int.from_bytes(response[26:28], "big") == 1280
    assert response[28:] == original[:548]
    assert _internet_checksum(response[:20]) == 0
    assert _internet_checksum(response[20:]) == 0
    assert validate_ip_packet(response, 1280) == ("100.123.44.87", "10.60.0.3")


def test_ipv6_pmtu_feedback_has_valid_pseudoheader_checksum():
    original = ipv6_packet(1500)
    response = packet_too_big_response(original, 1280)
    assert response is not None and len(response) == 1280
    assert response[40:42] == b"\x02\x00"
    assert int.from_bytes(response[44:48], "big") == 1280
    pseudo = response[8:40] + struct.pack("!I3xB", len(response) - 40, 58)
    assert _internet_checksum(pseudo + response[40:]) == 0
    assert response[48:] == original[:1232]


@pytest.mark.parametrize("packet", [b"bad", ipv4_packet(80, 1), ipv6_packet(80, 58)])
def test_invalid_packets_and_icmp_errors_do_not_generate_feedback(packet):
    assert packet_too_big_response(packet, 1280) is None


@pytest.mark.parametrize("mtu", [0, 65535])
def test_impossible_inner_mtu_is_rejected(mtu):
    with pytest.raises(ValueError, match="encapsulated UDP"):
        AioquicConnectIpTransport(server_url="https://127.0.0.1:8444", ip_packet_mtu=mtu)
