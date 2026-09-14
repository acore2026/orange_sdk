from __future__ import annotations

import asyncio
import inspect

import pytest

from agent_sdk import (
    AcnContext,
    AgentLifecycleState,
    AgentSdk,
    AgentSdkError,
    ComputeConstraints,
    ComputeInputFormat,
    ComputeRequestType,
    ComputeResources,
    ComputeSessionRequest,
    ComputeSessionStatus,
    ControlAction,
    ControlActionRequest,
    ControlInputType,
    ErrorCode,
    RecognitionTargetStatus,
    RuntimeHttpResponse,
)

from conftest import LOCAL_ID, PEER_ID, group_payload


def create_request(request_id: str = "create-001") -> ComputeSessionRequest:
    return ComputeSessionRequest(
        message_type="COMPUTE_SESSION_REQUEST",
        request_type=ComputeRequestType.CREATE,
        input_format=ComputeInputFormat.STRUCTURED,
        request_id=request_id,
        acn_context=AcnContext(
            group_id="g1",
            requester_agent_id=LOCAL_ID,
            target_agent_id=PEER_ID,
        ),
        constraints=ComputeConstraints(
            capability_id="dog-vision",
            resources=ComputeResources(cpu_millicores=2000, memory_mib=4096),
            allow_base_qos=True,
        ),
        ui_locale="zh-CN",
    )


def connect_config(role: str = "consumer") -> dict:
    return {
        "compute_service_session_id": "css-001",
        "compute_instance_id": "ci-001",
        "binding_ref": "binding-css-001-7f84",
        "role": role,
        "receiver_agent_id": LOCAL_ID,
        "service_endpoint": "http://8.8.8.9:8788/sandbox-base",
        "network_binding": {
            "pdu_session_id": 1,
            "dnn": "internet",
            "snssai": {"sst": 1, "sd": "010203"},
            "ue_ipv4": "8.8.8.7",
            "runtime_data_plane": {
                "access_type": "HTTP3_CONNECT_IP",
                "session_selection": "EXACT_PDU_SESSION_ID",
            },
        },
        "connection_parameters": {
            "media_connections_path": "/v1/media-connections",
            "transport": "WEBRTC",
            "recognition_target_path_template": (
                "/v1/recognition-targets/{compute_service_session_id}"
            ),
        },
    }


def test_computing_api_replaces_legacy_offloading_api() -> None:
    assert not hasattr(AgentSdk, "create_offloading_session")
    assert "request" in inspect.signature(AgentSdk.create_computing_session).parameters
    assert "compute_service_session_id" in inspect.signature(
        AgentSdk.start_video_upload
    ).parameters
    assert "compute_service_session_id" in inspect.signature(
        AgentSdk.get_processed_video_stream
    ).parameters
    assert "compute_service_session_id" in inspect.signature(
        AgentSdk.update_recognition_target
    ).parameters
    assert "compute_service_session_id" in inspect.signature(
        AgentSdk.get_recognition_target
    ).parameters
    assert "compute_service_session_id" in inspect.signature(
        AgentSdk.create_control_action
    ).parameters
    assert "compute_service_session_id" in inspect.signature(
        AgentSdk.get_control_action
    ).parameters


async def test_create_uses_formal_path_and_exact_body(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]
    await runtime.deliver_group_config(group_payload())

    status = await sdk.create_computing_session(create_request())

    assert isinstance(status, ComputeSessionStatus)
    assert status.compute_service_session_id == "css-001"
    _, path, body = runtime.requests[-1]
    assert path == "/v1/computing/session-requests"
    assert body == {
        "message_type": "COMPUTE_SESSION_REQUEST",
        "request_type": "CREATE",
        "input_format": "STRUCTURED",
        "request_id": "create-001",
        "acn_context": {
            "group_id": "g1",
            "requester_agent_id": LOCAL_ID,
            "target_agent_id": PEER_ID,
        },
        "constraints": {
            "capability_id": "dog-vision",
            "resources": {"cpu_millicores": 2000, "memory_mib": 4096},
            "allow_base_qos": True,
        },
        "ui_locale": "zh-CN",
    }
    assert "proof" not in body
    assert "timestamp" not in body


