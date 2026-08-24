from __future__ import annotations

import uuid


async def test_offloading_session_and_video_use_media_adapter(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    media = sdk_fixture["media"]

    session = await sdk.create_offloading_session(
        "did:example:agent-a",
        workload_type="video_rendering",
        sandbox_id="sandbox-edge-1",
    )

    _, path, body = sdk_fixture["runtime"].requests[-1]
    assert path == "/compute/v1/offloading-sessions"
    assert set(body) == {
        "request_id",
        "agent_id",
        "workload_type",
        "preferred_sandbox_id",
        "timestamp",
        "proof",
    }
    assert body["agent_id"] == "did:example:agent-a"
    assert body["workload_type"] == "video_rendering"
    assert body["preferred_sandbox_id"] == "sandbox-edge-1"
    uuid.UUID(body["request_id"])
    assert "task_type" not in body
    assert body["timestamp"] == "2026-08-19T00:00:00Z"
    assert body["proof"] == {"jws": "test-proof"}
    upload = await sdk.start_video_upload(
        session.session_id,
        camera_id=2,
        width=1280,
        height=720,
        fps=30,
        bitrate_kbps=2500,
    )
    stream = await sdk.get_processed_video_stream(session.session_id)

    assert session.state == "CONNECTED"
    assert session.expires_at is not None
    assert media.connected_session == "session-1"
    assert media.upload_args == (
        "session-1",
        {
            "camera_id": 2,
            "width": 1280,
            "height": 720,
            "fps": 30,
            "bitrate_kbps": 2500,
        },
    )
    assert upload.track_id == "camera-track-1"
    assert await stream.recv() == b"frame"
