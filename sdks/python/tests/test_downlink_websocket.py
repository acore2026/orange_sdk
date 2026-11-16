from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import WSMsgType, web

from agent_sdk import AgentSdkError, ErrorCode, NetworkMessageAction
from agent_sdk.models import NetworkMessageType
from agent_sdk.runtime import DOWNLINK_WEBSOCKET_PATH, HttpRuntimeTransport

from conftest import AckNetworkListener


async def _runtime_server():
    connections: asyncio.Queue[web.WebSocketResponse] = asyncio.Queue()
    responses: asyncio.Queue[dict] = asyncio.Queue()
    upgrades: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

    async def downlink(request: web.Request) -> web.WebSocketResponse:
        await upgrades.put(
            (request.headers.get("Connection", ""), request.headers.get("Upgrade", ""))
        )
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        await connections.put(socket)
        async for message in socket:
            if message.type is WSMsgType.TEXT:
                await responses.put(json.loads(message.data))
        return socket

    app = web.Application()
    app.router.add_get(DOWNLINK_WEBSOCKET_PATH, downlink)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, port, connections, responses, upgrades


async def test_runtime_downlink_websocket_supports_concurrent_out_of_order_responses():
    runner, port, connections, responses, upgrades = await _runtime_server()
    release_first = asyncio.Event()
    calls: list[tuple[str, int, dict]] = []

    async def handler(message_type, transaction_id, payload):
        calls.append((message_type, transaction_id, dict(payload)))
        if payload["sequence"] == 1:
            await release_first.wait()
            return {"group_id": "group-invitation", "result": "ACCEPT"}
        return {"group_id": "group-config", "result": "ACK"}

    transport = HttpRuntimeTransport("127.0.0.1", port)
    try:
        await transport.start_downlink(handler)
        socket = await asyncio.wait_for(connections.get(), timeout=1)
        connection, upgrade = await asyncio.wait_for(upgrades.get(), timeout=1)
        assert "upgrade" in connection.lower()
        assert upgrade.lower() == "websocket"

        await socket.send_json(
            {
                "kind": "request",
                "request_id": "delivery-1",
                "message_type": "ACN_AGENT_GROUPING_INVITATION",
                "transaction_id": 49,
                "payload": {
                    "group_info": {
                        "target_agent_id": "agent-b",
                        "group_id": "group-invitation",
                        "group_name": "task-patrol",
                    },
                    "group_administrator": {"agent_id": "agent-a"},
                    "sequence": 1,
                },
            }
        )
        await socket.send_json(
            {
                "kind": "request",
                "request_id": "delivery-2",
                "message_type": "ACN_AGENT_GROUPING_NOTIFICATION",
                "transaction_id": 50,
                "payload": {"group_id": "group-config", "sequence": 2},
            }
        )

        assert await asyncio.wait_for(responses.get(), timeout=1) == {
            "kind": "response",
            "request_id": "delivery-2",
            "payload": {"group_id": "group-config", "result": "ACK"},
        }
        release_first.set()
        assert await asyncio.wait_for(responses.get(), timeout=1) == {
            "kind": "response",
            "request_id": "delivery-1",
            "payload": {"group_id": "group-invitation", "result": "ACCEPT"},
        }
        assert {call[1] for call in calls} == {49, 50}
    finally:
        await transport.close()
        await runner.cleanup()


async def test_runtime_downlink_rejects_invalid_correlated_request():
    runner, port, connections, responses, _ = await _runtime_server()

    async def handler(message_type, transaction_id, payload):
        raise AssertionError("invalid request must not reach handler")

    transport = HttpRuntimeTransport("127.0.0.1", port)
    try:
        await transport.start_downlink(handler)
        socket = await asyncio.wait_for(connections.get(), timeout=1)
        await socket.send_json(
            {
                "kind": "request",
                "request_id": "invalid-1",
                "message_type": "ACN_AGENT_GROUPING_INVITATION",
                "transaction_id": "not-an-integer",
                "payload": {},
            }
        )
        assert await asyncio.wait_for(responses.get(), timeout=1) == {
            "kind": "response",
            "request_id": "invalid-1",
            "payload": {"result": "REJECT"},
        }
        with pytest.raises(AgentSdkError) as error:
            await transport.start_downlink(handler)
        assert error.value.code is ErrorCode.INVALID_ARGUMENT
    finally:
        await transport.close()
        await runner.cleanup()


async def test_sdk_maps_nas_invitation_to_network_listener(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]
    listener = AckNetworkListener(NetworkMessageAction.ACCEPT)
    sdk.register_network_message_listener(listener)
    payload = {
        "group_info": {
            "target_agent_id": "agent-b",
            "group_id": "group-a-b",
            "group_name": "task-patrol",
        },
        "group_administrator": {"agent_id": "a1"},
    }

    result = await runtime.deliver_downlink(
        "ACN_AGENT_GROUPING_INVITATION", payload, transaction_id=49
    )

    assert result == {"group_id": "group-a-b", "result": "ACCEPT"}
    assert listener.messages == [(NetworkMessageType.GROUP_INVITATION, payload)]


async def test_runtime_downlink_accepts_event_without_request_id_or_response():
    runner, port, connections, responses, _ = await _runtime_server()
    delivered = asyncio.Event()

    async def handler(message_type, transaction_id, payload):
        assert message_type == "COMPUTE_SESSION_STATUS"
        assert transaction_id == 34
        delivered.set()
        return None

    transport = HttpRuntimeTransport("127.0.0.1", port)
    try:
        await transport.start_downlink(handler)
        socket = await asyncio.wait_for(connections.get(), timeout=1)
        await socket.send_json({
            "kind": "event",
            "message_type": "COMPUTE_SESSION_STATUS",
            "transaction_id": 34,
            "payload": {"request_id": "create-001", "status": "ACCEPTED", "cause": ""},
        })
        await asyncio.wait_for(delivered.wait(), timeout=1)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(responses.get(), timeout=0.05)
    finally:
        await transport.close()
        await runner.cleanup()


async def test_runtime_request_with_status_preserves_c04_on_http_422():
    async def computing(request: web.Request) -> web.Response:
        body = await request.json()
        return web.json_response(
            {
                "message_type": "COMPUTE_SESSION_STATUS",
                "request_id": body["request_id"],
                "status": "CLARIFICATION_REQUIRED",
                "cause": "missing-capability",
                "missing_fields": ["constraints.capability_id"],
            },
            status=422,
        )

    app = web.Application()
    app.router.add_post("/v1/computing/session-requests", computing)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    transport = HttpRuntimeTransport("127.0.0.1", port)
    try:
        response = await transport.request_with_status(
            "POST",
            "/v1/computing/session-requests",
            {"request_id": "create-001"},
        )
        assert response.status_code == 422
        assert response.body["status"] == "CLARIFICATION_REQUIRED"
        assert response.body["request_id"] == "create-001"
    finally:
        await transport.close()
        await runner.cleanup()
