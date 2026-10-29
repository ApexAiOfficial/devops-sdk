"""The host contract: what a host gives the DevOps engine, and what it is offered back.

This module is engine-neutral: it imports nothing from the engine, so a host
can build and test its adapters with this SDK alone. :mod:`apexai_devops_sdk.engine`
converts these values into the engine's own types at run time.

The contract has two directions:

- **Observation** (host to engine). A :class:`TargetAdapter` answers one
  question at a time: "is predicate P true of target T right now?" with an
  :class:`Observation`. Only ``OBSERVED_BROKEN`` can ever lead to a decision;
  ``UNOBSERVABLE`` and ``ERROR`` are never acted on. When an adapter cannot
  tell, it must say so rather than guess.
- **Attention** (engine to host). When a decision needs a person, the engine
  offers an :class:`AttentionNotice` to the host's :class:`AttentionSink`,
  which answers with a :class:`SinkOutcome`. Offers are at least once; every
  notice carries a ``notification_id`` so a sink can de-duplicate.

Every value here is closed words, identifiers and primitives. Nothing an
adapter returns should carry free text from the system it observes (logs,
error output, environment): such text can hold secrets, and observations may
be published to operators.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final, Protocol, runtime_checkable

CONTRACT_VERSION: Final = "1"
"""The host contract this SDK implements. A breaking change raises it."""

MAX_DETAIL_BYTES: Final = 4096
"""An observation's ``detail``, as compact JSON, is at most this many bytes."""

IDENTIFIER: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
"""Target, predicate, source, action and condition identifiers."""

CLOSED_WORD: Final = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,63}")
"""Reason codes and other closed vocabulary: lower case, no spaces, no free text."""

NOTIFICATION_ID: Final = re.compile(r"ntf_[0-9a-f]{8,64}")
"""The shape of an attention notification's identifier."""

_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z")


class ContractError(ValueError):
    """A value outside the host contract. The message names the field, never its content."""


def is_identifier(value: object) -> bool:
    """Whether ``value`` is a contract identifier."""

    return isinstance(value, str) and IDENTIFIER.fullmatch(value) is not None


def is_closed_word(value: object) -> bool:
    """Whether ``value`` is a closed vocabulary word (a reason code, a state word)."""

    return isinstance(value, str) and CLOSED_WORD.fullmatch(value) is not None


def utc_timestamp(moment: datetime | None = None) -> str:
    """``moment`` (default: now) as the contract's timestamp, ISO 8601 in UTC with ``Z``."""

    when = datetime.now(UTC) if moment is None else moment
    if when.tzinfo is None:
        raise ContractError("timestamp: a naive datetime has no time zone")
    return when.astimezone(UTC).isoformat().replace("+00:00", "Z")


class Outcome(StrEnum):
    """The four results of one observation. Only ``OBSERVED_BROKEN`` can lead to action."""

    OBSERVED_HEALTHY = "observed_healthy"
    OBSERVED_BROKEN = "observed_broken"
    UNOBSERVABLE = "unobservable"
    ERROR = "error"


def _primitive(value: object) -> bool:
    """A detail value: a number, a bool, ``None``, or a string that is an identifier.

    Strings are held to the identifier shape (no spaces, no slashes, at most
    128 characters), so free text such as a log line, an error message or a
    path cannot ride in a detail.
    """

    if value is None or isinstance(value, (bool, int)):
        return True
    if isinstance(value, str):
        return IDENTIFIER.fullmatch(value) is not None
    return isinstance(value, float) and math.isfinite(value)


