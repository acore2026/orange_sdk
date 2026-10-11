from __future__ import annotations

import asyncio
import struct
from fractions import Fraction
from types import MethodType

import av
import pytest
from aioice import Candidate, Connection, stun
from aiortc import RTCSessionDescription, VideoStreamTrack
from aiortc.codecs import h264, vpx
from aiortc.rtcrtpparameters import RTCRtpCodecParameters

from agent_sdk.rtc_policy import (
    HighLatencyPeerConnection, _BoundedEncoder, _ConsentTransaction,
    configure_consent, limit_sender_bitrate, query_consent,
    pace_video_transport,
)


async def connect_ice_pair():
    a, b = Connection(ice_controlling=True), Connection(ice_controlling=False)
    await asyncio.gather(a.gather_candidates(), b.gather_candidates())
    for own, peer in ((a, b), (b, a)):
        own.remote_username, own.remote_password = peer.local_username, peer.local_password
        # Host interfaces may have multiple addresses; use the same one on
        # both sides, with no STUN servers or external network dependency.
        await own.add_remote_candidate(peer.local_candidates[0])
        await own.add_remote_candidate(None)
    await asyncio.gather(a.connect(), b.connect())
    for own in (a, b):
        own._query_consent_task.cancel()
        await asyncio.gather(own._query_consent_task, return_exceptions=True)
        own._query_consent_task = None
    return a, b


async def test_real_stun_response_after_650ms_preserves_consent_and_probe_clock():
    a, b = await connect_ice_pair()
    loop = asyncio.get_running_loop()
    probe_times = []
    protocol = next(iter(b._nominated.values())).protocol
    original = protocol.send_stun
    def delayed(message, addr):
        if message.message_class == stun.Class.RESPONSE:
            loop.call_later(0.65, original, message, addr)
        else:
            original(message, addr)
    protocol.send_stun = delayed
    sender = next(iter(a._nominated.values())).protocol
    send = sender.send_stun
    def record(message, addr):
        probe_times.append(loop.time())
        send(message, addr)
    sender.send_stun = record
    try:
        a._query_consent_task = asyncio.create_task(query_consent(a, response_timeout=1, interval=0.15, expiry=1.2))
        await asyncio.sleep(2.5)
        assert not a._closed
        assert len(probe_times) >= 10
        assert max(y - x for x, y in zip(probe_times, probe_times[1:])) < 0.4
        assert stun.RETRY_RTO == 0.5
    finally:
        await asyncio.gather(a.close(), b.close())
    assert not sender.transactions


async def test_no_response_expires_on_elapsed_time_not_failure_count():
    a, b = await connect_ice_pair()
    protocol = next(iter(b._nominated.values())).protocol
    protocol.send_stun = lambda message, addr: None
    start = asyncio.get_running_loop().time()
    try:
        a._query_consent_task = asyncio.create_task(query_consent(a, response_timeout=2, interval=0.1, expiry=0.4))
        await asyncio.wait_for(asyncio.shield(a._query_consent_task), 1)
        assert a._closed
        assert 0.35 <= asyncio.get_running_loop().time() - start < 0.8
        assert not next(iter(b._nominated.values())).protocol.transactions
    finally:
        await asyncio.gather(a.close(), b.close())


async def test_bad_integrity_and_wrong_address_do_not_refresh_consent():
    class Protocol:
        def send_stun(self, message, addr):
            pass
    request = stun.Message(stun.Method.BINDING, stun.Class.REQUEST)
    tx = _ConsentTransaction(request, ("127.0.0.1", 1234), Protocol(), b"secret", 0.1)
    task = asyncio.create_task(tx.run())
    response = stun.Message(stun.Method.BINDING, stun.Class.RESPONSE, transaction_id=request.transaction_id)
    response.add_message_integrity(b"wrong-secret")
    tx.response_received(response, ("127.0.0.1", 1234))
    response.add_message_integrity(b"secret")
    tx.response_received(response, ("127.0.0.2", 1234))
    with pytest.raises(stun.TransactionTimeout):
        await task


