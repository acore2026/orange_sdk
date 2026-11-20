# ACN AGW Python SDK wheels

These wheels are built from the current `orange_sdk` and `moq-py` worktrees.
They require Python 3.10 or newer. Install both in the terminal's virtual
environment; pip will resolve the SDK runtime dependencies from its configured
package index:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install ./moq_transport-0.1.0-py3-none-any.whl ./agent_connect_sdk-0.17.11-py3-none-any.whl
```

Application code imports only the public SDK API:

```python
from agent_sdk import AgentSdk, AGWConfig
```

The SDK wheel contains the closed-lab third-party capability signing test key
and is not suitable for production. No Relay service is included or started by
installing these wheels; the Relay is deployed with the core network.

For an offline terminal, prepare a wheelhouse containing these two wheels and
all transitive runtime dependencies for the target Python version and platform,
then install with `python -m pip install --no-index --find-links=wheelhouse ...`.