@dataclass(frozen=True, slots=True)
class Observation:
    """One fresh, bounded observation of a named predicate on a target.

    ``detail`` holds small primitives only: numbers, bools, ``None`` and
    identifier-shaped strings (no nested values, no free text), at most
    :data:`MAX_DETAIL_BYTES` as JSON. It is copied when the observation is
    made, so a caller that reuses its own mapping cannot change it later.
    ``target_fingerprint`` identifies what the target currently *is* (for
    example a store and the bot it serves): when it changes, the engine
    treats the target as a different one.
    """

    target_id: str
    predicate_id: str
    outcome: Outcome
    observed_at: str
    source_id: str
    reason_code: str = ""
    target_fingerprint: str | None = None
    duration_ms: int = 0
    detail: Mapping[str, str | int | float | bool | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not is_identifier(self.target_id):
            raise ContractError("observation: target_id is not an identifier")
        if not is_identifier(self.predicate_id):
            raise ContractError("observation: predicate_id is not an identifier")
        if not isinstance(self.outcome, Outcome):
            raise ContractError("observation: outcome is not an Outcome")
        if not isinstance(self.observed_at, str) or not _TIMESTAMP.fullmatch(self.observed_at):
            raise ContractError("observation: observed_at is not a UTC timestamp")
        if not is_identifier(self.source_id):
            raise ContractError("observation: source_id is not an identifier")
        if self.reason_code != "" and not is_closed_word(self.reason_code):
            raise ContractError("observation: reason_code is not a closed word")
        if self.target_fingerprint is not None and not is_identifier(self.target_fingerprint):
            raise ContractError("observation: target_fingerprint is not an identifier")
        if type(self.duration_ms) is not int or self.duration_ms < 0:
            raise ContractError("observation: duration_ms is not a non-negative integer")
        if not isinstance(self.detail, Mapping) or not all(
            is_identifier(key) and _primitive(value) for key, value in self.detail.items()
        ):
            raise ContractError("observation: detail holds something other than primitives")
        copied = dict(self.detail)
        size = len(json.dumps(copied, separators=(",", ":"), sort_keys=True))
        if size > MAX_DETAIL_BYTES:
            raise ContractError("observation: detail is larger than MAX_DETAIL_BYTES")
        object.__setattr__(self, "detail", copied)  # the caller's mapping is not kept

    @property
    def conclusive(self) -> bool:
        """Whether the observation established the target's state either way."""

        return self.outcome in (Outcome.OBSERVED_HEALTHY, Outcome.OBSERVED_BROKEN)


@dataclass(frozen=True, slots=True)
class Remedy:
    """A host-registered remedy for one condition class.

    ``action_id`` names an action the host registered with the engine; the
    engine decides whether, when and under what authority it runs. After
    it runs, ``verification_predicate_id`` must observe ``expected_healthy``.
    """

    condition_class: str
    action_id: str
    verification_predicate_id: str
    expected_healthy: bool = True

    def __post_init__(self) -> None:
        for name in ("condition_class", "action_id", "verification_predicate_id"):
            if not is_identifier(getattr(self, name)):
                raise ContractError(f"remedy: {name} is not an identifier")
        if type(self.expected_healthy) is not bool:
            raise ContractError("remedy: expected_healthy is not a bool")


@runtime_checkable
class TargetAdapter(Protocol):
    """The host's knowledge of its targets: how to look, and what a broken look means.

    The engine calls every member inside a time bound and treats an
    exception, a timeout or a malformed answer as an error, never as broken.
    Members must be fast, read-only and safe to call from a forked child:
    open what they need when they are called, and start no threads.
    """

    @property
    def observation_timeout_seconds(self) -> float:
        """How long one observation may take; the engine may allow less."""
        ...

    def observe(self, target_id: str, predicate_id: str) -> Observation:
        """A fresh observation of one predicate on one target."""
        ...

    def target_fingerprint(self, target_id: str) -> str:
        """A stable identifier of what the target currently is."""
        ...

    def desired_revision(self, target_id: str) -> int:
        """The target's desired-state revision; 0 when the host has none."""
        ...

    def select_remedy(self, target_id: str, condition_class: str) -> Remedy | None:
        """The registered remedy for a condition, or ``None`` (detection only)."""
        ...

    def condition_class(self, observation: Observation) -> str:
        """The durable condition class of a broken observation."""
        ...


class SinkOutcome(StrEnum):
    """A sink's answer to one offer."""

    ACCEPTED = "accepted"
    """Delivered (or already delivered) to at least one recipient."""
    REFUSED = "refused"
    """Can never be delivered (every recipient refused); not retried."""
    FAILED = "failed"
    """Not delivered this time; the engine offers it again later."""


_NOTICE_FIELDS: Final = (
    "notification_id",
    "work_id",
    "attention_kind",
    "incident_id",
    "desired_revision",
    "target_id",
    "decision_state",
    "reason_code",
    "recovery_kind",
    "attempt",
    "settled_at",
)


@dataclass(frozen=True, slots=True)
class AttentionNotice:
    """What the engine offers a sink: closed words, identifiers and timestamps only."""

    notification_id: str
    work_id: str
    attention_kind: str
    incident_id: str
    desired_revision: int
    target_id: str
    decision_state: str
    reason_code: str | None
    recovery_kind: str | None
    attempt: int
    settled_at: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.notification_id, str) or not NOTIFICATION_ID.fullmatch(
            self.notification_id
        ):
            raise ContractError("notice: notification_id is not a notification identifier")
        for name in ("work_id", "incident_id", "target_id"):
            if not is_identifier(getattr(self, name)):
                raise ContractError(f"notice: {name} is not an identifier")
        for name in ("attention_kind", "decision_state"):
            if not is_closed_word(getattr(self, name)):
                raise ContractError(f"notice: {name} is not a closed word")
        for name in ("reason_code", "recovery_kind"):
            value = getattr(self, name)
            if value is not None and not is_closed_word(value):
                raise ContractError(f"notice: {name} is not a closed word")
        for name in ("desired_revision", "attempt"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ContractError(f"notice: {name} is not a non-negative integer")
        if self.settled_at is not None and (
            not isinstance(self.settled_at, str) or not _TIMESTAMP.fullmatch(self.settled_at)
        ):
            raise ContractError("notice: settled_at is not a UTC timestamp")

    @classmethod
    def from_payload(cls, payload: object) -> AttentionNotice:
        """The notice in any payload with the contract's fields.

        Accepts this class, a mapping, an object with a ``safe_record()``
        method returning one (the engine's payload), or an object with the
        fields as attributes. Raises :class:`ContractError` otherwise.
        """

        if isinstance(payload, AttentionNotice):
            return payload
        record: Any = payload
        safe = getattr(payload, "safe_record", None)
        if callable(safe):
            record = safe()
        values: dict[str, Any]
        if isinstance(record, Mapping):
            values = {name: record.get(name) for name in _NOTICE_FIELDS}
        else:
            values = {name: getattr(record, name, None) for name in _NOTICE_FIELDS}
        return cls(**values)

    def record(self) -> dict[str, object]:
        """The notice as a plain mapping."""

        return {name: getattr(self, name) for name in _NOTICE_FIELDS}


@runtime_checkable
class AttentionSink(Protocol):
    """A host transport for attention notices.

    ``offer`` runs inside the engine's time bound, in a child process. It
    returns a :class:`SinkOutcome` value; anything else, an exception or a
    timeout counts as ``failed`` and is offered again later.
    """

    def offer(self, payload: Any) -> str:
        """Deliver one notice; answer ``accepted``, ``refused`` or ``failed``."""
        ...
