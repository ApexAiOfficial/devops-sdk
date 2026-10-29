"""A read-only target adapter over Docker Compose services, through the Docker Engine API.

Each target is one Compose service container. One look is one
``GET /containers/<project>-<service>-<index>/json`` over the daemon's unix
socket (HTTP/1.0, the answer capped, every receive inside the look's
deadline): no subprocess, no write, no other request.

**Two closed predicates per target** (their names are the host's, see
:class:`ComposePredicates`):

- *running*: healthy when the container is ``running``; broken when it is
  ``exited``, ``dead`` or ``restarting``; inconclusive when ``created``,
  ``paused`` or ``removing``.
- *healthy*: judged only on a running container, by the image's own
  ``HEALTHCHECK``: ``healthy`` or ``unhealthy``; ``starting`` and an image
  without a health check are inconclusive.

**Identity.** The container must carry its Compose project and service
labels, or the look is ``container_mismatch``. The fingerprint is the target,
its project and its service, so a container re-created by an upgrade is the
same target.

**Nothing the daemon returned is echoed.** The inspect answer includes the
container's environment (often secrets) and its health-check output. Only
the state words and the two labels are read, and only closed words cross
into an observation.

**Privilege.** Access to the Docker API is root-equivalent on most hosts.
Grant it to a monitor deliberately; a status file the orchestrator writes
(:mod:`apexai_devops_sdk.statusfile`) needs no such access.

Refused rather than guessed: Compose v1 names or a custom ``container_name``
(``container_missing``), more than one replica (name the index), a deadline
that does not fit inside the look's bound.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from apexai_devops_sdk.contract import (
    ContractError,
    Observation,
    Outcome,
    Remedy,
    is_closed_word,
    is_identifier,
    utc_timestamp,
)

DEFAULT_SOCKET: Final = "/var/run/docker.sock"
SOURCE_ID: Final = "docker_container_state"
MAX_RESPONSE_BYTES: Final = 256 * 1024
REQUEST_DEADLINE_SECONDS: Final = 3.0
OBSERVATION_TIMEOUT_SECONDS: Final = 5.0
PROJECT_LABEL: Final = "com.docker.compose.project"
SERVICE_LABEL: Final = "com.docker.compose.service"

_RUNNING = "running"
_DOWN = frozenset({"exited", "dead", "restarting"})
_PASSING = frozenset({"created", "paused", "removing"})
_HEALTH = frozenset({"healthy", "unhealthy", "starting"})
_NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,62}")
_STATUS_CODE = re.compile(rb"[0-9]{3}")
_LENGTH = re.compile(rb"[0-9]{1,9}")


@dataclass(frozen=True, slots=True)
class ComposeService:
    """Where one target runs: its Compose project, service and replica index."""

    project: str
    service: str
    index: int = 1

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.project) or not _NAME.fullmatch(self.service):
            raise ContractError("compose: project and service must be Compose names")
        if type(self.index) is not int or self.index < 1:
            raise ContractError("compose: index must be a positive integer")

    @property
    def container(self) -> str:
        """Compose v2's container name."""

        return f"{self.project}-{self.service}-{self.index}"


@dataclass(frozen=True, slots=True)
class ComposePredicates:
    """The host's names for the two predicates and their condition classes."""

    running: str = "service_running"
    healthy: str = "service_healthy"
    down_condition: str = "service_down"
    unhealthy_condition: str = "service_unhealthy"

    def __post_init__(self) -> None:
        if not (is_identifier(self.running) and is_identifier(self.healthy)) or (
            self.running == self.healthy
        ):
            raise ContractError("compose: the two predicates must be distinct identifiers")
        # A broken look's reason code is its condition class, so both are closed words.
        if not (is_closed_word(self.down_condition) and is_closed_word(self.unhealthy_condition)):
            raise ContractError("compose: condition classes must be closed words")
        if self.down_condition == self.unhealthy_condition:
            raise ContractError("compose: the two condition classes must be distinct")