async def test_create_group_response_preserves_earlier_active_config(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]
    await runtime.deliver_group_config(group_payload())

    group = await sdk.create_group(
        LOCAL_ID,
        [PEER_ID],
        "task-patrol",
        "internet",
        max_members=2,
    )
    status = await sdk.create_computing_session(create_request())

    assert group.status == "ACTIVE"
    assert status.compute_service_session_id == "css-001"


async def test_create_requires_a_locally_active_group(sdk_fixture):
    with pytest.raises(AgentSdkError) as error:
        await sdk_fixture["sdk"].create_computing_session(create_request())

    assert error.value.code is ErrorCode.GROUP_NOT_ACTIVE


async def test_create_rejects_mismatched_c04_request_id(sdk_fixture):
    runtime = sdk_fixture["runtime"]
    await runtime.deliver_group_config(group_payload())

    async def response_with_wrong_id(method, path, body):
        del method, path
        return RuntimeHttpResponse(
            202,
            {
                "message_type": "COMPUTE_SESSION_STATUS",
                "request_id": "another-request",
                "compute_service_session_id": "css-001",
                "status_revision": "1",
                "status": "ACCEPTED",
                "cause": "",
            },
        )

    runtime.request_with_status = response_with_wrong_id
    with pytest.raises(AgentSdkError, match="request_id") as error:
        await sdk_fixture["sdk"].create_computing_session(create_request())

    assert error.value.code is ErrorCode.RUNTIME_REJECTED


async def test_older_http_c04_cannot_regress_newer_downlink_c04(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]
    await runtime.deliver_group_config(group_payload())

    async def stale_http_response(method, path, body):
        del method, path
        await runtime.deliver_downlink(
            "COMPUTE_SESSION_STATUS",
            {
                "request_id": body["request_id"],
                "compute_service_session_id": "css-001",
                "status_revision": "7",
                "status": "ACTIVE",
                "cause": "",
            },
            50,
        )
        return RuntimeHttpResponse(
            202,
            {
                "message_type": "COMPUTE_SESSION_STATUS",
                "request_id": body["request_id"],
                "compute_service_session_id": "css-001",
                "status_revision": "2",
                "status": "ACCEPTED",
                "cause": "",
            },
        )

    runtime.request_with_status = stale_http_response

    status = await sdk.create_computing_session(create_request())

    assert status.status == "ACTIVE"
    assert status.status_revision == "7"


@pytest.mark.parametrize(
    "method,request_type,target",
    [
        ("query_computing_session", ComputeRequestType.QUERY, "target_request_id"),
        ("cancel_computing_session", ComputeRequestType.CANCEL, "target_request_id"),
        ("release_computing_session", ComputeRequestType.RELEASE, "compute_service_session_id"),
    ],
)
async def test_lifecycle_requests_share_formal_endpoint(
    sdk_fixture, method, request_type, target
):
    request = ComputeSessionRequest(
        message_type="COMPUTE_SESSION_REQUEST",
        request_type=request_type,
        input_format=ComputeInputFormat.STRUCTURED,
        request_id=f"{request_type.value.lower()}-001",
        target_request_id="create-001" if target == "target_request_id" else None,
        compute_service_session_id="css-001" if target == "compute_service_session_id" else None,
    )

    await getattr(sdk_fixture["sdk"], method)(request)

    _, path, body = sdk_fixture["runtime"].requests[-1]
    assert path == "/v1/computing/session-requests"
    assert body["request_type"] == request_type.value
    assert body[target] in {"create-001", "css-001"}


async def test_c02_is_accepted_internally_and_consumer_media_uses_cached_endpoint(
    sdk_fixture,
):
    runtime = sdk_fixture["runtime"]
    response = await runtime.deliver_downlink(
        "COMPUTE_CONNECT_CONFIG", connect_config(), 49
    )

    assert response == {
        "compute_service_session_id": "css-001",
        "compute_instance_id": "ci-001",
        "binding_ref": "binding-css-001-7f84",
        "role": "consumer",
        "receiver_agent_id": LOCAL_ID,
        "network_binding": connect_config()["network_binding"],
        "accepted": True,
        "cause": "",
    }
    stream = await sdk_fixture["sdk"].get_processed_video_stream("css-001")
    assert sdk_fixture["media"].stream_args == ("css-001",)
    method, url, body, timeout, source_ipv4 = sdk_fixture["sandbox"].requests[-1]
    assert (method, url, timeout, source_ipv4) == (
        "POST",
        "http://8.8.8.9:8788/v1/media-connections",
        120.0,
        "8.8.8.7",
    )
    assert body["computing_context"] == {
        "compute_service_session_id": "css-001",
        "compute_instance_id": "ci-001",
        "binding_ref": "binding-css-001-7f84",
        "role": "consumer",
        "agent_id": LOCAL_ID,
    }
    assert body["offer"]["type"] == "offer"
    assert "a=recvonly" in body["offer"]["sdp"]
    assert "8.8.8.9/32" in sdk_fixture["backend"].routes
    assert "8.8.8.10/32" in sdk_fixture["backend"].routes
    assert await stream.recv() == b"frame"


