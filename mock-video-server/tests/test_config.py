from __future__ import annotations

import os
from unittest import TestCase
from unittest.mock import patch

from services.asr.config import AsrSettings
from services.intent.config import IntentSettings
from services.video.config import VideoSettings


class ServiceSettingsTest(TestCase):
    def test_four_listeners_have_independent_default_ports(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            asr = AsrSettings.from_env()
            intent = IntentSettings.from_env()
            video = VideoSettings.from_env()

        self.assertEqual(9004, asr.port)
        self.assertEqual(8011, intent.port)
        self.assertEqual(28502, video.port)
        self.assertEqual(28501, video.management_port)
        self.assertEqual("0.0.0.0", asr.host)
        self.assertEqual("127.0.0.1", intent.host)
        self.assertEqual("127.0.0.1", video.public_ip)

    def test_mock_defaults_replace_model_backends(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            asr = AsrSettings.from_env()
            intent = IntentSettings.from_env()
            video = VideoSettings.from_env()

        self.assertEqual("mock-fixed-transcription", asr.model)
        self.assertEqual("cpu", asr.device)
        self.assertEqual("rules", intent.backend)
        self.assertEqual("", intent.model)
        self.assertEqual(
            ("patrol", "movement", "find_object", "grab", "other"),
            intent.candidates,
        )
        self.assertEqual("http://127.0.0.1:8011/api/v1/intent", asr.intent_url)
        self.assertTrue(video.yolo_enabled)
        self.assertEqual((640, 480), (video.video_width, video.video_height))
        self.assertEqual(30.0, video.video_fps)
        self.assertEqual(1150, video.h264_rtp_payload_bytes)
        self.assertEqual("", video.management_token)
        self.assertEqual(
            "http://127.0.0.1:8011/api/v1/intent",
            video.intent_url,
        )
        self.assertEqual(
            "http://127.0.0.1:9004/api/v1/transcribe",
            video.asr_url,
        )
        self.assertEqual(50 * 1024 * 1024, video.audio_max_upload_bytes)
        self.assertEqual("", video.producer_control_url)

    def test_pruned_sandbox_environment_names_are_honored(self) -> None:
        with patch.dict(
            os.environ,
            {
                "SANDBOX_MANAGEMENT_PORT": "38501",
                "SANDBOX_USER_PORT": "38502",
                "ASR_PORT": "39004",
                "INTENT_PORT": "38011",
                "VIDEO_PUBLIC_IP": "172.30.0.10",
                "FREE6GC_COMPUTING_SANDBOX_MANAGEMENT_TOKEN": "secret",
                "SANDBOX_PRODUCER_CONTROL_URL": "http://{ue_ipv4_address}:8080/v1/control-actions",
            },
            clear=True,
        ):
            asr = AsrSettings.from_env()
            intent = IntentSettings.from_env()
            video = VideoSettings.from_env()

        self.assertEqual(38501, video.management_port)
        self.assertEqual(38502, video.port)
        self.assertEqual(39004, asr.port)
        self.assertEqual(38011, intent.port)
        self.assertEqual("172.30.0.10", video.public_ip)
        self.assertEqual("secret", video.management_token)
        self.assertEqual(
            "http://{ue_ipv4_address}:8080/v1/control-actions",
            video.producer_control_url,
        )

    def test_legacy_mock_port_environment_still_works(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MOCK_VIDEO_PORT": "28500",
                "MOCK_VIDEO_SERVER_IP": "172.30.0.10",
            },
            clear=True,
        ):
            video = VideoSettings.from_env()

        self.assertEqual(28500, video.port)
        self.assertEqual("172.30.0.10", video.public_ip)

    def test_video_host_only_ice_and_class_filters(self) -> None:
        with patch.dict(
            os.environ,
            {"WEBRTC_ICE_SERVERS": "", "YOLO_CLASSES": "person,dog"},
            clear=True,
        ):
            settings = VideoSettings.from_env()

        self.assertEqual((), settings.rtc_ice_servers)
        self.assertEqual(("person", "dog"), settings.yolo_classes)

    def test_h264_rtp_payload_matches_tun_safe_range(self) -> None:
        with patch.dict(
            os.environ,
            {"VIDEO_H264_RTP_PAYLOAD_BYTES": "1120"},
            clear=True,
        ):
            settings = VideoSettings.from_env()
        self.assertEqual(1120, settings.h264_rtp_payload_bytes)

        for value in ("1099", "1151", "invalid"):
            with self.subTest(value=value), patch.dict(
                os.environ,
                {"VIDEO_H264_RTP_PAYLOAD_BYTES": value},
                clear=True,
            ):
                with self.assertRaises(ValueError):
                    VideoSettings.from_env()