class _Refused(Exception):
    """A look that ends in a closed reason code; never carries daemon text."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ComposeHealthAdapter:
    """A :class:`~apexai_devops_sdk.contract.TargetAdapter` over Compose service containers."""

    def __init__(
        self,
        targets: Mapping[str, ComposeService],
        *,
        socket_path: str = DEFAULT_SOCKET,
        predicates: ComposePredicates | None = None,
        remedies: Mapping[str, Remedy] | None = None,
        clock: Callable[[], datetime] | None = None,
        deadline_seconds: float = REQUEST_DEADLINE_SECONDS,
        observation_timeout_seconds: float = OBSERVATION_TIMEOUT_SECONDS,
        source_id: str = SOURCE_ID,
    ) -> None:
        if not targets or not all(
            is_identifier(target) and isinstance(where, ComposeService)
            for target, where in targets.items()
        ):
            raise ContractError("compose: targets must map identifiers to ComposeService")
        if not os.path.isabs(socket_path):
            raise ContractError("compose: socket_path must be an absolute path")
        if not 0 < deadline_seconds < observation_timeout_seconds:
            raise ContractError("compose: deadline_seconds must fit inside the look's bound")
        if not is_identifier(source_id):
            raise ContractError("compose: source_id must be an identifier")
        self._targets = dict(targets)
        self._socket = socket_path
        self._names = predicates or ComposePredicates()
        conditions = {self._names.down_condition, self._names.unhealthy_condition}
        chosen = dict(remedies or {})
        if not all(
            isinstance(remedy, Remedy)
            and condition in conditions
            and remedy.condition_class == condition
            for condition, remedy in chosen.items()
        ):
            raise ContractError("compose: each remedy must be keyed by its own condition class")
        self._remedies = chosen
        self._clock = clock
        self._deadline = deadline_seconds
        self._timeout = observation_timeout_seconds
        self._source = source_id

    @property
    def targets(self) -> tuple[str, ...]:
        return tuple(sorted(self._targets))

    @property
    def predicates(self) -> ComposePredicates:
        return self._names

    @property
    def observation_timeout_seconds(self) -> float:
        return self._timeout

    # ------------------------------------------------------------ the protocol

    def observe(self, target_id: str, predicate_id: str) -> Observation:
        names = self._names
        if predicate_id not in (names.running, names.healthy):
            return self._result(target_id, predicate_id, Outcome.UNOBSERVABLE, "predicate_unknown")
        where = self._targets.get(target_id)
        if where is None:
            return self._result(target_id, predicate_id, Outcome.UNOBSERVABLE, "target_unknown")
        began = time.monotonic()
        try:
            state, health = self._inspect(where)
        except _Refused as refused:
            return self._result(target_id, predicate_id, Outcome.UNOBSERVABLE, refused.reason)
        spent = int((time.monotonic() - began) * 1000)
        if predicate_id == names.running:
            if state == _RUNNING:
                outcome, reason = Outcome.OBSERVED_HEALTHY, "running"
            elif state in _DOWN:
                outcome, reason = Outcome.OBSERVED_BROKEN, names.down_condition
            else:
                outcome, reason = Outcome.UNOBSERVABLE, f"container_{state}"
        elif state != _RUNNING:
            # Liveness is the running predicate's; health is judged on a running container.
            outcome, reason = Outcome.UNOBSERVABLE, f"container_{state}"
        elif health == "healthy":
            outcome, reason = Outcome.OBSERVED_HEALTHY, "healthy"
        elif health == "unhealthy":
            outcome, reason = Outcome.OBSERVED_BROKEN, names.unhealthy_condition
        else:  # starting, or no health check declared
            outcome, reason = Outcome.UNOBSERVABLE, f"health_{health}"
        return self._result(
            target_id,
            predicate_id,
            outcome,
            reason,
            detail={"state": state, "health": health},
            fingerprint=self._fingerprint(target_id),
            duration_ms=spent,
        )

    def target_fingerprint(self, target_id: str) -> str:
        return self._fingerprint(target_id)

    def desired_revision(self, target_id: str) -> int:
        return 0  # a container states no desired revision; none is invented

    def select_remedy(self, target_id: str, condition_class: str) -> Remedy | None:
        return self._remedies.get(condition_class) if target_id in self._targets else None

    def condition_class(self, observation: Observation) -> str:
        if observation.predicate_id == self._names.running:
            return self._names.down_condition
        if observation.predicate_id == self._names.healthy:
            return self._names.unhealthy_condition
        return "condition_unknown"

    # ------------------------------------------------------------ internals

    def _inspect(self, where: ComposeService) -> tuple[str, str]:
        """The container's state and health words, or a closed refusal."""

        status, body = self._get(f"/containers/{where.container}/json")
        if status == 404:
            raise _Refused("container_missing")
        if status != 200:
            raise _Refused("daemon_refused")
        try:
            document = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise _Refused("response_malformed") from None
        if not isinstance(document, dict):
            raise _Refused("response_malformed")
        config, state = document.get("Config"), document.get("State")
        labels = config.get("Labels") if isinstance(config, dict) else None
        if not isinstance(labels, dict) or (
            labels.get(PROJECT_LABEL) != where.project or labels.get(SERVICE_LABEL) != where.service
        ):
            raise _Refused("container_mismatch")
        if not isinstance(state, dict):
            raise _Refused("state_unrecognised")
        word = state.get("Status")
        if type(word) is not str or (word != _RUNNING and word not in _DOWN | _PASSING):
            raise _Refused("state_unrecognised")
        check = state.get("Health")
        if check is None:
            return word, "undeclared"
        health = check.get("Status") if isinstance(check, dict) else None
        if type(health) is not str or health not in _HEALTH:
            raise _Refused("state_unrecognised")
        return word, health

    def _get(self, path: str) -> tuple[int, bytes]:
        """One HTTP/1.0 GET over the daemon's socket: the status and the capped body."""

        deadline = time.monotonic() + self._deadline

        def remaining() -> float:
            left = deadline - time.monotonic()
            if left <= 0:
                raise _Refused("daemon_timeout")
            return left

        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        raw = bytearray()
        try:
            try:
                connection.settimeout(remaining())
                connection.connect(self._socket)
                connection.settimeout(remaining())
                connection.sendall(f"GET {path} HTTP/1.0\r\nHost: docker\r\n\r\n".encode())
                while len(raw) <= MAX_RESPONSE_BYTES and not _whole(raw):
                    connection.settimeout(remaining())  # re-armed on every receive
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    raw += chunk
            except TimeoutError:
                raise _Refused("daemon_timeout") from None
            except OSError:
                raise _Refused("daemon_unreachable") from None
        finally:
            connection.close()
        if len(raw) > MAX_RESPONSE_BYTES:
            raise _Refused("response_oversized")
        head, separator, body = bytes(raw).partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        words = lines[0].split(b" ", 2)
        if (
            not separator
            or len(words) < 2
            or not words[0].startswith(b"HTTP/1.")
            or not _STATUS_CODE.fullmatch(words[1])
        ):
            raise _Refused("response_malformed")
        length = _content_length(lines[1:])
        if length is not None:
            if len(body) < length:
                raise _Refused("response_malformed")  # closed before the whole body
            body = body[:length]
        return int(words[1]), body

    def _fingerprint(self, target_id: str) -> str:
        where = self._targets.get(target_id)
        material = f"{target_id}\0{where.project}\0{where.service}" if where else target_id
        return hashlib.sha256(material.encode()).hexdigest()[:32]

    def _result(
        self,
        target_id: str,
        predicate_id: str,
        outcome: Outcome,
        reason: str,
        *,
        detail: dict[str, str] | None = None,
        fingerprint: str | None = None,
        duration_ms: int = 0,
    ) -> Observation:
        moment = None if self._clock is None else self._clock()
        # An identifier the caller passed that the contract cannot carry is named, not echoed.
        return Observation(
            target_id=target_id if is_identifier(target_id) else "invalid-target",
            predicate_id=predicate_id if is_identifier(predicate_id) else "invalid-predicate",
            outcome=outcome,
            observed_at=utc_timestamp(moment),
            source_id=self._source,
            reason_code=reason,
            target_fingerprint=fingerprint,
            duration_ms=duration_ms,
            detail=detail or {},
        )


def _content_length(header_lines: list[bytes]) -> int | None:
    """The one well-formed ``Content-Length``, or ``None``; never chunked (an HTTP/1.0 answer)."""

    length = None
    for line in header_lines:
        name, _colon, value = line.partition(b":")
        name = name.strip().lower()
        if name == b"transfer-encoding":
            raise _Refused("response_malformed")
        if name == b"content-length":
            if length is not None or not _LENGTH.fullmatch(value.strip()):
                raise _Refused("response_malformed")
            length = int(value.strip())
    return length


def _whole(raw: bytearray) -> bool:
    """Whether the answer is complete by its ``Content-Length`` (a kept-open socket ends there)."""

    head, separator, body = bytes(raw).partition(b"\r\n\r\n")
    if not separator:
        return False
    try:
        length = _content_length(head.split(b"\r\n")[1:])
    except _Refused:
        return True  # malformed: stop reading; the parse refuses it
    return length is not None and len(body) >= length
