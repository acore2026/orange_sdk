from __future__ import annotations

import uuid

import pytest

from agent_sdk import AgentSdkError, ErrorCode, OffloadingSessionRole

from conftest import LOCAL_ID, PEER_ID, _create_sdk_fixture, group_payload


async def test_compute_control_override_installs_route_and_isolates_requests(tmp_path):
    fixture = await _create_sdk_fixture(
        tmp_path,
        restore_profile=True,
        compute_override=True,
    )
    try:
        sdk = fixture["sdk"]
        runtime = fixture["runtime"]
        compute_runtime = fixture["compute_runtime"]
        backend = fixture["backend"]
        await runtime.deliver_group_config(group_payload())

        await sdk.create_offloading_session(
            LOCAL_ID,
            workload_type="video_rendering",
            group_id="g1",
        )

        assert "172.30.0.10/32" in backend.routes
        assert compute_runtime.requests[-1][1] == "/compute/v1/offloading-sessions"
        assert not any(path.startswith("/compute/") for _, path, _ in runtime.requests)
    finally:
        await fixture["sdk"].close()


async def test_producer_starts_pull_then_notifies_every_target(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]
    media = sdk_fixture["media"]
    messenger = sdk_fixture["messenger"]
    second_peer = "did:example:agent-c"
    config = group_payload()
    config["members"]["agent-c"] = {
        "agent_id": second_peer,
        "agent_name": "Agent C",
        "skills": ["video"],
        "agent_ip": "8.8.8.10",
        "service_endpoints": "http://agent-c.example:4001/A2A/message",
    }
    await runtime.deliver_group_config(config)

    session = await sdk.create_offloading_session(
        LOCAL_ID,
        workload_type="video_rendering",
        group_id="g1",
        sandbox_id="sandbox-edge-1",
    )

    _, path, body = runtime.requests[-1]
    assert path == "/compute/v1/offloading-sessions"
    assert set(body) == {
        "request_id",
        "agent_id",
        "workload_type",
        "group_id",
        "preferred_sandbox_id",
        "timestamp",
        "proof",
    }
    assert body["agent_id"] == LOCAL_ID
    assert body["group_id"] == "g1"
    assert body["workload_type"] == "video_rendering"
    assert body["preferred_sandbox_id"] == "sandbox-edge-1"
    uuid.UUID(body["request_id"])
    assert session.role is OffloadingSessionRole.PRODUCER
    assert session.state == "ALLOCATED"
    assert session.producer is not None
    assert "producer-token" not in repr(session)

    upload = await sdk.start_video_upload(
        session.session_id,
        target_agent_ids=[PEER_ID, second_peer],
        camera_id=2,
        width=1280,
        height=720,
        fps=30,
        bitrate_kbps=2500,
    )

    assert session.state == "SOURCE_CONNECTED"
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
    _, consumer_path, consumer_body = runtime.requests[-1]
    assert consumer_path == "/compute/v1/offloading-sessions/session-1/consumers"
    assert consumer_body["target_agent_ids"] == [PEER_ID, second_peer]
    assert consumer_body["group_id"] == "g1"
    assert len(messenger.calls) == 2
    assert [call[1]["dst_agent_id"] for call in messenger.calls] == [
        PEER_ID,
        second_peer,
    ]
    for index, (_, wire, _) in enumerate(messenger.calls, 1):
        assert wire["type"] == "processed_video_invitation"
        invitation = wire["payload"]
        assert invitation["consumer_agent_id"] == wire["dst_agent_id"]
        assert invitation["source_agent_id"] == LOCAL_ID
        assert "producer-token" not in str(invitation)
        assert invitation["processed_stream"]["access_ticket"] == (
            f"consumer-ticket-{index}"
        )

    with pytest.raises(AgentSdkError) as error:
        await sdk.get_processed_video_stream(session.session_id)
    assert error.value.code is ErrorCode.OFFLOADING_ROLE_INVALID


async def test_consumer_imports_p2p_invitation_and_gets_processed_stream(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]
    backend = sdk_fixture["backend"]
    await runtime.deliver_group_config(group_payload())
    invitation = {
        "type": "processed_video_invitation",
        "version": "1.0",
        "session_id": "session-from-b",
        "group_id": "g1",
        "source_agent_id": PEER_ID,
        "consumer_agent_id": LOCAL_ID,
        "sandbox_id": "video-server-1",
        "state": "SOURCE_CONNECTED",
        "expires_at": "2027-09-01T00:00:00Z",
        "processed_stream": {
            "video_server_ip": "8.8.8.9",
            "offer_url": "https://8.8.8.9:28500/v1/processed/offer",
            "access_ticket": "consumer-ticket-a",
            "protocol": "webrtc",
            "signaling": "non-trickle",
        },
    }

    session = await sdk.accept_offloading_session(PEER_ID, "g1", invitation)
    stream = await sdk.get_processed_video_stream(session.session_id)

    assert session.role is OffloadingSessionRole.CONSUMER
    assert session.processed_stream is not None
    assert "consumer-ticket-a" not in repr(session)
    assert session.processed_stream.offer_url.endswith("/v1/processed/offer")
    assert "8.8.8.9/32" in backend.routes
    assert await stream.recv() == b"frame"


async def test_upload_rejects_target_outside_group_before_camera_starts(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]
    media = sdk_fixture["media"]
    await runtime.deliver_group_config(group_payload())
    session = await sdk.create_offloading_session(
        LOCAL_ID, "video_rendering", "g1"
    )

    with pytest.raises(AgentSdkError) as error:
        await sdk.start_video_upload(
            session.session_id,
            target_agent_ids=["did:example:not-in-group"],
        )

    assert error.value.code is ErrorCode.TARGET_NOT_IN_GROUP
    assert media.upload_args is None


async def test_upload_stops_when_target_notification_fails(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]
    media = sdk_fixture["media"]
    messenger = sdk_fixture["messenger"]
    await runtime.deliver_group_config(group_payload())
    session = await sdk.create_offloading_session(
        LOCAL_ID, "video_rendering", "g1"
    )

    async def reject_message(endpoint, body, timeout):
        messenger.calls.append((endpoint, body, timeout))
        return {"status": "REJECTED"}

    messenger.send = reject_message
    with pytest.raises(AgentSdkError) as error:
        await sdk.start_video_upload(
            session.session_id,
            target_agent_ids=[PEER_ID],
        )

    assert error.value.code is ErrorCode.MESSAGE_DELIVERY_FAILED
    assert media.upload.state == "STOPPED"
    assert session.state == "ALLOCATED"
