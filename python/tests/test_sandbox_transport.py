from __future__ import annotations

from aiohttp import web

from agent_sdk.runtime import HttpSandboxTransport


async def test_sandbox_transport_supports_sandbox_resource_methods():
    received = []

    async def create(request: web.Request) -> web.Response:
        received.append((request.method, request.path, await request.json()))
        return web.json_response({"media_connection_id": "media-001"}, status=201)

    async def delete(request: web.Request) -> web.Response:
        received.append((request.method, request.path, await request.read()))
        return web.Response(status=204)

    async def recognition(request: web.Request) -> web.Response:
        body = await request.json() if request.can_read_body else None
        received.append((request.method, request.path, body))
        return web.json_response({"status": "APPLIED"})

    app = web.Application()
    app.router.add_post("/v1/media-connections", create)
    app.router.add_delete("/v1/media-connections/{connection_id}", delete)
    app.router.add_put("/v1/recognition-targets/{session_id}", recognition)
    app.router.add_get("/v1/recognition-targets/{session_id}", recognition)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    transport = HttpSandboxTransport()
    try:
        created = await transport.request_with_status(
            "POST",
            f"http://127.0.0.1:{port}/v1/media-connections",
            {"request_id": "media-request-001"},
            2.0,
            "127.0.0.1",
        )
        deleted = await transport.request_with_status(
            "DELETE",
            f"http://127.0.0.1:{port}/v1/media-connections/media-001",
            None,
            2.0,
            "127.0.0.1",
        )
        updated = await transport.request_with_status(
            "PUT",
            f"http://127.0.0.1:{port}/v1/recognition-targets/css-001",
            {"request_id": "recognition-001"},
            2.0,
            "127.0.0.1",
        )
        fetched = await transport.request_with_status(
            "GET",
            f"http://127.0.0.1:{port}/v1/recognition-targets/css-001",
            None,
            2.0,
            "127.0.0.1",
        )
    finally:
        await transport.close()
        await runner.cleanup()

    assert created.status_code == 201
    assert created.body == {"media_connection_id": "media-001"}
    assert deleted.status_code == 204
    assert deleted.body == {}
    assert updated.status_code == 200
    assert fetched.status_code == 200
    assert received == [
        ("POST", "/v1/media-connections", {"request_id": "media-request-001"}),
        ("DELETE", "/v1/media-connections/media-001", b""),
        ("PUT", "/v1/recognition-targets/css-001", {"request_id": "recognition-001"}),
        ("GET", "/v1/recognition-targets/css-001", None),
    ]
