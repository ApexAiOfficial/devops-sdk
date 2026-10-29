"""An attention sink that delivers notices to Telegram chats through the Bot API.

Send-only, built for the engine's offer semantics (see
:class:`~apexai_devops_sdk.contract.AttentionSink`):

- **One HTTPS request per recipient**, opened inside the offer. Every socket
  operation times out at what remains of the offer's deadline (1.5 s by
  default, inside the engine's bound), and an answer completing after it
  counts as failed.
- **At least once across a crash, de-duplicated** by one marker file per
  ``(notification_id, chat)`` in a state directory the host owns: anything
  kept in memory would die with the offer's process.
- **A recipient that cannot be reached** (the API answers 400 or 403: the
  chat never started the bot, blocked it, or is gone) is refused for that
  notice and never retried, and the engine records the refusal; a chat that
  later unblocks the bot receives the notices after it. Any other failure is
  transient and retried by the engine. One failing chat never stops
  delivery to the others: each retry starts at a different chat.

The message is plain text built from the notice's closed words and
identifiers only (:func:`render`, or the host's own ``render``); it is never
parsed as markup.

**The token** is read at offer time from an owner-only secret file
(:func:`apexai_devops_sdk.config.read_secret_file`), never kept in the sink,
never logged or echoed, and only when some chat still needs a send.

**The token travels in the request path** (the Bot API's design), so
``api_base`` must be a server you trust with it: Telegram's, or your own Bot
API server.

**The state directory** must exist, be this user's and be writable by no
one else, or no marker in it is trusted. An offer proves it can write there
before it sends, so a delivery
whose marker cannot be written is not re-sent on every retry. Only a regular
file counts as a marker, and something else at a marker's name stops the
offer rather than risking a duplicate. Markers are never pruned (one small
file per notice and chat); rotating them is the operator's.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import stat
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

from apexai_devops_sdk.config import private_directory, read_secret_file
from apexai_devops_sdk.contract import AttentionNotice, ContractError, SinkOutcome

API_BASE: Final = "https://api.telegram.org"
DEADLINE_SECONDS: Final = 1.5
MAX_RESPONSE_BYTES: Final = 64 * 1024
TOKEN_PATTERN: Final = re.compile(r"[0-9]{5,16}:[A-Za-z0-9_-]{30,64}")

_KEY = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})

ACCEPTED: Final = SinkOutcome.ACCEPTED.value
REFUSED: Final = SinkOutcome.REFUSED.value
FAILED: Final = SinkOutcome.FAILED.value


def render(notice: AttentionNotice) -> str:
    """The default message: the notice's closed words and identifiers, nothing else."""

    lines = [
        f"DevOps attention: {notice.target_id}",
        f"{notice.attention_kind}: decision {notice.work_id} settled {notice.decision_state}",
    ]
    if notice.reason_code:
        lines.append(f"reason: {notice.reason_code}")
    if notice.recovery_kind:
        lines.append(f"recovery: {notice.recovery_kind}")
    lines.append(f"incident {notice.incident_id}, revision {notice.desired_revision}")
    lines.append(f"notification {notice.notification_id}, offer {notice.attempt}")
    return "\n".join(lines)