async def test_producer_media_uses_cached_c02(sdk_fixture):
    await sdk_fixture["runtime"].deliver_downlink(
        "COMPUTE_CONNECT_CONFIG", connect_config("producer"), 50
    )

    upload = await sdk_fixture["sdk"].start_video_upload("css-001", fps=24)

    assert upload.track_id == "camera-track-1"
    assert sdk_fixture["media"].upload_args[0] == "css-001"
    assert sdk_fixture["media"].upload_args[1]["fps"] == 24
    method, url, body, _, _ = sdk_fixture["sandbox"].requests[-1]
    assert (method, url) == ("POST", "http://8.8.8.9:8788/v1/media-connections")
    assert body["computing_context"]["role"] == "producer"
    assert "a=sendonly" in body["offer"]["sdp"]


async def test_consumer_close_deletes_only_media_and_restores_service_route(sdk_fixture):
    await sdk_fixture["runtime"].deliver_downlink(
        "COMPUTE_CONNECT_CONFIG", connect_config("consumer"), 50
    )
    stream = await sdk_fixture["sdk"].get_processed_video_stream("css-001")

    await stream.close()

    assert sdk_fixture["sandbox"].requests[-1][0:2] == (
        "DELETE",
        "http://8.8.8.9:8788/v1/media-connections/media-consumer-001",
    )
    assert "8.8.8.9/32" in sdk_fixture["backend"].routes
    assert "8.8.8.10/32" not in sdk_fixture["backend"].routes
    assert "css-001" in sdk_fixture["sdk"]._computing_sessions


async def test_media_retry_reuses_request_id_and_offer(sdk_fixture):
    await sdk_fixture["runtime"].deliver_downlink(
        "COMPUTE_CONNECT_CONFIG", connect_config("producer"), 50
    )
    sandbox = sdk_fixture["sandbox"]
    actual_request = sandbox.request_with_status
    calls = []

    async def fail_once(method, url, body, timeout_seconds, source_ipv4):
        calls.append((method, url, body, timeout_seconds, source_ipv4))
        if len(calls) == 1:
            raise AgentSdkError(
                ErrorCode.MEDIA_NEGOTIATION_FAILED,
                "connection reset after POST",
                retryable=True,
            )
        return await actual_request(method, url, body, timeout_seconds, source_ipv4)

    sandbox.request_with_status = fail_once
    await sdk_fixture["sdk"].start_video_upload("css-001")

    assert len(calls) == 2
    assert calls[0][2] == calls[1][2]
    assert calls[0][2]["request_id"].startswith("media-producer-")


async def test_media_retry_after_public_timeout_keeps_pending_offer(sdk_fixture):
    await sdk_fixture["runtime"].deliver_downlink(
        "COMPUTE_CONNECT_CONFIG", connect_config("producer"), 50
    )
    sandbox = sdk_fixture["sandbox"]
    actual_request = sandbox.request_with_status
    calls = []

    async def fail_first_api_call(method, url, body, timeout_seconds, source_ipv4):
        calls.append((method, url, body, timeout_seconds, source_ipv4))
        if len(calls) <= 2:
            raise AgentSdkError(ErrorCode.TIMEOUT, "unknown POST result", retryable=True)
        return await actual_request(method, url, body, timeout_seconds, source_ipv4)

    sandbox.request_with_status = fail_first_api_call
    with pytest.raises(AgentSdkError) as error:
        await sdk_fixture["sdk"].start_video_upload("css-001")
    assert error.value.retryable is True

    upload = await sdk_fixture["sdk"].start_video_upload("css-001")

    assert upload.track_id == "camera-track-1"
    assert len(calls) == 3
    assert calls[0][2] == calls[1][2] == calls[2][2]


