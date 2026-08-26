from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


EXAMPLE_DIR = Path(__file__).parents[1] / "examples"
EXAMPLE_PATH = EXAMPLE_DIR / "interactive_linux_agent.py"


def _load_example():
    sys.path.insert(0, str(EXAMPLE_DIR))
    try:
        spec = importlib.util.spec_from_file_location(
            "interactive_linux_agent", EXAMPLE_PATH
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


async def test_enter_gate_runs_one_displayed_step_per_enter(capsys):
    module = _load_example()
    prompts = []
    gate = module.EnterStepGate(lambda prompt: prompts.append(prompt) or "")

    await gate("sdk.init", "initialize SDK")
    await gate("sdk.apply_identity", "apply identity")

    assert gate.step == 2
    assert len(prompts) == 2
    output = capsys.readouterr().out
    assert "[步骤 1] sdk.init" in output
    assert "[步骤 2] sdk.apply_identity" in output
    assert output.count("[调用中]") == 2


async def test_enter_gate_supports_explicit_quit():
    module = _load_example()
    gate = module.EnterStepGate(lambda prompt: "q")

    with pytest.raises(module.InteractiveDemoAborted, match="用户在调用 sdk.init 前"):
        await gate("sdk.init", "initialize SDK")


def test_interactive_parser_reuses_real_linux_flow_arguments():
    module = _load_example()
    args = module.parser().parse_args(
        [
            "--runtime-ip",
            "192.168.3.10",
            "--local-vlan-ip",
            "192.168.1.10",
            "--agent-name",
            "Agent A",
            "--owner",
            "owner-a",
            "--masque-url",
            "https://192.168.3.10:4433/.well-known/masque/ip",
        ]
    )

    assert args.runtime_ip == "192.168.3.10"
    assert args.agent_name == "Agent A"