class TelegramAttentionSink:
    """An :class:`~apexai_devops_sdk.contract.AttentionSink` over ``sendMessage``."""

    def __init__(
        self,
        *,
        token_file: Path | str,
        chat_ids: Iterable[int],
        state_dir: Path | str,
        token_key: str = "TELEGRAM_BOT_TOKEN",
        api_base: str = API_BASE,
        deadline_seconds: float = DEADLINE_SECONDS,
        render: Callable[[AttentionNotice], str] = render,
    ) -> None:
        chats = tuple(chat_ids)
        if (
            not chats
            or len(set(chats)) != len(chats)
            or any(type(chat) is not int or chat == 0 for chat in chats)
        ):
            raise ContractError("telegram: chat_ids must be distinct, non-zero integers")
        if not Path(token_file).is_absolute() or not Path(state_dir).is_absolute():
            raise ContractError("telegram: token_file and state_dir must be absolute paths")
        if not _KEY.fullmatch(token_key):
            raise ContractError("telegram: token_key must be an environment-style name")
        try:
            parts = urlsplit(api_base)
            port = parts.port  # a malformed port raises here, not at the first send
        except ValueError:
            raise ContractError("telegram: api_base is not a URL origin") from None
        secure = parts.scheme == "https" and parts.hostname
        local = parts.scheme == "http" and parts.hostname in _LOOPBACK
        if (
            not (secure or local)
            or parts.path not in ("", "/")
            or parts.query
            or parts.fragment
            or "@" in parts.netloc
        ):
            raise ContractError("telegram: api_base must be an https origin (http on loopback)")
        if not 0 < deadline_seconds < 2.0:
            raise ContractError("telegram: deadline_seconds must be inside the offer's bound")
        self._token_file = Path(token_file)
        self._token_key = token_key
        self._chats = chats
        self._state = Path(state_dir)
        self._scheme, self._host, self._port = parts.scheme, str(parts.hostname), port
        self._deadline = deadline_seconds
        self._render = render

    def __repr__(self) -> str:
        return f"TelegramAttentionSink(chats={len(self._chats)})"  # never the token's path

    def offer(self, payload: Any) -> str:
        """Deliver a notice to every chat not yet told: ``accepted``, ``refused`` or ``failed``."""

        try:
            notice = AttentionNotice.from_payload(payload)
        except (ContractError, TypeError):
            # Not a recipient's refusal: a payload this SDK cannot read (an engine
            # newer than the SDK, say). Failed, so the engine keeps it and retries,
            # rather than refused, which would drop the notice for good.
            return FAILED
        try:
            return self._offer(notice)
        except Exception:  # noqa: BLE001 - every unknown is a transient failure, never text
            return FAILED

    def check(self) -> bool:
        """Whether an offer could deliver now: the state directory and the token are usable.

        Call it at start: a sink that can never deliver should refuse the start.
        """

        return self._writable() and self._token() is not None

    # ------------------------------------------------------------ internals

    def _token(self) -> str | None:
        return read_secret_file(self._token_file, self._token_key, pattern=TOKEN_PATTERN)

    def _offer(self, notice: AttentionNotice) -> str:
        deadline = time.monotonic() + self._deadline
        if not private_directory(self._state):
            return FAILED  # a marker proves nothing in a directory others can write
        delivered = refused = 0
        owed: list[int] = []
        for chat in self._chats:
            told = self._state / f"{notice.notification_id}.{chat}"
            unreachable = self._state / f"{notice.notification_id}.{chat}.refused"
            if _is_marker(told):
                delivered += 1
            elif _is_marker(unreachable):
                refused += 1
            elif os.path.lexists(told) or os.path.lexists(unreachable):
                return FAILED  # something foreign holds the marker's name: never send blind
            else:
                owed.append(chat)
        if not owed:
            return ACCEPTED if delivered else REFUSED
        if not self._writable():
            # Proved before any send: a marker that cannot be written after a
            # delivery would re-send the same message on every retry.
            return FAILED
        token = self._token()
        if token is None:
            return FAILED  # the operator can fix the file; the offer is retried
        text = self._render(notice)
        transient = 0
        # Each retry starts at a different chat, so one that times out (and spends
        # the offer's deadline) cannot keep the chats after it from ever being tried.
        start = notice.attempt % len(owed)
        for chat in owed[start:] + owed[:start]:
            status = self._send(token, chat, text, deadline)
            if status == ACCEPTED:
                _write_marker(self._state / f"{notice.notification_id}.{chat}")
                delivered += 1
            elif status == REFUSED:
                _write_marker(self._state / f"{notice.notification_id}.{chat}.refused")
                refused += 1
            else:
                transient += 1
        if transient:
            return FAILED  # retried; the chats already told are skipped then
        return ACCEPTED if delivered else REFUSED

    def _writable(self) -> bool:
        """The state directory is this user's alone, and a file can be written in it now."""

        if not private_directory(self._state):
            return False
        probe = self._state / f".probe-{os.getpid()}"
        try:
            probe.unlink(missing_ok=True)  # a crashed offer's, if its pid came round again
            _write_marker(probe)
            probe.unlink()
        except OSError:
            return False
        return True

    def _send(self, token: str, chat: int, text: str, deadline: float) -> str:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return FAILED
        connection: http.client.HTTPConnection
        if self._scheme == "https":
            connection = http.client.HTTPSConnection(self._host, self._port, timeout=remaining)
        else:
            connection = http.client.HTTPConnection(self._host, self._port, timeout=remaining)
        body = json.dumps(
            {"chat_id": chat, "text": text, "disable_web_page_preview": True}
        ).encode()
        try:
            connection.connect()  # TCP and TLS, inside the timeout above
            left = deadline - time.monotonic()
            if left <= 0 or connection.sock is None:
                return FAILED
            connection.sock.settimeout(left)  # re-armed for the request's own phase
            connection.request(
                "POST",
                f"/bot{token}/sendMessage",
                body=body,
                headers={"Content-Type": "application/json", "Connection": "close"},
            )
            if connection.sock is not None:
                connection.sock.settimeout(max(deadline - time.monotonic(), 0.001))
            response = connection.getresponse()
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, http.client.HTTPException):
            return FAILED
        finally:
            connection.close()
        if time.monotonic() > deadline or len(raw) > MAX_RESPONSE_BYTES:
            return FAILED
        if response.status in (400, 403):
            return REFUSED
        if response.status != 200:
            return FAILED
        try:
            answer = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            return FAILED
        return ACCEPTED if isinstance(answer, dict) and answer.get("ok") is True else FAILED


def _is_marker(path: Path) -> bool:
    """A marker is a regular file; a link, a directory or anything else proves nothing."""

    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _write_marker(path: Path) -> None:
    """Create a marker, never following a link or truncating something already there."""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(path, flags, 0o600)
    try:
        os.write(descriptor, b"1\n")
    finally:
        os.close(descriptor)