async def test_media_rejects_untrusted_response_echo(sdk_fixture):
    await sdk_fixture["runtime"].deliver_downlink(
        "COMPUTE_CONNECT_CONFIG", connect_config("consumer"), 50
    )
    sandbox = sdk_fixture["sandbox"]

    async def wrong_echo(method, url, body, timeout_seconds, source_ipv4):
        del method, url, timeout_seconds, source_ipv4
        return RuntimeHttpResponse(201, {
            "request_id": body["request_id"],
            "computing_context": {**body["computing_context"], "binding_ref": "wrong"},
            "media_connection_id": "untrusted-media",
            "answer": {"type": "answer", "sdp": "v=0\r\n"},
        })

    sandbox.request_with_status = wrong_echo
    with pytest.raises(AgentSdkError) as error:
        await sdk_fixture["sdk"].get_processed_video_stream("css-001")

    assert error.value.code is ErrorCode.MEDIA_NEGOTIATION_FAILED
    assert sdk_fixture["media"].prepared.aborted is True


async def test_media_rejects_host_candidate_outside_c02_user_plane(sdk_fixture):
    await sdk_fixture["runtime"].deliver_downlink(
        "COMPUTE_CONNECT_CONFIG", connect_config("producer"), 50
    )
    media = sdk_fixture["media"]
    original_prepare = media.prepare_video_upload

    async def prepare_with_wifi_candidate(session, **kwargs):
        prepared = await original_prepare(session, **kwargs)
        prepared.offer_sdp += "a=candidate:9 1 UDP 1 192.168.1.9 50001 typ host\r\n"
        return prepared

    media.prepare_video_upload = prepare_with_wifi_candidate
    with pytest.raises(AgentSdkError, match="outside the C-02"):
        await sdk_fixture["sdk"].start_video_upload("css-001")

    assert media.prepared.aborted is True
    assert sdk_fixture["sandbox"].requests == []


async def test_answer_application_failure_deletes_remote_and_restores_route(sdk_fixture):
    await sdk_fixture["runtime"].deliver_downlink(
        "COMPUTE_CONNECT_CONFIG", connect_config("producer"), 50
    )
    media = sdk_fixture["media"]
    original_prepare = media.prepare_video_upload

    async def failing_prepare(session, **kwargs):
        prepared = await original_prepare(session, **kwargs)

        async def fail(answer_sdp, timeout_seconds):
            del answer_sdp, timeout_seconds
            raise AgentSdkError(
                ErrorCode.MEDIA_NEGOTIATION_FAILED, "setRemoteDescription failed"
            )

        prepared.apply_answer = fail
        return prepared

    media.prepare_video_upload = failing_prepare
    with pytest.raises(AgentSdkError, match="setRemoteDescription"):
        await sdk_fixture["sdk"].start_video_upload("css-001")

    assert sdk_fixture["sandbox"].requests[-1][0] == "DELETE"
    assert "8.8.8.9/32" in sdk_fixture["backend"].routes
    assert "8.8.8.10/32" not in sdk_fixture["backend"].routes


async def test_c05_wins_a_media_create_race_and_deletes_late_connection(sdk_fixture):
    runtime = sdk_fixture["runtime"]
    await runtime.deliver_downlink(
        "COMPUTE_CONNECT_CONFIG", connect_config("producer"), 50
    )
    sandbox = sdk_fixture["sandbox"]
    actual_request = sandbox.request_with_status
    post_started = asyncio.Event()
    release_post = asyncio.Event()

    async def delayed_post(method, url, body, timeout_seconds, source_ipv4):
        if method == "POST":
            post_started.set()
            await release_post.wait()
        return await actual_request(method, url, body, timeout_seconds, source_ipv4)

    sandbox.request_with_status = delayed_post
    upload_task = asyncio.create_task(
        sdk_fixture["sdk"].start_video_upload("css-001")
    )
    await post_started.wait()
    close = {
        "compute_service_session_id": "css-001",
        "compute_instance_id": "ci-001",
        "binding_ref": "binding-css-001-7f84",
        "role": "producer",
        "receiver_agent_id": LOCAL_ID,
        "cause": "released",
    }

    ack = await runtime.deliver_downlink("COMPUTE_SESSION_CLOSE", close, 51)
    release_post.set()
    with pytest.raises(AgentSdkError) as error:
        await upload_task

    assert ack["closed"] is True
    assert error.value.code is ErrorCode.COMPUTING_SESSION_INVALID
    assert any(request[0] == "DELETE" for request in sandbox.requests)
    assert "css-001" not in sdk_fixture["sdk"]._computing_media


