from __future__ import annotations


async def test_offloading_session_and_video_use_media_adapter(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    media = sdk_fixture["media"]

    session = await sdk.create_offloading_session(
        "did:example:agent-a",
        "video_rendering",
        sandbox_id="sandbox-edge-1",
    )
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
