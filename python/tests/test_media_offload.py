from __future__ import annotations

import inspect
import uuid

import pytest

from agent_sdk import (
    AgentSdk,
    AgentSdkError,
    ErrorCode,
    OffloadingSession,
    ProcessedVideoEndpoint,
    SandboxSpec,
    VideoUploadEndpoint,
)

from conftest import LOCAL_ID, _create_sdk_fixture, group_payload


def test_media_api_surface_has_no_accept_or_target_distribution() -> None:
    assert not hasattr(AgentSdk, "accept_offloading_session")
    assert "target_agent_ids" not in inspect.signature(
        AgentSdk.start_video_upload
    ).parameters
    assert "session" in inspect.signature(AgentSdk.start_video_upload).parameters
    assert "session" in inspect.signature(
        AgentSdk.get_processed_video_stream
    ).parameters
    create_parameters = inspect.signature(AgentSdk.create_offloading_session).parameters
    assert "sandbox_spec" in create_parameters
    assert "agent_id" not in create_parameters
    assert "group_id" not in create_parameters
    assert "sandbox_id" not in create_parameters


@pytest.mark.parametrize(
    "sandbox_spec,field",
    [
        (SandboxSpec(vcpus=0, memory_mb=4096), "sandbox_spec.vcpus"),
        (SandboxSpec(vcpus=2, memory_mb=0), "sandbox_spec.memory_mb"),
    ],
)
async def test_create_rejects_invalid_sandbox_spec(sdk_fixture, sandbox_spec, field):
    with pytest.raises(AgentSdkError) as error:
        await sdk_fixture["sdk"].create_offloading_session(
            workload_type="video_rendering",
            sandbox_spec=sandbox_spec,
        )

    assert error.value.code is ErrorCode.INVALID_ARGUMENT
    assert error.value.field == field


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
            workload_type="video_rendering",
            sandbox_spec=SandboxSpec(vcpus=2, memory_mb=4096),
        )

        assert "172.30.0.10/32" in backend.routes
        assert compute_runtime.requests[-1][1] == "/compute/v1/offloading-sessions"
        assert not any(path.startswith("/compute/") for _, path, _ in runtime.requests)
    finally:
        await fixture["sdk"].close()


async def test_create_upload_and_self_receive_use_only_declared_media_apis(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]
    media = sdk_fixture["media"]
    messenger = sdk_fixture["messenger"]
    backend = sdk_fixture["backend"]
    await runtime.deliver_group_config(group_payload())

    session = await sdk.create_offloading_session(
        workload_type="video_rendering",
        sandbox_spec=SandboxSpec(vcpus=2, memory_mb=4096),
    )

    _, path, body = runtime.requests[-1]
    assert path == "/compute/v1/offloading-sessions"
    assert set(body) == {
        "request_id",
        "workload_type",
        "sandbox_spec",
        "timestamp",
        "proof",
    }
    assert body["workload_type"] == "video_rendering"
    assert body["sandbox_spec"] == {"vcpus": 2, "memory_mb": 4096}
    uuid.UUID(body["request_id"])
    assert session.state == "ALLOCATED"
    assert session.producer is not None
    assert session.processed_stream is not None
    assert "access_token" not in repr(session)
    assert "access_ticket" not in repr(session)

    upload = await sdk.start_video_upload(
        session,
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
    assert not any(path.endswith("/consumers") for _, path, _ in runtime.requests)
    assert messenger.calls == []

    stream = await sdk.get_processed_video_stream(session)
    assert "8.8.8.9/32" in backend.routes
    assert await stream.recv() == b"frame"


async def test_application_supplied_remote_session_gets_processed_stream(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]
    backend = sdk_fixture["backend"]
    await runtime.deliver_group_config(group_payload())
    session = OffloadingSession(
        session_id="session-from-b",
        state="SOURCE_CONNECTED",
        processed_stream=ProcessedVideoEndpoint(
            video_server_ip="8.8.8.9",
            offer_url="https://8.8.8.9:28500/v1/processed/offer",
            protocol="webrtc",
            signaling="non-trickle",
        ),
    )

    stream = await sdk.get_processed_video_stream(session)

    assert session.processed_stream.offer_url.endswith("/v1/processed/offer")
    assert "8.8.8.9/32" in backend.routes
    assert await stream.recv() == b"frame"


async def test_application_supplied_session_drives_upload_endpoint(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    media = sdk_fixture["media"]
    backend = sdk_fixture["backend"]
    session = OffloadingSession(
        session_id="session-created-elsewhere",
        state="ALLOCATED",
        producer=VideoUploadEndpoint(
            video_server_ip="9.9.9.9",
            source_start_url="https://9.9.9.9:29500/source",
            source_stop_url="https://9.9.9.9:29500/source/stop",
        ),
        processed_stream=ProcessedVideoEndpoint(
            video_server_ip="9.9.9.10",
            offer_url="https://9.9.9.10:29501/processed",
        ),
    )

    upload = await sdk.start_video_upload(session, fps=24)

    assert upload.track_id == "camera-track-1"
    assert media.upload_args[0] == "session-created-elsewhere"
    assert {"9.9.9.9/32", "9.9.9.10/32"} <= backend.routes


async def test_processed_stream_rejects_session_without_endpoint(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    session = OffloadingSession(
        session_id="session-without-endpoint",
        state="SOURCE_CONNECTED",
    )

    with pytest.raises(AgentSdkError) as error:
        await sdk.get_processed_video_stream(session)

    assert error.value.code is ErrorCode.OFFLOADING_SESSION_INVALID