async def test_original_wire_authentication_accepts_unknown_stun_attributes():
    class Protocol:
        def send_stun(self, message, addr):
            pass
    request = stun.Message(stun.Method.BINDING, stun.Class.REQUEST)
    tx = _ConsentTransaction(request, ("127.0.0.1", 1234), Protocol(), b"secret", 0.1)
    task = asyncio.create_task(tx.run())
    # This extension is ignored by aioice's parser; re-serializing the parsed
    # message would remove it and incorrectly reject a valid HMAC.
    prefix = bytearray(bytes(stun.Message(stun.Method.BINDING, stun.Class.RESPONSE, transaction_id=request.transaction_id)))
    prefix.extend(struct.pack("!HH4s", 0x802f, 4, b"test"))
    prefix[2:4] = struct.pack("!H", 8)
    signature = stun.message_integrity(bytes(prefix), b"secret")
    prefix.extend(struct.pack("!HH", 8, 20) + signature)
    prefix[2:4] = struct.pack("!H", 32)
    tx.raw_response_received(bytes(prefix), ("127.0.0.1", 1234))
    response, addr = await task
    assert response.message_class == stun.Class.RESPONSE
    assert addr == ("127.0.0.1", 1234)


@pytest.mark.parametrize("encoder", [h264.H264Encoder, vpx.Vp8Encoder])
def test_receiver_feedback_cannot_raise_encoder_above_configured_cap(encoder):
    bounded = _BoundedEncoder(encoder(), 500_000)
    assert bounded.target_bitrate == 500_000
    bounded.target_bitrate = 2_500_000
    assert bounded.target_bitrate == 500_000
    bounded.target_bitrate = 600_000
    assert bounded.target_bitrate == 500_000


def test_vp8_stall_does_not_inflate_frame_budget_or_rewrite_capture_clock():
    from pathlib import Path
    path = Path(__file__).parents[1] / "examples/assets/video-offload-test.mp4"
    normal = _BoundedEncoder(vpx.Vp8Encoder(), 250_000)
    stalled = _BoundedEncoder(vpx.Vp8Encoder(), 250_000)
    # Keep descriptor lengths identical for the comparison.
    normal._encoder.picture_id = stalled._encoder.picture_id = 1234
    with av.open(str(path)) as container:
        for index, frame in enumerate(container.decode(video=0)):
            if index == 12:
                break
            frame = frame.reformat(width=640, height=480)
            frame.time_base = Fraction(1, 90000)
            frame.pts = index * 9000
            expected, _ = normal.encode(frame)
            # Same images, but a 3-second capture gap after each send stall.
            frame.pts = index * 270000
            actual, timestamp = stalled.encode(frame)
            assert timestamp == frame.pts == index * 270000
            assert frame.time_base == Fraction(1, 90000)
            assert actual == expected


async def test_actual_peer_negotiation_installs_policy_on_both_ends_and_encodes():
    from aiortc import RTCConfiguration
    a = HighLatencyPeerConnection(RTCConfiguration(iceServers=[]))
    b = HighLatencyPeerConnection(RTCConfiguration(iceServers=[]))
    sender = a.addTrack(VideoStreamTrack())
    limit_sender_bitrate(sender, 500_000)
    try:
        await a.setLocalDescription(await a.createOffer())
        await b.setRemoteDescription(a.localDescription)
        await b.setLocalDescription(await b.createAnswer())
        await a.setRemoteDescription(b.localDescription)
        for pc in (a, b):
            for transceiver in pc.getTransceivers():
                assert transceiver.sender.transport.transport._connection._high_latency_consent
        encoded = await sender._next_encoded_frame(RTCRtpCodecParameters(mimeType="video/VP8", clockRate=90000, payloadType=97))
        assert encoded.payloads
        assert sender._RTCRtpSender__encoder.target_bitrate == 500_000
    finally:
        await asyncio.gather(a.close(), b.close())


async def test_video_packet_pacer_smooths_bursts_without_delaying_rtcp():
    class Transport:
        async def _send_rtp(self, packet):
            sent.append((packet, asyncio.get_running_loop().time()))
    sent = []
    transport = Transport()
    pace_video_transport(transport, 100_000)
    video = b"\x80\x61" + bytes(998)
    control = b"\x80\xc9" + bytes(30)
    await transport._send_rtp(video)
    second = asyncio.create_task(transport._send_rtp(video))
    await asyncio.sleep(0)
    await transport._send_rtp(control)
    await second
    assert [packet for packet, _ in sent] == [video, control, video]
    assert sent[2][1] - sent[0][1] >= 0.08
    assert sent[1][1] - sent[0][1] < 0.05
