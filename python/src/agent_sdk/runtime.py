from __future__ import annotations

from typing import Any, Mapping

import httpx

from .errors import AgentSdkError, ErrorCode


class HttpRuntimeTransport:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        verify: bool | str = True,
        endpoint_registration_path: str = "/sdk/v1/endpoints",
        timeout: float = 10.0,
    ) -> None:
        self._base_url = f"https://{host}:{port}"
        self._registration_path = endpoint_registration_path
        self._client = httpx.AsyncClient(
            base_url=self._base_url, verify=verify, timeout=timeout
        )

    async def connect(self) -> None:
        try:
            await self._client.get("/health")
        except httpx.HTTPStatusError:
            return
        except httpx.HTTPError as exc:
            raise AgentSdkError(
                ErrorCode.RUNTIME_UNREACHABLE,
                f"AgentRuntime is unreachable: {exc}",
                retryable=True,
            ) from exc

    async def register_endpoint(
        self, local_ip: str, tcp_port: int, udp_port: int
    ) -> str:
        response = await self.request(
            "POST",
            self._registration_path,
            {
                "local_vlan_ip": local_ip,
                "tcp_port": tcp_port,
                "udp_port": udp_port,
                "callback_paths": [
                    "/agent/group-invitation",
                    "/agent/group-moq-info",
                    "/A2A/message",
                ],
            },
        )
        registration_id = response.get("registration_id")
        if not isinstance(registration_id, str) or not registration_id:
            raise AgentSdkError(
                ErrorCode.RUNTIME_REJECTED,
                "endpoint registration response has no registration_id",
            )
        return registration_id

    async def request(
        self, method: str, path: str, body: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        try:
            response = await self._client.request(method, path, json=dict(body))
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise AgentSdkError(
                ErrorCode.TIMEOUT, f"Runtime request timed out: {path}", retryable=True
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise AgentSdkError(
                ErrorCode.RUNTIME_REJECTED,
                f"Runtime rejected {path}: HTTP {exc.response.status_code}",
                details={"response": exc.response.text[:1024]},
            ) from exc
        except httpx.HTTPError as exc:
            raise AgentSdkError(
                ErrorCode.RUNTIME_UNREACHABLE,
                f"Runtime request failed: {exc}",
                retryable=True,
            ) from exc
        if not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            raise AgentSdkError(
                ErrorCode.RUNTIME_REJECTED,
                "Runtime response must be JSON or an empty success response",
            ) from exc
        if not isinstance(payload, Mapping):
            raise AgentSdkError(
                ErrorCode.RUNTIME_REJECTED, "Runtime response must be a JSON object"
            )
        return payload

    async def close(self) -> None:
        await self._client.aclose()


class HttpPeerMessenger:
    def __init__(self, *, scheme: str = "http", verify: bool | str = True) -> None:
        self._scheme = scheme
        self._verify = verify

    async def send(
        self, ip: str, port: int, body: Mapping[str, Any], timeout: float
    ) -> Mapping[str, Any]:
        host = f"[{ip}]" if ":" in ip else ip
        url = f"{self._scheme}://{host}:{port}/A2A/message"
        try:
            async with httpx.AsyncClient(verify=self._verify, timeout=timeout) as client:
                response = await client.post(url, json=dict(body))
                response.raise_for_status()
                result = response.json()
        except httpx.HTTPError as exc:
            raise AgentSdkError(
                ErrorCode.MESSAGE_DELIVERY_FAILED,
                f"A2A delivery to {ip}:{port} failed: {exc}",
                retryable=True,
            ) from exc
        if not isinstance(result, Mapping):
            raise AgentSdkError(
                ErrorCode.MESSAGE_DELIVERY_FAILED,
                "A2A response must be a JSON object",
            )
        return result