async def test_c04_deduplicates_by_status_revision(sdk_fixture):
    runtime = sdk_fixture["runtime"]
    await runtime.deliver_downlink(
        "COMPUTE_SESSION_STATUS",
        {
            "request_id": "create-001",
            "compute_service_session_id": "css-001",
            "status_revision": "3",
            "status": "MEDIA_CONNECTING",
            "cause": "",
        },
        34,
    )
    await runtime.deliver_downlink(
        "COMPUTE_SESSION_STATUS",
        {
            "request_id": "create-001",
            "compute_service_session_id": "css-001",
            "status_revision": "2",
            "status": "ACTIVATING",
            "cause": "",
        },
        35,
    )

    assert sdk_fixture["sdk"]._computing_statuses["css-001"].status == "MEDIA_CONNECTING"
    assert (
        sdk_fixture["sdk"]._computing_statuses_by_request["create-001"].status
        == "MEDIA_CONNECTING"
    )


async def test_terminal_c04_takes_precedence_over_cached_c02(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]
    await runtime.deliver_downlink(
        "COMPUTE_CONNECT_CONFIG", connect_config("consumer"), 49
    )
    await runtime.deliver_downlink(
        "COMPUTE_SESSION_STATUS",
        {
            "request_id": "create-001",
            "compute_service_session_id": "css-001",
            "status_revision": "7",
            "status": "FAILED",
            "cause": "resource-activation-failed",
        },
        50,
    )

    with pytest.raises(AgentSdkError) as error:
        await sdk.get_processed_video_stream("css-001")

    assert error.value.code is ErrorCode.COMPUTING_SESSION_INVALID
    assert sdk_fixture["media"].stream_args is None


async def test_c02_after_terminal_c04_is_rejected_without_route(sdk_fixture):
    runtime = sdk_fixture["runtime"]
    await runtime.deliver_downlink(
        "COMPUTE_SESSION_STATUS",
        {
            "request_id": "create-001",
            "compute_service_session_id": "css-001",
            "status_revision": "7",
            "status": "FAILED",
            "cause": "resource-activation-failed",
        },
        49,
    )

    ack = await runtime.deliver_downlink(
        "COMPUTE_CONNECT_CONFIG", connect_config("consumer"), 50
    )

    assert ack["accepted"] is False
    assert ack["cause"] == "session-closed"
    assert "8.8.8.9/32" not in sdk_fixture["backend"].routes


async def test_c05_waits_for_earlier_c02_installation(sdk_fixture):
    runtime = sdk_fixture["runtime"]
    backend = sdk_fixture["backend"]
    route_install_started = asyncio.Event()
    release_route_install = asyncio.Event()
    original_add = backend.add

    async def blocking_add(cidr):
        if cidr == "8.8.8.9/32":
            route_install_started.set()
            await release_route_install.wait()
        await original_add(cidr)

    backend.add = blocking_add
    c02 = asyncio.create_task(
        runtime.deliver_downlink(
            "COMPUTE_CONNECT_CONFIG", connect_config("consumer"), 49
        )
    )
    await route_install_started.wait()
    close = {
        "compute_service_session_id": "css-001",
        "compute_instance_id": "ci-001",
        "binding_ref": "binding-css-001-7f84",
        "role": "consumer",
        "receiver_agent_id": LOCAL_ID,
        "cause": "released",
    }
    c05 = asyncio.create_task(
        runtime.deliver_downlink("COMPUTE_SESSION_CLOSE", close, 50)
    )
    await asyncio.sleep(0)

    assert not c05.done()
    release_route_install.set()

    assert (await c02)["accepted"] is True
    assert (await c05)["closed"] is True
    assert "8.8.8.9/32" not in backend.routes


