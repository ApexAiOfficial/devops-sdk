"""The bridge to the DevOps engine, used at run time where the engine is present.

Everything else in this SDK is engine-neutral. This module converts SDK
values into the engine's own types through the engine's public host
interface, and builds the engine's host wiring for a monitor. It contains no
engine code: it imports the engine lazily, when a function here is called,
and raises :class:`EngineUnavailable` if the engine is absent.

The engine is proprietary, runs only in environments ApexAi operates, and is
not distributed with this SDK or otherwise.

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
import secrets
from collections.abc import Iterable, Mapping
from types import ModuleType
from typing import Any, Final

from apexai_devops_sdk.contract import (
    AttentionSink,
    ContractError,
    Observation,
    Outcome,
    Remedy,
    TargetAdapter,
    Watch,
    is_identifier,
)

CONTROLLER_ID: Final = re.compile(r"ctl_[0-9a-f]{32}")
"""A monitor's machine identity: ``ctl_`` and 32 lower-case hex digits."""

_OBSERVATION = "apexai_ops.control.observation"
_AUTHORITY = "apexai_ops.control.authority"
_REGISTRY = "apexai_ops.modules.registry"
_WIRING = "apexai_ops.runtime.wiring"
_INVENTORY = "apexai_ops.control.inventory"
_REARM = "apexai_ops.control.rearm"
_SUPERVISION = "apexai_ops.control.supervision"

WATCH_SOURCE_PREFIX: Final = "sdk-"
"""Each monitor's watches come from its own inventory source: ``sdk-`` and the last 28
hex digits of its controller id (32 characters, the engine's limit). Stable across
restarts, so the engine applies a changed set as the same source's update; another
monitor's set is another source, whose removals the engine never applies without
confirmation."""

WATCH_AUTHORIZER_ID: Final = "sdk-declared-watches"
"""Who decided a declared watch, as the engine records it: the monitor's own list."""


class EngineUnavailable(RuntimeError):
    """The DevOps engine (or the part of its host interface needed) is not present."""


def _module(name: str) -> ModuleType:
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as missing:
        if missing.name is not None and (
            name == missing.name or name.startswith(f"{missing.name}.")
        ):
            raise EngineUnavailable("the DevOps engine's host interface is not installed") from None
        raise EngineUnavailable("the DevOps engine is installed but failed to import") from missing
    except ImportError as broken:
        raise EngineUnavailable("the DevOps engine is installed but failed to import") from broken


def engine_available(*, watches: bool = False) -> bool:
    """Whether the parts of the engine's host interface a monitor uses can be imported.

    With ``watches=True``, also the parts that declared watches need (an older
    engine has the rest, and :func:`read_only_host_wiring` refuses it watches).
    """

    names: tuple[str, ...] = (_OBSERVATION, _AUTHORITY, _REGISTRY, _WIRING)
    if watches:
        names += (_INVENTORY, _REARM, _SUPERVISION)
    try:
        for name in names:
            _module(name)
    except EngineUnavailable:
        return False
    return not watches or _reconciles_watches(_module(_WIRING))


def _reconciles_watches(wiring: ModuleType) -> bool:
    return "watch_inventory" in getattr(wiring.HostWiring, "__dataclass_fields__", {})


