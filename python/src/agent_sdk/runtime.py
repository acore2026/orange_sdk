from __future__ import annotations

import logging
import uuid
from typing import Any, Mapping

import httpx

from .errors import AgentSdkError, ErrorCode
from .logging_utils import log_event


class HttpRuntimeTransport:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        verify: bool | str = True,
        endpoint_registration_path: str = "/sdk/v1/endpoints",
        timeout: float = 10.0,
        logger: logging.Logger | None = None,
    ) -> None:
        self._base_url = f"https://{host}:{port}"
        self._registration_path = endpoint_registration_path
        self._client = httpx.AsyncClient(
            base_url=self._base_url, verify=verify, timeout=timeout
        )
        self._logger = logger or logging.getLogger(__name__)

    @staticmethod
    def _response_body(response: httpx.Response) -> Any:
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return f"<non-json-response:{len(response.content)} bytes>"

    async def connect(self) -> None:
        request_id = uuid.uuid4().hex
        log_event(
            self._logger,
            logging.INFO,
            "http_request",
            request_id=request_id,
            direction="outbound",
            peer="AgentRuntime",
            method="GET",
            url=f"{self._base_url}/health",
            body=None,
        )
        try:
            response = await self._client.get("/health")
            log_event(
                self._logger,
                logging.INFO,
                "http_response",
                request_id=request_id,
                direction="inbound",
                peer="AgentRuntime",
                method="GET",
                url=f"{self._base_url}/health",
                status_code=response.status_code,
                body=self._response_body(response),
            )
        except httpx.HTTPStatusError:
            return
        except httpx.HTTPError as exc:
            log_event(
                self._logger,
                logging.ERROR,
                "http_error",
                exc_info=True,
                request_id=request_id,
                direction="outbound",
                peer="AgentRuntime",
                method="GET",
                url=f"{self._base_url}/health",
                error_type=type(exc).__name__,
                error=str(exc),
            )
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
        request_id = uuid.uuid4().hex
        url = f"{self._base_url}{path}"
        log_event(
            self._logger,
            logging.INFO,
            "http_request",
            request_id=request_id,
            direction="outbound",
            peer="AgentRuntime",
            method=method,
            url=url,
            body=body,
        )
        try:
            response = await self._client.request(method, path, json=dict(body))
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            log_event(
                self._logger,
                logging.ERROR,
                "http_error",
                exc_info=True,
                request_id=request_id,
                direction="outbound",
                peer="AgentRuntime",
                method=method,
                url=url,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise AgentSdkError(
                ErrorCode.TIMEOUT, f"Runtime request timed out: {path}", retryable=True
            ) from exc
        except httpx.HTTPStatusError as exc:
            log_event(
                self._logger,
                logging.ERROR,
                "http_response",
                request_id=request_id,
                direction="inbound",
                peer="AgentRuntime",
                method=method,
                url=url,
                status_code=exc.response.status_code,
                body=self._response_body(exc.response),
            )
            raise AgentSdkError(
                ErrorCode.RUNTIME_REJECTED,
                f"Runtime rejected {path}: HTTP {exc.response.status_code}",
                details={"response": exc.response.text[:1024]},
            ) from exc
        except httpx.HTTPError as exc:
            log_event(
                self._logger,
                logging.ERROR,
                "http_error",
                exc_info=True,
                request_id=request_id,
                direction="outbound",
                peer="AgentRuntime",
                method=method,
                url=url,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise AgentSdkError(
                ErrorCode.RUNTIME_UNREACHABLE,
                f"Runtime request failed: {exc}",
                retryable=True,
            ) from exc
        response_body = self._response_body(response)
        log_event(
            self._logger,
            logging.INFO,
            "http_response",
            request_id=request_id,
            direction="inbound",
            peer="AgentRuntime",
            method=method,
            url=url,
            status_code=response.status_code,
            body=response_body,
        )
        if response_body is None:
            return {}
        if isinstance(response_body, str):
            raise AgentSdkError(
                ErrorCode.RUNTIME_REJECTED,
                "Runtime response must be JSON or an empty success response",
            )
        payload = response_body
        if not isinstance(payload, Mapping):
            raise AgentSdkError(
                ErrorCode.RUNTIME_REJECTED, "Runtime response must be a JSON object"
            )
        return payload

    async def close(self) -> None:
        await self._client.aclose()


class HttpPeerMessenger:
    def __init__(
        self,
        *,
        scheme: str = "http",
        verify: bool | str = True,
        logger: logging.Logger | None = None,
    ) -> None:
        self._scheme = scheme
        self._verify = verify
        self._logger = logger or logging.getLogger(__name__)

    async def send(
        self, ip: str, port: int, body: Mapping[str, Any], timeout: float
    ) -> Mapping[str, Any]:
        host = f"[{ip}]" if ":" in ip else ip
        url = f"{self._scheme}://{host}:{port}/A2A/message"
        request_id = uuid.uuid4().hex
        log_event(
            self._logger,
            logging.INFO,
            "http_request",
            request_id=request_id,
            direction="outbound",
            peer="Agent",
            method="POST",
            url=url,
            body=body,
        )
        try:
            async with httpx.AsyncClient(verify=self._verify, timeout=timeout) as client:
                response = await client.post(url, json=dict(body))
                response.raise_for_status()
                try:
                    result = response.json()
                except ValueError as exc:
                    log_event(
                        self._logger,
                        logging.ERROR,
                        "http_response",
                        request_id=request_id,
                        direction="inbound",
                        peer="Agent",
                        method="POST",
                        url=url,
                        status_code=response.status_code,
                        body=HttpRuntimeTransport._response_body(response),
                        error_type=type(exc).__name__,
                        error="A2A response is not JSON",
                    )
                    raise AgentSdkError(
                        ErrorCode.MESSAGE_DELIVERY_FAILED,
                        "A2A response must be JSON",
                    ) from exc
        except AgentSdkError:
            raise
        except httpx.HTTPError as exc:
            response = getattr(exc, "response", None)
            log_event(
                self._logger,
                logging.ERROR,
                "http_response" if response is not None else "http_error",
                exc_info=response is None,
                request_id=request_id,
                direction="inbound" if response is not None else "outbound",
                peer="Agent",
                method="POST",
                url=url,
                status_code=getattr(response, "status_code", None),
                body=(
                    HttpRuntimeTransport._response_body(response)
                    if response is not None
                    else None
                ),
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise AgentSdkError(
                ErrorCode.MESSAGE_DELIVERY_FAILED,
                f"A2A delivery to {ip}:{port} failed: {exc}",
                retryable=True,
            ) from exc
        log_event(
            self._logger,
            logging.INFO,
            "http_response",
            request_id=request_id,
            direction="inbound",
            peer="Agent",
            method="POST",
            url=url,
            status_code=response.status_code,
            body=result,
        )
        if not isinstance(result, Mapping):
            raise AgentSdkError(
                ErrorCode.MESSAGE_DELIVERY_FAILED,
                "A2A response must be a JSON object",
            )
        return result
