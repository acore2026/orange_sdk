from __future__ import annotations

import logging

import httpx
import pytest

from agent_sdk import AgentSdkError, ErrorCode
from agent_sdk.logging_utils import close_logger, configure_local_logger, log_event
from agent_sdk.rest_server import AiohttpLocalServer
from agent_sdk.runtime import HttpRuntimeTransport


async def test_public_function_entry_exit_error_and_redaction(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    log_path = sdk_fixture["log_path"]

    await sdk.apply_identity(
        "Alice",
        "Agent A",
        description="ordinary-description",
        metadata={
            "region": "CN",
            "os": "Linux",
            "version": "0.12.0",
        },
    )
    try:
        await sdk.send_message(
            "missing-group", "missing-agent",
            {"text": "hello", "access_token": "nested-secret-token"},
            message_type="text", task_id="task-missing",
        )
    except AgentSdkError as exc:
        assert exc.code is ErrorCode.GROUP_NOT_ACTIVE

    text = log_path.read_text(encoding="utf-8")
    assert '"event":"function_enter","function":"apply_identity"' in text
    assert '"event":"function_exit","function":"apply_identity"' in text
    assert '"event":"function_error","function":"send_message"' in text
    assert '"duration_ms":' in text
    # The SDK-generated public key is added below the northbound function
    # boundary and is never accepted from or logged as an application argument.
    assert '"public_key"' not in text
    assert '"access_token":"[REDACTED]"' in text
    assert "nested-secret-token" not in text
    assert "vc-a" not in text


async def test_runtime_http_request_response_are_logged_and_redacted(tmp_path):
    log_path = tmp_path / "runtime.log"
    logger = configure_local_logger(
        name=f"test.runtime.{id(tmp_path)}",
        file_path=str(log_path),
        level="INFO",
        max_bytes=1024 * 1024,
        backup_count=1,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={"agent_id": "did:example:a", "vc0": {"id": "secret-vc"}},
        )

    transport = HttpRuntimeTransport("runtime.example", 443, logger=logger)
    await transport._client.aclose()
    transport._client = httpx.AsyncClient(
        base_url="http://runtime.example:443",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await transport.request(
            "POST",
            "/idm/v1/identity-applications",
            {
                "agent_id": "did:example:a",
                "signature": "secret-signature",
                "proof": {"jws": "secret-jws"},
            },
        )
        assert result["vc0"]["id"] == "secret-vc"
    finally:
        await transport.close()
        close_logger(logger)

    text = log_path.read_text(encoding="utf-8")
    assert '"event":"http_request"' in text
    assert '"event":"http_response"' in text
    assert '"method":"POST"' in text
    assert '"status_code":200' in text
    assert "/idm/v1/identity-applications" in text
    assert text.count("[REDACTED]") >= 3
    assert "secret-signature" not in text
    assert "secret-jws" not in text
    assert "secret-vc" not in text


async def test_local_http_ingress_and_response_are_logged(tmp_path):
    log_path = tmp_path / "ingress.log"
    logger = configure_local_logger(
        name=f"test.ingress.{id(tmp_path)}",
        file_path=str(log_path),
        level="INFO",
        max_bytes=1024 * 1024,
        backup_count=1,
    )
    server = AiohttpLocalServer(logger=logger)

    async def on_a2a(payload):
        assert payload["group_id"] == "g1"

    try:
        await server.start(
            physical_ip="127.0.0.1",
            agent_ip="127.0.0.2",
            tcp_port=0,
            udp_port=28443,
            on_a2a_message=on_a2a,
        )
        socket_address = server._sites[1]._server.sockets[0].getsockname()
        physical_socket_address = server._sites[0]._server.sockets[0].getsockname()
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"http://127.0.0.2:{socket_address[1]}/A2A/message",
                headers={
                    "Authorization": "Bearer inbound-token",
                    "Cookie": "session=inbound-cookie",
                    "X-Api-Key": "inbound-api-key",
                },
                json={
                    "group_id": "g1",
                    "proof": {"jws": "inbound-secret-proof"},
                },
            )
            removed_callback = await client.post(
                f"http://127.0.0.1:{physical_socket_address[1]}/agent/group-moq-info",
                json={"group_id": "g1"},
            )
        assert response.status_code == 200
        assert response.json() == {"status": "OK"}
        assert removed_callback.status_code == 404
    finally:
        await server.close()
        close_logger(logger)

    text = log_path.read_text(encoding="utf-8")
    assert '"event":"http_request"' in text
    assert '"event":"http_request_body"' in text
    assert '"event":"http_response"' in text
    assert '"status_code":200' in text
    assert '"body":{"status":"OK"}' in text
    assert "inbound-secret-proof" not in text
    assert "inbound-token" not in text
    assert "inbound-cookie" not in text
    assert "inbound-api-key" not in text


def test_local_log_rotation(tmp_path):
    log_path = tmp_path / "rotating.log"
    logger = configure_local_logger(
        name=f"test.rotation.{id(tmp_path)}",
        file_path=str(log_path),
        level="INFO",
        max_bytes=300,
        backup_count=2,
    )
    try:
        for index in range(30):
            log_event(
                logger,
                logging.INFO,
                "rotation_test",
                index=index,
                content="x" * 100,
            )
    finally:
        close_logger(logger)

    assert log_path.exists()
    assert (tmp_path / "rotating.log.1").exists()


def test_invalid_log_level_is_rejected(tmp_path):
    with pytest.raises(AgentSdkError) as exc:
        configure_local_logger(
            name=f"test.invalid.{id(tmp_path)}",
            file_path=str(tmp_path / "invalid.log"),
            level="TRACE",
            max_bytes=1024,
            backup_count=1,
        )

    assert exc.value.code is ErrorCode.INVALID_ARGUMENT
    assert exc.value.field == "log_level"
