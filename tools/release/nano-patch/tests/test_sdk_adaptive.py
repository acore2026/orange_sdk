"""Run against the installed SDK wheel using its existing Python environment."""
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from agent_sdk.rtc_policy import limit_sender_bitrate


def sender():
    return SimpleNamespace(_RTCRtpSender__encoder=None,
                           _next_encoded_frame=AsyncMock(),
                           transport=SimpleNamespace(_send_rtp=AsyncMock()))


class AdaptiveVideoTests(unittest.TestCase):
    def test_default_keeps_native_encoder_and_transport(self):
        with patch.dict(os.environ, {}, clear=True):
            s=sender();original=s._next_encoded_frame;send=s.transport._send_rtp
            limit_sender_bitrate(s)
            self.assertIs(s._next_encoded_frame,original)
            self.assertIs(s.transport._send_rtp,send)
            self.assertFalse(hasattr(s,'_video_bitrate_limit'))

    def test_explicit_cap_and_wire_pacing_remain_supported(self):
        s=sender()
        limit_sender_bitrate(s,500000,wire_bitrate=2000000)
        self.assertEqual(s._video_bitrate_limit,500000)
        self.assertEqual(s.transport._video_packet_pacer._rate,2000000)

    def test_invalid_pacing_rejected_before_sender_mutation(self):
        for value in ('-1','invalid'):
            with patch.dict(os.environ,{'AGENT_VIDEO_WIRE_BITRATE_BPS':value}):
                s=sender();original=s._next_encoded_frame
                with self.assertRaises(ValueError):limit_sender_bitrate(s)
                self.assertIs(s._next_encoded_frame,original)


if __name__=='__main__':unittest.main()
