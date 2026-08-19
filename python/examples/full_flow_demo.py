"""Compatibility wrapper for the wheel-installed ``agent-sdk-self-check`` command."""

from agent_sdk.full_flow_demo import main, run_demo

__all__ = ["main", "run_demo"]


if __name__ == "__main__":
    main()
