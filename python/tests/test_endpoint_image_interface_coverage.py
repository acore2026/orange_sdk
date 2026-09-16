from __future__ import annotations

import ast
import inspect
from pathlib import Path

from agent_sdk import AgentSdk


EXAMPLES = Path(__file__).parents[1] / "examples"


def _referenced_attributes(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)
    }


def test_arm_endpoint_suite_references_every_public_sdk_interface():
    public_interfaces = {
        name
        for name, member in vars(AgentSdk).items()
        if not name.startswith("_")
        and (
            isinstance(member, property)
            or inspect.isfunction(member)
            or inspect.iscoroutinefunction(member)
        )
    }
    covered = _referenced_attributes(EXAMPLES / "agent_a_test.py")
    covered.update(_referenced_attributes(EXAMPLES / "agent_b_test.py"))

    assert public_interfaces - covered == set()


def test_arm_endpoint_suite_references_every_media_handle_interface():
    covered = _referenced_attributes(EXAMPLES / "agent_a_test.py")
    covered.update(_referenced_attributes(EXAMPLES / "agent_b_test.py"))

    assert {"pause", "resume", "stop", "recv", "close"} <= covered
