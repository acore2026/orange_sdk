from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_example():
    path = Path(__file__).parents[1] / "examples" / "full_flow_demo.py"
    spec = importlib.util.spec_from_file_location("full_flow_demo", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_full_flow_example_calls_every_primary_api(capsys):
    module = _load_example()

    summary = await module.run_demo()

    assert summary["runtime_request_count"] == 8
    assert summary["peer_endpoint"] == ("8.8.8.8", 4001)
    assert summary["installed_route"] is True
    assert summary["received_message_count"] == 1
    assert summary["message_delivered"] is True
    assert summary["media_state"] == "STOPPED"
    assert "FULL FLOW DEMO PASSED" in capsys.readouterr().out