async def test_c05_closes_sdk_media_and_replays_same_c06(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]
    await runtime.deliver_downlink(
        "COMPUTE_CONNECT_CONFIG", connect_config("producer"), 49
    )
    upload = await sdk.start_video_upload("css-001")
    close = {
        "compute_service_session_id": "css-001",
        "compute_instance_id": "ci-001",
        "binding_ref": "binding-css-001-7f84",
        "role": "producer",
        "receiver_agent_id": LOCAL_ID,
        "cause": "released",
    }

    first = await runtime.deliver_downlink("COMPUTE_SESSION_CLOSE", close, 50)
    second = await runtime.deliver_downlink("COMPUTE_SESSION_CLOSE", close, 50)

    assert first == second
    assert first["closed"] is True
    assert first["cause"] == ""
    assert upload.state == "STOPPED"
    assert sdk_fixture["sandbox"].requests[-1][0:2] == (
        "DELETE",
        "http://8.8.8.9:8788/v1/media-connections/media-producer-001",
    )
    assert "8.8.8.9/32" not in sdk_fixture["backend"].routes


async def test_identity_removal_preserves_profile_until_c05_closes_config(
    sdk_fixture,
):
    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]
    await runtime.deliver_downlink(
        "COMPUTE_CONNECT_CONFIG", connect_config("consumer"), 49
    )
    profile = sdk.local_profile
    assert profile is not None
    runtime.requests.clear()

    with pytest.raises(AgentSdkError) as deregister_error:
        await sdk.deregister_identity(profile.agent_id, reason="normal")
    with pytest.raises(AgentSdkError) as reset_error:
        await sdk.reset_agent()

    assert deregister_error.value.code is ErrorCode.AGENT_STATE_INVALID
    assert reset_error.value.code is ErrorCode.AGENT_STATE_INVALID
    assert sdk.local_profile == profile
    assert runtime.requests == []

    close_waiter = asyncio.create_task(sdk.await_computing_session_closed("css-001"))
    await asyncio.sleep(0)
    close = {
        "compute_service_session_id": "css-001",
        "compute_instance_id": "ci-001",
        "binding_ref": "binding-css-001-7f84",
        "role": "consumer",
        "receiver_agent_id": LOCAL_ID,
        "cause": "released",
    }
    close_ack = await runtime.deliver_downlink("COMPUTE_SESSION_CLOSE", close, 50)
    assert close_ack is not None
    assert close_ack["closed"] is True
    await close_waiter

    result = await sdk.deregister_identity(profile.agent_id, reason="normal")

    assert result.success is True
    assert sdk.agent_lifecycle_state is AgentLifecycleState.NO_IDENTITY
    assert sdk.local_profile is None
    assert [request[1] for request in runtime.requests] == [
        "/acn-agent/v1/agent-deletions"
    ]


async def test_non_terminal_c04_blocks_reset_before_c02_arrives(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]
    profile = sdk.local_profile
    assert profile is not None
    await runtime.deliver_group_config(group_payload())
    await sdk.create_computing_session(create_request())

    with pytest.raises(AgentSdkError) as reset_error:
        await sdk.reset_agent()

    assert reset_error.value.code is ErrorCode.AGENT_STATE_INVALID
    assert sdk.local_profile == profile

    await runtime.deliver_downlink(
        "COMPUTE_SESSION_STATUS",
        {
            "request_id": "cancel-001",
            "compute_service_session_id": "css-001",
            "status_revision": "3",
            "status": "REQUEST_CANCELLED",
            "cause": "cancelled",
        },
        50,
    )
    result = await sdk.reset_agent()

    assert result.success is True
    assert sdk.agent_lifecycle_state is AgentLifecycleState.NO_IDENTITY


async def test_in_flight_computing_request_blocks_reset_atomically(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]
    profile = sdk.local_profile
    assert profile is not None
    await runtime.deliver_group_config(group_payload())
    request_started = asyncio.Event()
    release_response = asyncio.Event()

    async def delayed_terminal_response(method, path, body):
        del method, path
        request_started.set()
        await release_response.wait()
        return RuntimeHttpResponse(
            202,
            {
                "message_type": "COMPUTE_SESSION_STATUS",
                "request_id": body["request_id"],
                "compute_service_session_id": "css-001",
                "status_revision": "3",
                "status": "REQUEST_CANCELLED",
                "cause": "cancelled",
            },
        )

    runtime.request_with_status = delayed_terminal_response
    create = asyncio.create_task(sdk.create_computing_session(create_request()))
    await request_started.wait()

    with pytest.raises(AgentSdkError) as reset_error:
        await sdk.reset_agent()

    assert reset_error.value.code is ErrorCode.AGENT_STATE_INVALID
    assert sdk.local_profile == profile

    release_response.set()
    assert (await create).status == "REQUEST_CANCELLED"
    assert (await sdk.reset_agent()).success is True


