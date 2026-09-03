from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from ipaddress import ip_address
from typing import Any, Mapping

import httpx
from aiohttp import ClientSession, ClientTimeout, ClientWebSocketResponse, WSMsgType

from .errors import AgentSdkError, ErrorCode
from .logging_utils import log_event
from .models import NetworkMessageAction


DOWNLINK_WEBSOCKET_PATH = "/v1/acn/downlink-websocket"
UE_INFO_PATH = "/v1/ue/info"


class HttpRuntimeTransport:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        verify: bool | str = True,
        timeout: float = 10.0,
        logger: logging.Logger | None = None,
    ) -> None:
        self._base_url = f"http://{host}:{port}"
        self._timeout = timeout
        self._client = httpx.AsyncClient(
            base_url=self._base_url, verify=verify, timeout=timeout
        )
        self._logger = logger or logging.getLogger(__name__)
        self._downlink_session: ClientSession | None = None
        self._downlink_socket: ClientWebSocketResponse | None = None
        self._downlink_task: asyncio.Task[None] | None = None
        self._downlink_requests: set[asyncio.Task[None]] = set()
        self._downlink_send_lock = asyncio.Lock()
        self._downlink_closing = False

    @staticmethod
    def _response_body(response: httpx.Response) -> Any:
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return f"<non-json-response:{len(response.content)} bytes>"

    async def start_downlink(
        self,
        handler: Callable[
            [str, int, Mapping[str, Any]], Awaitable[NetworkMessageAction]
        ],
    ) -> None:
        if self._downlink_task is not None:
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT,
                "Runtime downlink WebSocket is already started",
            )
        self._downlink_closing = False
        self._downlink_session = ClientSession(
            timeout=ClientTimeout(total=None, sock_connect=self._timeout)
        )
        try:
            socket = await self._open_downlink_socket()
        except Exception:
            await self._downlink_session.close()
            self._downlink_session = None
            raise
        self._downlink_socket = socket
        self._downlink_task = asyncio.create_task(
            self._run_downlink(socket, handler),
            name="agent-runtime-downlink-websocket",
        )

    async def _open_downlink_socket(self) -> ClientWebSocketResponse:
        assert self._downlink_session is not None
        url = f"{self._base_url}{DOWNLINK_WEBSOCKET_PATH}"
        log_event(
            self._logger,
            logging.INFO,
            "websocket_connect",
            direction="outbound",
            peer="AgentRuntime",
            method="GET",
            url=url,
        )
        try:
            socket = await self._downlink_session.ws_connect(url, heartbeat=30.0)
        except Exception as exc:
            log_event(
                self._logger,
                logging.ERROR,
                "websocket_error",
                exc_info=True,
                direction="outbound",
                peer="AgentRuntime",
                method="GET",
                url=url,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise AgentSdkError(
                ErrorCode.RUNTIME_UNREACHABLE,
                f"AgentRuntime downlink WebSocket is unreachable: {exc}",
                retryable=True,
            ) from exc
        log_event(
            self._logger,
            logging.INFO,
            "websocket_connected",
            direction="outbound",
            peer="AgentRuntime",
            method="GET",
            url=url,
        )
        return socket

    async def _run_downlink(
        self,
        initial_socket: ClientWebSocketResponse,
        handler: Callable[
            [str, int, Mapping[str, Any]], Awaitable[NetworkMessageAction]
        ],
    ) -> None:
        socket = initial_socket
        retry_delay = 0.5
        while not self._downlink_closing:
            try:
                async for message in socket:
                    if message.type is WSMsgType.TEXT:
                        task = asyncio.create_task(
                            self._process_downlink_frame(socket, message.data, handler),
                            name="agent-runtime-downlink-request",
                        )
                        self._downlink_requests.add(task)
                        task.add_done_callback(self._downlink_requests.discard)
                    elif message.type is WSMsgType.ERROR:
                        raise socket.exception() or RuntimeError(
                            "Runtime downlink WebSocket failed"
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "websocket_disconnected",
                    peer="AgentRuntime",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
            finally:
                await socket.close()
            if self._downlink_closing:
                break
            while not self._downlink_closing:
                await asyncio.sleep(retry_delay)
                try:
                    socket = await self._open_downlink_socket()
                    self._downlink_socket = socket
                    retry_delay = 0.5
                    break
                except AgentSdkError:
                    retry_delay = min(retry_delay * 2, 10.0)

    async def _process_downlink_frame(
        self,
        socket: ClientWebSocketResponse,
        raw_message: str,
        handler: Callable[
            [str, int, Mapping[str, Any]], Awaitable[NetworkMessageAction]
        ],
    ) -> None:
        request_id: str | None = None
        group_id: str | None = None
        try:
            message = json.loads(raw_message)
            if not isinstance(message, Mapping):
                raise ValueError("WebSocket message must be a JSON object")
            raw_request_id = message.get("request_id")
            if not isinstance(raw_request_id, str) or not raw_request_id:
                raise ValueError("request_id must be a non-empty string")
            request_id = raw_request_id
            if message.get("kind") != "request":
                raise ValueError("kind must be request")
            message_type = message.get("message_type")
            if not isinstance(message_type, str) or not message_type:
                raise ValueError("message_type must be a non-empty string")
            transaction_id = message.get("transaction_id")
            if (
                isinstance(transaction_id, bool)
                or not isinstance(transaction_id, int)
                or transaction_id < 0
            ):
                raise ValueError("transaction_id must be a non-negative integer")
            payload = message.get("payload")
            if not isinstance(payload, Mapping):
                raise ValueError("payload must be a JSON object")
            if message_type == "ACN_AGENT_GROUPING_INVITATION":
                group_info = payload.get("group_info")
                raw_group_id = (
                    group_info.get("group_id")
                    if isinstance(group_info, Mapping)
                    else None
                )
            elif message_type == "ACN_AGENT_GROUPING_NOTIFICATION":
                raw_group_id = payload.get("group_id")
            else:
                raw_group_id = None
            if isinstance(raw_group_id, str) and raw_group_id:
                group_id = raw_group_id
            log_event(
                self._logger,
                logging.INFO,
                "websocket_request",
                direction="inbound",
                peer="AgentRuntime",
                request_id=request_id,
                message_type=message_type,
                transaction_id=transaction_id,
                body=payload,
            )
            action = await handler(message_type, transaction_id, payload)
            if not isinstance(action, NetworkMessageAction):
                raise TypeError("downlink handler must return NetworkMessageAction")
        except Exception as exc:
            log_event(
                self._logger,
                logging.ERROR,
                "websocket_request_error",
                exc_info=True,
                direction="inbound",
                peer="AgentRuntime",
                request_id=request_id,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            action = NetworkMessageAction.REJECT
        if request_id is None:
            return
        response_payload = {"result": action.value}
        if group_id is not None:
            response_payload = {"group_id": group_id, **response_payload}
        response = {
            "kind": "response",
            "request_id": request_id,
            "payload": response_payload,
        }
        try:
            async with self._downlink_send_lock:
                await socket.send_json(response)
            log_event(
                self._logger,
                logging.INFO,
                "websocket_response",
                direction="outbound",
                peer="AgentRuntime",
                request_id=request_id,
                body=response,
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "websocket_response_error",
                direction="outbound",
                peer="AgentRuntime",
                request_id=request_id,
                error_type=type(exc).__name__,
                error=str(exc),
            )

    async def _request_json(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
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
            request_arguments: dict[str, Any] = {
                "headers": {"Content-Type": "application/json"}
            }
            if body is not None:
                request_arguments["json"] = dict(body)
            response = await self._client.request(method, path, **request_arguments)
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

    async def get_ue_agent_ip(self) -> str:
        payload = await self._request_json("GET", UE_INFO_PATH, None)
        nas = payload.get("nas")
        if not isinstance(nas, Mapping):
            raise AgentSdkError(
                ErrorCode.RUNTIME_REJECTED,
                "GET /v1/ue/info response has no valid nas object",
                field="nas",
            )
        if nas.get("registered") is not True:
            raise AgentSdkError(
                ErrorCode.RUNTIME_REJECTED,
                "UERANSIM UE is not registered",
                field="nas.registered",
                retryable=True,
            )
        if nas.get("state") != "session_ready":
            raise AgentSdkError(
                ErrorCode.RUNTIME_REJECTED,
                "UERANSIM NAS state is not session_ready",
                field="nas.state",
                retryable=True,
            )
        if nas.get("security_context") is not True:
            raise AgentSdkError(
                ErrorCode.RUNTIME_REJECTED,
                "UERANSIM NAS security context is not ready",
                field="nas.security_context",
                retryable=True,
            )

        sessions = payload.get("pdu_sessions")
        if not isinstance(sessions, list):
            raise AgentSdkError(
                ErrorCode.RUNTIME_REJECTED,
                "GET /v1/ue/info response has no valid pdu_sessions array",
                field="pdu_sessions",
            )
        active_ipv4: list[tuple[str, bool]] = []
        for session in sessions:
            if not isinstance(session, Mapping):
                continue
            if session.get("state") != "active" or session.get("type") != "IPv4":
                continue
            raw_ipv4 = session.get("ipv4")
            if not isinstance(raw_ipv4, str):
                raise AgentSdkError(
                    ErrorCode.RUNTIME_REJECTED,
                    "active IPv4 PDU Session has no ipv4 address",
                    field="pdu_sessions.ipv4",
                )
            try:
                parsed_ipv4 = ip_address(raw_ipv4)
            except ValueError as exc:
                raise AgentSdkError(
                    ErrorCode.RUNTIME_REJECTED,
                    "PDU Session ipv4 must be an IPv4 literal",
                    field="pdu_sessions.ipv4",
                ) from exc
            if parsed_ipv4.version != 4:
                raise AgentSdkError(
                    ErrorCode.RUNTIME_REJECTED,
                    "PDU Session ipv4 must be an IPv4 literal",
                    field="pdu_sessions.ipv4",
                )
            active_ipv4.append((str(parsed_ipv4), session.get("default_route") is True))

        defaults = [address for address, is_default in active_ipv4 if is_default]
        if len(defaults) != 1:
            raise AgentSdkError(
                ErrorCode.RUNTIME_REJECTED,
                "exactly one active default IPv4 PDU Session is required",
                field="pdu_sessions",
                retryable=True,
            )
        return defaults[0]

    async def request(
        self, method: str, path: str, body: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return await self._request_json(method, path, body)

    async def close(self) -> None:
        self._downlink_closing = True
        if self._downlink_task is not None:
            self._downlink_task.cancel()
            await asyncio.gather(self._downlink_task, return_exceptions=True)
            self._downlink_task = None
        for task in tuple(self._downlink_requests):
            task.cancel()
        if self._downlink_requests:
            await asyncio.gather(*self._downlink_requests, return_exceptions=True)
        self._downlink_requests.clear()
        if self._downlink_socket is not None:
            await self._downlink_socket.close()
            self._downlink_socket = None
        if self._downlink_session is not None:
            await self._downlink_session.close()
            self._downlink_session = None
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
        self, endpoint: str, body: Mapping[str, Any], timeout: float
    ) -> Mapping[str, Any]:
        url = endpoint
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
                f"A2A delivery to {url} failed: {exc}",
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
