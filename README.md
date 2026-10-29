# ApexAI DevOps SDK

Integrate your application with the **ApexAI DevOps engine**: an autonomous operations engine that
watches your services, decides when something is wrong, tells the right person, and (only where
you authorize it) repairs it, with every decision durable, bounded and attributed.

This repository is the **integration layer**. It gives you:

| Module | What it is |
|---|---|
| `apexai_devops_sdk.contract` | The host contract: `Observation`, `Outcome`, `Remedy`, the `TargetAdapter` and `AttentionSink` protocols, `AttentionNotice` |
| `apexai_devops_sdk.compose` | `ComposeHealthAdapter`: watch Docker Compose services through the Docker Engine API (running, and the image's own `HEALTHCHECK`) |
| `apexai_devops_sdk.statusfile` | `read_json_status`: a bounded, link-safe read of a JSON status file another process writes |
| `apexai_devops_sdk.telegram` | `TelegramAttentionSink`: deliver attention notices to Telegram chats, at least once and de-duplicated |
| `apexai_devops_sdk.config` | Settings helpers that refuse rather than guess, and secrets read from owner-only files |
| `apexai_devops_sdk.testing` | Contract checks for your own adapter, and offline fakes (Docker daemon, Telegram Bot API) |
| `apexai_devops_sdk.engine` | The run-time bridge to the engine, and a read-only host wiring for monitors |

The DevOps engine itself is proprietary and licensed separately. It is **not** part of this
package, and nothing here contains or reproduces it. The SDK has no runtime dependencies beyond the
Python standard library: you can build and test your adapters with the SDK alone, and you need the
engine only where you run it.

## Status

`0.1.0`, alpha. The host contract is version `1` (`apexai_devops_sdk.CONTRACT_VERSION`). Until
`1.0`, minor versions may change the API; every change is in [CHANGELOG.md](CHANGELOG.md).

## Install

```bash
pip install "apexai-devops-sdk @ git+https://github.com/ApexAiOfficial/devops-sdk@v0.1.0"
```

Python 3.11 or later, Linux.

## Quick start: watch a Compose service

```python
from apexai_devops_sdk.compose import ComposeHealthAdapter, ComposeService

adapter = ComposeHealthAdapter({"web": ComposeService(project="shop", service="app")})
look = adapter.observe("web", "service_healthy")
print(look.outcome, look.reason_code)   # e.g. Outcome.OBSERVED_BROKEN service_unhealthy
```

`observe` never raises for the daemon's sake and never guesses. A container it cannot read, a
daemon that does not answer in time, or an answer it does not understand is `unobservable`, which
the engine never acts on. Nothing the daemon returns (the container's environment, the health
check's output) is ever copied into an observation.

## Write your own adapter

Implement the four questions of `TargetAdapter` for your own system, then prove the contract in
your test suite:

```python
from apexai_devops_sdk import Observation, Outcome, Remedy, utc_timestamp
from apexai_devops_sdk.testing import assert_target_adapter

class QueueAdapter:
    observation_timeout_seconds = 2.0

    def observe(self, target_id: str, predicate_id: str) -> Observation:
        backlog = read_backlog(target_id)            # your own, bounded, read-only look
        return Observation(
            target_id=target_id, predicate_id=predicate_id,
            outcome=Outcome.OBSERVED_BROKEN if backlog > 1000 else Outcome.OBSERVED_HEALTHY,
            observed_at=utc_timestamp(), source_id="queue_depth",
            reason_code="backlog_high" if backlog > 1000 else "backlog_ok",
        )

    def target_fingerprint(self, target_id: str) -> str: return f"queue-{target_id}"
    def desired_revision(self, target_id: str) -> int: return 0
    def select_remedy(self, target_id: str, condition_class: str) -> Remedy | None: return None
    def condition_class(self, observation: Observation) -> str: return "backlog_high"

def test_my_adapter_meets_the_contract():
    assert_target_adapter(QueueAdapter(), targets=["orders"], predicates=["backlog"])
```

## Run it under the engine

Where the engine is installed, a host distribution exposes one entry point,
`apexai_ops.hosts` / `runtime`, returning the engine's wiring. For a monitor that observes and
alerts but never acts:

```python
# my_host/wiring.py, registered as [project.entry-points."apexai_ops.hosts"] runtime = "my_host.wiring:runtime"
import os
from apexai_devops_sdk.compose import ComposeHealthAdapter, ComposeService
from apexai_devops_sdk.engine import read_only_host_wiring
from apexai_devops_sdk.telegram import TelegramAttentionSink

def runtime():
    adapter = ComposeHealthAdapter({"web": ComposeService("shop", "app")})
    sink = TelegramAttentionSink(
        token_file="/etc/my-monitor/telegram.env",   # owner-only KEY=value file
        chat_ids=[int(os.environ["MY_MONITOR_CHAT_ID"])],
        state_dir="/var/lib/my-monitor/attention",
    )
    if not sink.check():
        raise RuntimeError("the attention sink cannot deliver")
    return read_only_host_wiring(
        adapter, controller_id=os.environ["MY_MONITOR_CONTROLLER_ID"],
        policy_revision="read-only-r1", attention_sink=sink,
    )
```

See [examples/](examples/) and [docs/](docs/): the [host contract](docs/contract.md), the
[adapters and sinks](docs/adapters.md), and the [architecture](docs/architecture.md), including
what belongs in this SDK, what belongs in your application, and what stays in the engine.

## Security

Report vulnerabilities as described in [SECURITY.md](SECURITY.md). Never put a token or key in an
environment variable, a command line or a log: the SDK reads secrets from owner-only files at the
moment it needs them.

## License

Apache License 2.0. See [LICENSE](LICENSE). The ApexAI DevOps engine is licensed separately.