async def test_recognition_target_uses_c02_path_and_context(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]
    sandbox = sdk_fixture["sandbox"]
    await runtime.deliver_downlink("COMPUTE_CONNECT_CONFIG", connect_config(), 49)

    updated = await sdk.update_recognition_target(
        "css-001",
        request_id="recognition-001",
        text="寻找红色玩偶",
        language="zh",
    )
    fetched = await sdk.get_recognition_target("css-001")

    assert isinstance(updated, RecognitionTargetStatus)
    assert updated == fetched
    assert updated.status == "APPLIED"
    assert updated.target_revision == "1"
    assert updated.target.label == "红色玩偶"
    method, url, body, _, source_ip = sandbox.requests[-2]
    assert method == "PUT"
    assert url == "http://8.8.8.9:8788/v1/recognition-targets/css-001"
    assert source_ip == "8.8.8.7"
    assert body == {
        "request_id": "recognition-001",
        "computing_context": {
            "compute_service_session_id": "css-001",
            "compute_instance_id": "ci-001",
            "binding_ref": "binding-css-001-7f84",
            "role": "consumer",
            "agent_id": LOCAL_ID,
        },
        "input": {"type": "TEXT", "text": "寻找红色玩偶", "language": "zh"},
    }
    assert sandbox.requests[-1][0:3] == (
        "GET",
        "http://8.8.8.9:8788/v1/recognition-targets/css-001",
        None,
    )


async def test_c02_rejects_invalid_recognition_target_template(sdk_fixture):
    payload = connect_config()
    payload["connection_parameters"]["recognition_target_path_template"] = (
        "/v1/recognition-targets/current"
    )

    response = await sdk_fixture["runtime"].deliver_downlink(
        "COMPUTE_CONNECT_CONFIG", payload, 49
    )

    assert response["accepted"] is False
    assert response["cause"] == "invalid-request"


async def test_text_control_action_uses_formal_sandbox_resource(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]
    sandbox = sdk_fixture["sandbox"]
    await runtime.deliver_downlink("COMPUTE_CONNECT_CONFIG", connect_config(), 49)

    created = await sdk.create_control_action(
        "css-001",
        ControlActionRequest(
            request_id="control-search-001",
            input_type=ControlInputType.TEXT,
            text="寻找杯子",
            language="zh",
        ),
    )
    fetched = await sdk.get_control_action("css-001", created.action_id)

    assert created.action_id == "action-001"
    assert created.status == "RUNNING"
    assert created.normalized_action is ControlAction.SEARCH_OBJECT
    assert created.normalized_parameters == {"query": "cup"}
    assert fetched.status == "COMPLETED"
    assert fetched.computing_context is None
    method, url, body, _, source_ip = sandbox.requests[-2]
    assert method == "POST"
    assert url == "http://8.8.8.9:8788/v1/control-actions"
    assert source_ip == "8.8.8.7"
    assert body == {
        "request_id": "control-search-001",
        "computing_context": {
            "compute_service_session_id": "css-001",
            "compute_instance_id": "ci-001",
            "binding_ref": "binding-css-001-7f84",
            "role": "consumer",
            "agent_id": LOCAL_ID,
        },
        "input": {"type": "TEXT", "text": "寻找杯子", "language": "zh"},
    }
    assert sandbox.requests[-1][0:3] == (
        "GET",
        "http://8.8.8.9:8788/v1/control-actions/action-001",
        None,
    )


async def test_structured_control_action_requires_parameters(sdk_fixture):
    with pytest.raises(AgentSdkError) as error:
        await sdk_fixture["sdk"].create_control_action(
            "css-001",
            ControlActionRequest(
                request_id="control-movement-001",
                input_type=ControlInputType.STRUCTURED,
                action=ControlAction.MOVEMENT,
            ),
        )

    assert error.value.code is ErrorCode.INVALID_ARGUMENT
    assert error.value.field == "parameters"


async def test_c02_rejects_network_binding_mismatch(sdk_fixture):
    payload = connect_config()
    payload["network_binding"]["ue_ipv4"] = "8.8.8.6"

    response = await sdk_fixture["runtime"].deliver_downlink(
        "COMPUTE_CONNECT_CONFIG", payload, 49
    )

    assert response["accepted"] is False
    assert response["cause"] == "network-binding-mismatch"
