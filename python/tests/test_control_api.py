from __future__ import annotations


async def test_update_capabilities_uses_dedicated_post_endpoint(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]
    update_items = [
        {
            "update_type": "add_skill",
            "skill_name": "camera",
            "reference_vc_id": "vc-camera-002",
        }
    ]
    credentials = [{"id": "vc-camera-002"}]

    await sdk.update_capabilities(
        "did:example:agent-a",
        update_items,
        credentials,
    )

    method, path, body = runtime.requests[-1]
    assert method == "POST"
    assert path == "/arf/v1/agent-cards-update"
    assert body == {
        "agent_id": "did:example:agent-a",
        "update_items": update_items,
        "credentials": credentials,
    }
