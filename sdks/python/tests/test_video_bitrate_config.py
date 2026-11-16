from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent_sdk.rtc_policy import limit_sender_bitrate


def sender():
    return SimpleNamespace(_RTCRtpSender__encoder=None, _next_encoded_frame=AsyncMock(),
                           transport=SimpleNamespace(_send_rtp=AsyncMock()))


@pytest.mark.parametrize('rate', [1000000, 2000000, 3000000])
def test_env_controls_pacing_separately_from_encoder(monkeypatch, rate):
    monkeypatch.setenv('AGENT_VIDEO_WIRE_BITRATE_BPS', str(rate))
    s = sender()
    limit_sender_bitrate(s, 500000)
    assert s._video_bitrate_limit == 500000
    assert s.transport._video_packet_pacer._rate == rate


def test_explicit_pacing_overrides_environment(monkeypatch):
    monkeypatch.setenv('AGENT_VIDEO_WIRE_BITRATE_BPS', '3000000')
    s = sender()
    limit_sender_bitrate(s, 250000, wire_bitrate=2000000)
    assert s.transport._video_packet_pacer._rate == 2000000


@pytest.mark.parametrize('value', ['-1', 'invalid'])
def test_invalid_pacing_rejected_before_mutating_sender(monkeypatch, value):
    monkeypatch.setenv('AGENT_VIDEO_WIRE_BITRATE_BPS', value)
    s = sender()
    with pytest.raises(ValueError):
        limit_sender_bitrate(s)
    assert not hasattr(s, '_video_bitrate_limit')


def test_unlimited_does_not_wrap_encoder_or_transport(monkeypatch):
    monkeypatch.delenv('AGENT_VIDEO_WIRE_BITRATE_BPS', raising=False)
    s = sender()
    original = s._next_encoded_frame
    limit_sender_bitrate(s)
    assert s._next_encoded_frame is original
    assert not hasattr(s, '_video_bitrate_limit')
    assert not hasattr(s.transport, '_video_packet_pacer')