def watch_source_id(controller_id: str) -> str:
    """The inventory source of the monitor ``controller_id``'s declared watches."""

    if not CONTROLLER_ID.fullmatch(controller_id):
        raise ContractError("engine: controller_id must be ctl_ and 32 hex digits")
    return WATCH_SOURCE_PREFIX + controller_id[-28:]


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
    try:
        converted = Outcome(str(getattr(outcome, "value", outcome)))
    except ValueError:
        raise ContractError("engine: the observation's outcome is not a contract Outcome") from None
    fields: dict[str, Any] = {
        "target_id": getattr(value, "target_id", None),
        "predicate_id": getattr(value, "predicate_id", None),
        "outcome": converted,
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


class _DeclaredWatchAuthorizer:
    """The engine's watch authorizer for a monitor's own declared set.

    It authorizes only the monitor's own machine actor: to declare exactly the
    declared watches, and to retire a watch that is not declared. The engine
    asks about retirement only for the store's watches missing from this
    monitor's snapshot. It applies such retirements without confirmation only
    from the source it accepted, within the retirement bound; it stages the
    rest for confirmation. Every other actor, and every other act, is refused.
    """

    def __init__(self, controller_id: str, declared: frozenset[tuple[str, str]]) -> None:
        self._controller = controller_id
        self._declared = declared
        self._rearm = _module(_REARM)

    def authorize_watch(
        self, *, actor_id: str, target_id: str, detection_predicate_id: str, act: Any
    ) -> Any:
        key = (str(target_id), str(detection_predicate_id))
        verb = str(getattr(act, "value", act))
        allowed = actor_id == self._controller and (
            (verb == "declare" and key in self._declared)
            or (verb == "retire" and key not in self._declared)
        )
        return self._rearm.RearmDecision(
            authorized=allowed, authorizer_id=WATCH_AUTHORIZER_ID, actor_kind="service"
        )


class _DeclaredWatchInventory:
    """The monitor's declared watches as one complete snapshot, read once at start."""

    def __init__(self, watches: tuple[Watch, ...], source_id: str) -> None:
        self._source = source_id
        self._items = tuple(
            sorted((w.target_id, w.predicate_id, float(w.interval_seconds)) for w in watches)
        )
        self._inventory = _module(_INVENTORY)
        self._supervision = _module(_SUPERVISION)

    def snapshot(self) -> Any:
        return self._inventory.WatchInventorySnapshot(
            source_id=self._source,
            # Fresh per read: the engine refuses a token an earlier reconciliation presented.
            # A short lowercase label (at most 32 characters), as the engine's
            # durable-identifier rule requires.
            complete_snapshot_token=f"snap-{secrets.token_hex(12)}",
            declared_watches=self._items,
            page_count=1,
            pages_received=1,
            set_digest=self._supervision.inventory_set_digest(self._items),
        )


def read_only_host_wiring(
    adapter: TargetAdapter,
    *,
    controller_id: str,
    policy_revision: str,
    attention_sink: AttentionSink | None = None,
    watches: Iterable[Watch] | None = None,
    max_unconfirmed_retirements: int = 1,
) -> Any:
    """The engine's host wiring for a monitor that observes and alerts, and never acts.

    Read-only at three layers: the standing policy allows no action and no
    target and is disabled; the action registry is empty; and the adapter's
    own remedies are never admitted under that policy. Raises
    :class:`ContractError` for a malformed identity, and whatever the engine
    raises for wiring it refuses.

    ``watches`` is the monitor's complete set of looks (:class:`Watch`). When
    given, even empty, the engine reconciles it once at start: it declares
    each watch as this monitor's own machine actor, and retires watches no
    longer in the set, at most ``max_unconfirmed_retirements`` at a time
    without confirmation. ``None`` (the default) declares no inventory at
    all, and changes no watch. Watches need an engine that reconciles a host's
    watch inventory; with an older one this raises :class:`EngineUnavailable`.
    """

    if not CONTROLLER_ID.fullmatch(controller_id):
        raise ContractError("engine: controller_id must be ctl_ and 32 hex digits")
    if not is_identifier(policy_revision):
        raise ContractError("engine: policy_revision must be an identifier")
    if attention_sink is not None and not isinstance(attention_sink, AttentionSink):
        raise ContractError("engine: attention_sink does not implement AttentionSink")
    try:
        declared = None if watches is None else tuple(watches)
    except TypeError:
        raise ContractError("engine: watches must be an iterable of Watch values") from None
    if declared is not None and any(type(watch) is not Watch for watch in declared):
        raise ContractError("engine: watches must be Watch values")
    keys = frozenset((w.target_id, w.predicate_id) for w in declared or ())
    if declared is not None and len(keys) != len(declared):
        raise ContractError("engine: a watch is declared twice")
    if (
        isinstance(max_unconfirmed_retirements, bool)
        or not isinstance(max_unconfirmed_retirements, int)
        or max_unconfirmed_retirements < 0
    ):
        raise ContractError("engine: max_unconfirmed_retirements must be a whole number")
    authority = _module(_AUTHORITY)
    registry = _module(_REGISTRY)
    wiring = _module(_WIRING)
    inventory: dict[str, Any] = {}
    if declared is not None:
        if not _reconciles_watches(wiring):
            raise EngineUnavailable("this DevOps engine cannot reconcile a host's watches")
        inventory = {
            "watch_authorizer": _DeclaredWatchAuthorizer(controller_id, keys),
            "watch_inventory": _DeclaredWatchInventory(declared, watch_source_id(controller_id)),
            "inventory_retirement_bound": max_unconfirmed_retirements,
        }
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
        **inventory,
    )
