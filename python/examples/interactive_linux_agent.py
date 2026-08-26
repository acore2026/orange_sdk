"""Run the real Linux Agent SDK flow one interface at a time.

Pressing Enter authorizes exactly the next displayed SDK call.  Waiting for
input happens in a worker thread so QUIC keep-alive, WebSocket downlink and the
SDK HTTP server continue running on the asyncio event loop.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable

import linux_agent
from agent_sdk import AgentSdk


class InteractiveDemoAborted(RuntimeError):
    pass


class EnterStepGate:
    def __init__(self, reader: Callable[[str], str] | None = None) -> None:
        self._reader = reader or input
        self.step = 0

    async def __call__(self, interface_name: str, description: str) -> None:
        self.step += 1
        print("\n" + "=" * 72)
        print(f"[步骤 {self.step}] {interface_name}")
        print(description)
        try:
            answer = await asyncio.to_thread(
                self._reader,
                "按回车调用该接口；输入 q 后回车退出：",
            )
        except EOFError as exc:
            raise InteractiveDemoAborted("标准输入已关闭，交互流程终止") from exc
        if answer.strip().lower() in {"q", "quit", "exit"}:
            raise InteractiveDemoAborted(
                f"用户在调用 {interface_name} 前终止流程"
            )
        print(f"[调用中] {interface_name}")


async def run_interactive(args: argparse.Namespace) -> None:
    print("Agent SDK 真实接口交互式测试")
    print("每次按回车只执行屏幕上显示的下一个接口。")
    print("等待输入期间，MASQUE 保活和下行 WebSocket 仍会正常运行。")

    gate = EnterStepGate()
    sdk = AgentSdk(
        media_offload_adapter=linux_agent.ExampleMediaOffloadAdapter(),
    )
    unregister_network = lambda: None
    unregister_group = lambda: None
    flow_completed = False
    try:
        await gate(
            "sdk.register_network_message_listener",
            "注册核心网邀请和群组配置通知监听器；该步骤不发送 HTTP。",
        )
        unregister_network = sdk.register_network_message_listener(
            linux_agent.NetworkListener()
        )
        print("[返回] 网络消息监听器注册成功")

        await gate(
            "sdk.register_group_message_listener",
            "注册群组内 A2A 消息监听器；该步骤不发送 HTTP。",
        )
        unregister_group = sdk.register_group_message_listener(
            linux_agent.GroupListener()
        )
        print("[返回] 群组消息监听器注册成功")

        await linux_agent.run_full_flow(sdk, args, before_step=gate)
        flow_completed = True
    finally:
        unregister_group()
        unregister_network()
        try:
            if flow_completed:
                await gate(
                    "sdk.close",
                    "关闭 SDK 并释放 WebSocket、MASQUE、TUN、HTTP 监听和路由资源。",
                )
        finally:
            await sdk.close()
            print("[返回] SDK已关闭")


def parser() -> argparse.ArgumentParser:
    value = linux_agent.parser()
    value.description = (
        "Run the real Agent SDK flow interactively; each Enter invokes one interface."
    )
    return value


if __name__ == "__main__":
    try:
        asyncio.run(run_interactive(parser().parse_args()))
    except InteractiveDemoAborted as exc:
        print(f"[已终止] {exc}")
