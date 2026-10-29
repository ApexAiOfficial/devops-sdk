"""Offline stand-ins for the services the SDK's adapters and sinks talk to.

Each fake runs as its own short Python process, not a thread, so the code
under test can be exercised in a forked child as the engine runs it. Both
record what they were asked and answer what the test configures.

- :class:`FakeDockerDaemon`: a unix-socket server answering every connection
  with one canned HTTP response, in one of several modes: ``answer``,
  ``stall`` (never answers), ``flood`` (never stops), ``hold`` (keeps the
  connection open after answering) and ``drip`` (one byte every 0.1 s).
- :class:`FakeTelegramApi`: an HTTP server on loopback answering
  ``sendMessage`` per chat with a configured status and body (or ``stall``).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import TracebackType
from typing import Any

_DAEMON = r"""
import socket, sys, time
path, answer, mode = sys.argv[1], sys.argv[2], sys.argv[3]
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(path)
server.listen(8)
print("ready", flush=True)
data = open(answer, "rb").read()
held = []
while True:
    peer, _ = server.accept()
    request = b""
    while b"\r\n\r\n" not in request:
        chunk = peer.recv(4096)
        if not chunk:
            break
        request += chunk
    with open(path + ".requests", "ab") as log:
        log.write(request)
    if mode == "stall":
        held.append(peer)
        continue
    if mode == "flood":
        try:
            while True:
                peer.sendall(data)
        except OSError:
            pass
        peer.close()
        continue
    if mode == "hold":
        peer.sendall(data)
        held.append(peer)
        continue
    if mode == "drip":
        try:
            for byte in data:
                peer.sendall(bytes((byte,)))
                time.sleep(0.1)
        except OSError:
            pass
        peer.close()
        continue
    try:
        peer.sendall(data)
    except OSError:
        pass
    peer.close()
"""

_API = r"""
import json, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer
config_path, log_path = sys.argv[1], sys.argv[2]
class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        request = json.loads(body)
        with open(log_path, "a") as log:
            log.write(json.dumps({"path": self.path, "body": request}) + "\n")
        config = json.load(open(config_path))
        status, answer = config.get(str(request.get("chat_id")), config["*"])
        if status == "stall":
            time.sleep(30)
            return
        data = answer.encode() if isinstance(answer, str) else json.dumps(answer).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def log_message(self, *args):
        pass
server = HTTPServer(("127.0.0.1", 0), Handler)
print(server.server_address[1], flush=True)
server.serve_forever()
"""

TELEGRAM_OK: list[Any] = [200, {"ok": True, "result": {"message_id": 1}}]


def http_response(status: int | bytes, body: bytes, *, extra_headers: bytes = b"") -> bytes:
    """A raw HTTP/1.0 response, as the Docker daemon would write it."""

    code = status if isinstance(status, bytes) else b"%d" % status
    return (
        b"HTTP/1.0 "
        + code
        + b" X\r\nContent-Type: application/json\r\n"
        + extra_headers
        + b"\r\n"
        + body
    )


def compose_container(
    status: str = "running",
    health: str | None = "healthy",
    *,
    project: str = "example",
    service: str = "app",
    secret: str = "canary-secret-value",
) -> dict[str, Any]:
    """An inspect document for one Compose container, carrying ``secret`` where real ones hold
    secrets (the environment and the health log), so a test can prove it is never echoed."""

    state: dict[str, Any] = {"Status": status, "Running": status == "running"}
    if health is not None:
        state["Health"] = {"Status": health, "Log": [{"Output": f"probe said {secret}"}]}
    return {
        "Id": "c" * 64,
        "State": state,
        "Config": {
            "Env": [f"APP_SECRET={secret}"],
            "Labels": {
                "com.docker.compose.project": project,
                "com.docker.compose.service": service,
            },
        },
    }


class FakeDockerDaemon:
    """A fake Docker daemon on a unix socket in ``directory``. Use as a context manager."""

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)
        self.socket_path = self.directory / "docker.sock"
        self._process: subprocess.Popen[str] | None = None

    def serve(self, answer: bytes, *, mode: str = "answer") -> str:
        """(Re)start serving ``answer``; returns the socket path."""

        if mode not in ("answer", "stall", "flood", "hold", "drip"):
            raise ValueError("unknown mode")
        self.stop()
        self.socket_path.unlink(missing_ok=True)
        Path(str(self.socket_path) + ".requests").unlink(missing_ok=True)
        answer_file = self.directory / "daemon-answer"
        answer_file.write_bytes(answer)
        self._process = subprocess.Popen(
            [sys.executable, "-I", "-c", _DAEMON, str(self.socket_path), str(answer_file), mode],
            stdout=subprocess.PIPE,
            text=True,
        )
        assert self._process.stdout is not None
        if self._process.stdout.readline().strip() != "ready":
            raise RuntimeError("the fake daemon did not start")
        return str(self.socket_path)

    def requests(self) -> bytes:
        """Every request received, concatenated."""

        log = Path(str(self.socket_path) + ".requests")
        return log.read_bytes() if log.exists() else b""

    def stop(self) -> None:
        if self._process is not None:
            self._process.kill()
            self._process.wait(timeout=10)
            self._process = None

    def __enter__(self) -> FakeDockerDaemon:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        value: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()


class FakeTelegramApi:
    """A fake Bot API on loopback, its state in ``directory``. Use as a context manager."""

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)
        self._config = self.directory / "telegram-api.json"
        self._log = self.directory / "telegram-api.log"
        self.answer(TELEGRAM_OK)
        self._process = subprocess.Popen(
            [sys.executable, "-I", "-c", _API, str(self._config), str(self._log)],
            stdout=subprocess.PIPE,
            text=True,
        )
        assert self._process.stdout is not None
        self.base_url = f"http://127.0.0.1:{int(self._process.stdout.readline())}"

    def answer(self, default: list[Any], **per_chat: list[Any]) -> None:
        """Answer every chat with ``default`` (``[status, body]``), or per chat id."""

        staged = self._config.with_suffix(".new")  # re-read per request: never torn
        staged.write_text(json.dumps({"*": default, **per_chat}), encoding="utf-8")
        os.replace(staged, self._config)

    def requests(self) -> list[dict[str, Any]]:
        """Every request received: its path and JSON body."""

        if not self._log.exists():
            return []
        return [json.loads(line) for line in self._log.read_text(encoding="utf-8").splitlines()]

    def stop(self) -> None:
        if self._process.poll() is None:
            self._process.kill()
            self._process.wait(timeout=10)

    def __enter__(self) -> FakeTelegramApi:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        value: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()
