# Adapters, sinks and helpers

## `ComposeHealthAdapter`

Watches Docker Compose service containers through the Docker Engine API on its unix socket.

```python
from apexai_devops_sdk.compose import ComposeHealthAdapter, ComposePredicates, ComposeService

adapter = ComposeHealthAdapter(
    {"web": ComposeService("shop", "app"), "jobs": ComposeService("shop", "worker")},
    socket_path="/var/run/docker.sock",
    predicates=ComposePredicates(          # your names; these are the defaults
        running="service_running", healthy="service_healthy",
        down_condition="service_down", unhealthy_condition="service_unhealthy",
    ),
)
```

| Container | *running* | *healthy* |
|---|---|---|
| `running`, health check `healthy` | healthy | healthy |
| `running`, health check `unhealthy` | healthy | **broken** |
| `running`, `starting` or no health check | healthy | unobservable |
| `exited`, `dead`, `restarting` | **broken** | unobservable |
| `created`, `paused`, `removing` | unobservable | unobservable |
| missing, mislabelled, unreadable, daemon silent | unobservable | unobservable |

One look is one `GET /containers/<project>-<service>-<index>/json` (Compose v2 naming), capped at
256 KiB, with every receive inside a 3-second deadline. The container must carry its Compose
project and service labels. Only the state words and the labels are read.

**Privilege:** the Docker API is root-equivalent on most hosts. Give a monitor access to it
deliberately. Where an orchestrator already writes a status file per service, reading that file
needs no such access.

## `read_json_status`

```python
from apexai_devops_sdk.statusfile import read_json_status

read = read_json_status("/srv/app/state/web.json", prefix="state")
if read.ok:
    observed = read.document.get("observed")
else:
    reason = read.refusal          # state_missing, state_unreadable, state_not_a_file, ...
```

The file is opened without following a symbolic link, must be a regular file, is read up to
`max_bytes` (64 KiB by default), and must hold one JSON object. What its values mean is your
adapter's to decide.

## `TelegramAttentionSink`

```python
from apexai_devops_sdk.telegram import TelegramAttentionSink

sink = TelegramAttentionSink(
    token_file="/etc/my-monitor/telegram.env",   # TELEGRAM_BOT_TOKEN=... ; mode 0600, yours
    chat_ids=[123456789],
    state_dir="/var/lib/my-monitor/attention",   # yours alone, not group- or world-writable
)
assert sink.check()                              # at start: refuse to run a sink that cannot deliver
```

One HTTPS `sendMessage` per chat, inside a 1.5-second deadline. A 400 or 403 marks that chat
refused for that notice; anything else is retried by the engine. One marker file per notice and
chat makes delivery at least once and de-duplicated across crashes. The message is plain text from
the notice's closed fields; pass `render=` for your own wording.

## `config`

- `read_secret_file(path, key, pattern=None)`: one value from an owner-only `KEY=value` file, or
  `None`.
- `setting`, `absolute_path`, `identifiers`, `integers`, `environment_key`: parse settings or raise
  `SettingError`, naming the setting and never its value.
- `private_directory(path)`: a real directory, yours, writable by no one else.

## `testing`

- `assert_target_adapter(adapter, targets=..., predicates=...)`: the contract checks, for your test
  suite.
- `FakeDockerDaemon`, `compose_container`, `http_response`: a Docker daemon on a unix socket that
  answers what you configure (or stalls, floods, drips, holds the connection).
- `FakeTelegramApi`, `TELEGRAM_OK`: a Bot API on loopback, answering per chat.

Both fakes run as separate processes, so the code under test can run in a forked child as it does
under the engine.
