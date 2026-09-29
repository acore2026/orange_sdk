from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "examples" / "dog_patrol_agent.py"
SPEC = importlib.util.spec_from_file_location("dog_patrol_agent", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
dog = importlib.util.module_from_spec(SPEC)
sys.modules["dog_patrol_agent"] = dog
SPEC.loader.exec_module(dog)


class FakeSdk:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_message(self, group_id, target_agent_id, payload, **kwargs):
        self.sent.append(
            {
                "group_id": group_id,
                "target_agent_id": target_agent_id,
                "payload": dict(payload),
                "kwargs": kwargs,
            }
        )

    async def close(self):
        return None


def make_agent():
    actions = dog.DryRunDogActions()
    sdk = FakeSdk()
    config = dog.DogPatrolConfig(
        runtime_ip="127.0.0.1",
        runtime_port=8080,
        local_tcp_port=4002,
        local_udp_port=28444,
        masque_url="https://127.0.0.1:4433",
        masque_authorization=None,
        owner="test",
        agent_name="D-1",
    )
    agent = dog.DogPatrolAgent(sdk, actions, config)
    agent.profile = type("Profile", (), {"agent_id": "dog-d1", "agent_name": "D-1"})()
    return agent, actions, sdk


def test_unconfirmed_expulsion_is_rejected_without_motion() -> None:
    async def scenario() -> None:
        agent, actions, sdk = make_agent()
        await agent.on_group_message(
            "group-1",
            "glasses-1",
            {"type": "dog_command", "command": "expel", "command_id": "danger-1"},
        )
        assert actions.calls == []
        assert sdk.sent[-1]["payload"]["type"] == "command_confirmation_required"
        await agent.on_group_message(
            "group-1",
            "glasses-1",
            {
                "type": "dog_command",
                "command": "expel",
                "command_id": "danger-1",
                "confirmed": True,
            },
        )
        assert actions.calls == ["StopMove", "FrontPounce"]
        await agent.close()

    asyncio.run(scenario())


def test_confirmed_story_runs_patrol_alert_and_unitree_mappings() -> None:
    async def scenario() -> None:
        agent, actions, sdk = make_agent()
        await agent.on_group_message(
            "group-1",
            "glasses-1",
            {
                "type": "patrol_request",
                "task_id": "task-a",
                "zone": "A",
                "confirmed": True,
                "route": ["forward", "left"],
            },
        )
        await agent._patrol_task
        await agent.on_group_message(
            "group-1",
            "glasses-1",
            {"type": "danger_detected", "object": "knife-wielding-person", "confidence": 0.95},
        )
        await agent.on_group_message(
            "group-1",
            "glasses-1",
            {"type": "dog_command", "command": "威吓歹徒", "confirmed": True, "command_id": "hello-1"},
        )
        await agent.on_group_message(
            "group-1",
            "glasses-1",
            {"type": "dog_command", "command": "驱逐歹徒", "confirmed": True, "command_id": "pounce-1"},
        )
        assert actions.calls == ["StandUp", "forward", "left", "StopMove", "Hello", "StopMove", "FrontPounce"]
        payload_types = [item["payload"]["type"] for item in sdk.sent]
        assert "danger_alert" in payload_types
        assert "dog_action_completed" in payload_types
        await agent.close()

    asyncio.run(scenario())
