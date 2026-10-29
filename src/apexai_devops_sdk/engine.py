"""The bridge to the DevOps engine, used at run time where the engine is installed.

Everything else in this SDK is engine-neutral. This module converts SDK
values into the engine's own types through the engine's public host
interface, and builds the engine's host wiring for a monitor. It contains no
engine code: it imports the engine lazily, when a function here is called,
and raises :class:`EngineUnavailable` if the engine is not installed.

The engine is licensed separately and is not distributed with this SDK.

Typical use, in a host distribution's ``runtime`` entry point::

    from apexai_devops_sdk.engine import read_only_host_wiring

    def runtime():
        adapter = ComposeHealthAdapter({...})
        return read_only_host_wiring(
            adapter, controller_id=os.environ["MY_CONTROLLER_ID"], policy_revision="r1"
        )
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Mapping
from types import ModuleType
from typing import Any, Final

from apexai_devops_sdk.contract import (
    AttentionSink,
    ContractError,
    Observation,
    Outcome,
    Remedy,
    TargetAdapter,
    is_identifier,
)

CONTROLLER_ID: Final = re.compile(r"ctl_[0-9a-f]{32}")
"""A monitor's machine identity: ``ctl_`` and 32 lower-case hex digits."""

_OBSERVATION = "apexai_ops.control.observation"
_AUTHORITY = "apexai_ops.control.authority"
_REGISTRY = "apexai_ops.modules.registry"
_WIRING = "apexai_ops.runtime.wiring"


class EngineUnavailable(RuntimeError):
    """The DevOps engine (or the part of its host interface needed) is not installed."""


def _module(name: str) -> ModuleType:
    try:
        return importlib.import_module(name)
    except ImportError:
        raise EngineUnavailable(
            f"the DevOps engine's host interface is not installed: {name}"
        ) from None


def engine_available() -> bool:
    """Whether the engine's host interface can be imported here."""

    try:
        _module(_OBSERVATION)
        _module(_WIRING)
    except EngineUnavailable:
        return False
    return True


def to_engine_observation(observation: Observation) -> Any:
    """The engine's observation for an SDK :class:`~apexai_devops_sdk.contract.Observation`."""

    if not isinstance(observation, Observation):
        raise ContractError("engine: observe must return an Observation")
    engine = _module(_OBSERVATION)
    return engine.TargetObservation(
        target_id=observation.target_id,
        predicate_id=observation.predicate_id,
        outcome=engine.ObservationOutcome(observation.outcome.value),
        observed_at=observation.observed_at,
        source_id=observation.source_id,
        reason_code=observation.reason_code,
        target_fingerprint=observation.target_fingerprint,
        duration_ms=observation.duration_ms,
        detail=dict(observation.detail),
    )


def to_engine_remedy(remedy: Remedy | None) -> Any:
    """The engine's remedy selection for an SDK :class:`~apexai_devops_sdk.contract.Remedy`."""

    if remedy is None:
        return None
    if not isinstance(remedy, Remedy):
        raise ContractError("engine: select_remedy must return a Remedy or None")
    engine = _module(_OBSERVATION)
    return engine.RemedySelection(
        condition_class=remedy.condition_class,
        action_id=remedy.action_id,
        verification_predicate_id=remedy.verification_predicate_id,
        expected_healthy=remedy.expected_healthy,
    )


def from_engine_observation(value: Any) -> Observation:
    """An SDK observation for one the engine hands back (to ``condition_class``).

    A ``detail`` the SDK contract cannot carry is dropped rather than refused:
    the condition class depends on the predicate, never on free text.
    """

    outcome = getattr(value, "outcome", None)
    fields: dict[str, Any] = {
        "target_id": getattr(value, "target_id", None),
        "predicate_id": getattr(value, "predicate_id", None),
        "outcome": Outcome(str(getattr(outcome, "value", outcome))),
        "observed_at": getattr(value, "observed_at", None),
        "source_id": getattr(value, "source_id", None),
        "reason_code": getattr(value, "reason_code", "") or "",
        "target_fingerprint": getattr(value, "target_fingerprint", None),
        "duration_ms": getattr(value, "duration_ms", 0),
    }
    detail = getattr(value, "detail", None)
    if isinstance(detail, Mapping):
        try:
            return Observation(**fields, detail=dict(detail))
        except ContractError:
            pass
    return Observation(**fields)


class EngineTargetAdapter:
    """An SDK :class:`~apexai_devops_sdk.contract.TargetAdapter` as the engine's adapter.

    Each call goes to the SDK adapter, and its answer is converted to the
    engine's type. Holds nothing but the adapter, so it is as fork-safe as
    the adapter is.
    """

    def __init__(self, adapter: TargetAdapter) -> None:
        if not isinstance(adapter, TargetAdapter):
            raise ContractError("engine: the adapter does not implement TargetAdapter")
        self._adapter = adapter

    @property
    def adapter(self) -> TargetAdapter:
        return self._adapter

    @property
    def observation_timeout_seconds(self) -> float:
        return float(self._adapter.observation_timeout_seconds)

    def observe(self, target_id: str, predicate_id: str) -> Any:
        return to_engine_observation(self._adapter.observe(target_id, predicate_id))

    def target_fingerprint(self, target_id: str) -> str:
        fingerprint = self._adapter.target_fingerprint(target_id)
        if not is_identifier(fingerprint):
            raise ContractError("engine: target_fingerprint must return an identifier")
        return fingerprint

    def desired_revision(self, target_id: str) -> int:
        revision = self._adapter.desired_revision(target_id)
        if type(revision) is not int or revision < 0:
            raise ContractError("engine: desired_revision must return a non-negative integer")
        return revision

    def select_remedy(self, target_id: str, condition_class: str) -> Any:
        return to_engine_remedy(self._adapter.select_remedy(target_id, condition_class))

    def condition_class(self, observation: Any) -> str:
        return self._adapter.condition_class(from_engine_observation(observation))


def read_only_host_wiring(
    adapter: TargetAdapter,
    *,
    controller_id: str,
    policy_revision: str,
    attention_sink: AttentionSink | None = None,
) -> Any:
    """The engine's host wiring for a monitor that observes and alerts, and never acts.

    Read-only at three layers: the standing policy allows no action and no
    target and is disabled; the action registry is empty; and the adapter's
    own remedies are never admitted under that policy. Raises
    :class:`ContractError` for a malformed identity, and whatever the engine
    raises for wiring it refuses.
    """

    if not CONTROLLER_ID.fullmatch(controller_id):
        raise ContractError("engine: controller_id must be ctl_ and 32 hex digits")
    if not is_identifier(policy_revision):
        raise ContractError("engine: policy_revision must be an identifier")
    if attention_sink is not None and not isinstance(attention_sink, AttentionSink):
        raise ContractError("engine: attention_sink does not implement AttentionSink")
    authority = _module(_AUTHORITY)
    registry = _module(_REGISTRY)
    wiring = _module(_WIRING)
    return wiring.HostWiring(
        adapter=EngineTargetAdapter(adapter),
        actor=authority.MachineActor(controller_id=controller_id),
        policy=authority.AutonomyPolicy(
            policy_revision=policy_revision,
            allowed_action_ids=frozenset(),
            allowed_target_ids=frozenset(),
            enabled=False,
        ),
        registry=registry.BotExtensionRegistry(),
        attention_sink=attention_sink,
    )
