"""The run-time bridge, against a stand-in for the engine's host interface.

The engine is proprietary and is not present in this repository's CI. These
tests install a minimal stand-in exposing the documented constructors the
bridge calls, so the conversion is checked everywhere; ``test_engine_live.py``
repeats the essentials against the real engine where it is present.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import pytest

from apexai_devops_sdk import ContractError, Observation, Outcome, Remedy, Watch
from apexai_devops_sdk import engine as bridge
from apexai_devops_sdk.compose import ComposeHealthAdapter, ComposeService

CONTROLLER = "ctl_" + "0" * 32


class _Outcome(StrEnum):
    OBSERVED_HEALTHY = "observed_healthy"
    OBSERVED_BROKEN = "observed_broken"
    UNOBSERVABLE = "unobservable"
    ERROR = "error"


@dataclass(frozen=True)
class _TargetObservation:
    target_id: str
    predicate_id: str
    outcome: _Outcome
    observed_at: str
    source_id: str
    reason_code: str = ""
    target_fingerprint: str | None = None
    duration_ms: int = 0
    detail: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _RemedySelection:
    condition_class: str
    action_id: str
    verification_predicate_id: str
    expected_healthy: bool = True


@dataclass(frozen=True)
class _MachineActor:
    controller_id: str


@dataclass(frozen=True)
class _AutonomyPolicy:
    policy_revision: str
    allowed_action_ids: frozenset[str]
    allowed_target_ids: frozenset[str]
    enabled: bool = True


class _Registry:
    pass


@dataclass
class _HostWiring:
    adapter: Any
    actor: Any
    policy: Any
    registry: Any
    attention_sink: Any = None


@pytest.fixture
def stand_in(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    modules = {
        "apexai_ops": {},
        "apexai_ops.control": {},
        "apexai_ops.control.observation": {
            "ObservationOutcome": _Outcome,
            "TargetObservation": _TargetObservation,
            "RemedySelection": _RemedySelection,
        },
        "apexai_ops.control.authority": {
            "MachineActor": _MachineActor,
            "AutonomyPolicy": _AutonomyPolicy,
        },
        "apexai_ops.modules": {},
        "apexai_ops.modules.registry": {"BotExtensionRegistry": _Registry},
        "apexai_ops.runtime": {},
        "apexai_ops.runtime.wiring": {"HostWiring": _HostWiring},
    }
    for name, members in modules.items():
        module = types.ModuleType(name)
        for member, value in members.items():
            setattr(module, member, value)
        monkeypatch.setitem(sys.modules, name, module)
    yield


@dataclass(frozen=True)
class _WatchInventorySnapshot:
    source_id: str
    complete_snapshot_token: str
    declared_watches: tuple[tuple[str, str, float], ...]
    page_count: int
    pages_received: int
    set_digest: str


@dataclass(frozen=True)
class _RearmDecision:
    authorized: bool
    authorizer_id: str
    actor_kind: str


@dataclass
class _WatchHostWiring(_HostWiring):
    watch_authorizer: Any = None
    watch_inventory: Any = None
    inventory_retirement_bound: Any = None


@pytest.fixture
def watch_stand_in(stand_in: None, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The stand-in, as an engine that reconciles a host's watch inventory."""

    modules: dict[str, dict[str, Any]] = {
        "apexai_ops.control.inventory": {"WatchInventorySnapshot": _WatchInventorySnapshot},
        "apexai_ops.control.rearm": {"RearmDecision": _RearmDecision},
        "apexai_ops.control.supervision": {"inventory_set_digest": lambda items: f"set:{items!r}"},
    }
    for name, members in modules.items():
        module = types.ModuleType(name)
        for member, value in members.items():
            setattr(module, member, value)
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(sys.modules["apexai_ops.runtime.wiring"], "HostWiring", _WatchHostWiring)
    yield


WATCHES = (Watch("web", "store_health", 60), Watch("api", "store_running", 30))


def _watch_wiring(**kwargs: Any) -> Any:
    adapter = ComposeHealthAdapter({"web": ComposeService("example", "app")},
                                   socket_path="/run/docker.sock")  # fmt: skip
    return bridge.read_only_host_wiring(
        adapter, controller_id=CONTROLLER, policy_revision="r1", **kwargs
    )


def test_an_older_engine_refuses_watches_and_says_so(stand_in: None) -> None:
    assert bridge.engine_available() and not bridge.engine_available(watches=True)
    with pytest.raises(bridge.EngineUnavailable):
        _watch_wiring(watches=WATCHES)
    assert _watch_wiring().attention_sink is None


def test_an_engine_whose_wiring_cannot_take_watches_is_not_reported_able(
    watch_stand_in: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The watch modules import, but the host wiring predates the inventory: not able."""

    monkeypatch.setattr(sys.modules["apexai_ops.runtime.wiring"], "HostWiring", _HostWiring)
    assert bridge.engine_available() and not bridge.engine_available(watches=True)
    with pytest.raises(bridge.EngineUnavailable):
        _watch_wiring(watches=WATCHES)


def test_without_watches_the_wiring_declares_nothing(watch_stand_in: None) -> None:
    assert bridge.engine_available(watches=True)
    wiring = _watch_wiring()
    assert wiring.watch_inventory is None and wiring.watch_authorizer is None


def test_the_inventory_is_one_complete_snapshot_with_a_fresh_token(watch_stand_in: None) -> None:
    wiring = _watch_wiring(watches=WATCHES)
    first, second = wiring.watch_inventory.snapshot(), wiring.watch_inventory.snapshot()
    items = (("api", "store_running", 30.0), ("web", "store_health", 60.0))
    assert first.declared_watches == items and first.set_digest == f"set:{items!r}"
    assert (first.page_count, first.pages_received) == (1, 1)
    assert first.source_id == second.source_id == bridge.watch_source_id(CONTROLLER)
    assert first.complete_snapshot_token != second.complete_snapshot_token
    token = first.complete_snapshot_token
    assert len(token) <= 32 and token == token.lower(), token
    assert wiring.inventory_retirement_bound == 1


def test_an_empty_set_is_a_set(watch_stand_in: None) -> None:
    wiring = _watch_wiring(watches=())
    assert wiring.watch_inventory.snapshot().declared_watches == ()
    assert wiring.watch_authorizer is not None


def test_the_authorizer_allows_only_the_monitor_its_own_set(watch_stand_in: None) -> None:
    authorize = _watch_wiring(watches=WATCHES).watch_authorizer.authorize_watch

    def allowed(actor: str, target: str, predicate: str, act: str) -> bool:
        decision = authorize(
            actor_id=actor, target_id=target, detection_predicate_id=predicate, act=act
        )
        assert decision.authorizer_id == bridge.WATCH_AUTHORIZER_ID
        return bool(decision.authorized)

    assert allowed(CONTROLLER, "web", "store_health", "declare")
    assert not allowed("ctl_" + "3" * 32, "web", "store_health", "declare")
    assert not allowed(CONTROLLER, "web", "store_running", "declare")
    assert allowed(CONTROLLER, "gone", "store_health", "retire")
    assert not allowed(CONTROLLER, "web", "store_health", "retire")
    assert not allowed("ctl_" + "3" * 32, "gone", "store_health", "retire")
    assert not allowed(CONTROLLER, "web", "store_health", "pause")


def _observation(outcome: Outcome = Outcome.OBSERVED_BROKEN) -> Observation:
    return Observation(
        target_id="web", predicate_id="service_running", outcome=outcome,
        observed_at="2026-10-29T07:00:00Z", source_id="example", reason_code="service_down",
        target_fingerprint="f" * 32, duration_ms=4, detail={"state": "exited"},
    )  # fmt: skip


def test_without_the_engine_the_bridge_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [m for m in sys.modules if m == "apexai_ops" or m.startswith("apexai_ops.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "apexai_ops", None)  # an import of it fails
    assert not bridge.engine_available()
    with pytest.raises(bridge.EngineUnavailable):
        bridge.to_engine_observation(_observation())


def test_an_observation_round_trips(stand_in: None) -> None:
    engine_value = bridge.to_engine_observation(_observation())
    assert isinstance(engine_value, _TargetObservation)
    assert engine_value.outcome is _Outcome.OBSERVED_BROKEN
    assert dict(engine_value.detail) == {"state": "exited"}
    assert bridge.from_engine_observation(engine_value) == _observation()
    assert bridge.engine_available()


def test_a_detail_the_contract_cannot_carry_is_dropped_on_the_way_back(stand_in: None) -> None:
    engine_value = _TargetObservation(
        target_id="web", predicate_id="p", outcome=_Outcome.ERROR,
        observed_at="2026-10-29T07:00:00Z", source_id="engine",
        detail={"free text": "a traceback with a secret"},
    )  # fmt: skip
    back = bridge.from_engine_observation(engine_value)
    assert back.outcome is Outcome.ERROR and dict(back.detail) == {}


def test_remedies_convert_and_none_stays_none(stand_in: None) -> None:
    assert bridge.to_engine_remedy(None) is None
    remedy = bridge.to_engine_remedy(Remedy("service_down", "restart_service", "service_running"))
    assert isinstance(remedy, _RemedySelection) and remedy.expected_healthy is True
    with pytest.raises(ContractError):
        bridge.to_engine_remedy("restart")  # type: ignore[arg-type]


def test_the_adapter_wrapper_converts_every_answer(stand_in: None) -> None:
    adapter = ComposeHealthAdapter({"web": ComposeService("example", "app")},
                                   socket_path="/nonexistent/docker.sock")  # fmt: skip
    wrapped = bridge.EngineTargetAdapter(adapter)
    look = wrapped.observe("web", "service_running")
    assert isinstance(look, _TargetObservation) and look.reason_code == "daemon_unreachable"
    assert wrapped.target_fingerprint("web") == adapter.target_fingerprint("web")
    assert wrapped.desired_revision("web") == 0
    assert wrapped.select_remedy("web", "service_down") is None
    broken = bridge.to_engine_observation(_observation())
    assert wrapped.condition_class(broken) == "service_down"
    assert wrapped.observation_timeout_seconds == 5.0
    with pytest.raises(ContractError):
        bridge.EngineTargetAdapter(object())  # type: ignore[arg-type]


def test_a_read_only_wiring_allows_nothing(stand_in: None) -> None:
    adapter = ComposeHealthAdapter({"web": ComposeService("example", "app")},
                                   socket_path="/run/docker.sock")  # fmt: skip
    wiring = bridge.read_only_host_wiring(adapter, controller_id=CONTROLLER, policy_revision="r1")
    assert isinstance(wiring, _HostWiring) and isinstance(
        wiring.adapter, bridge.EngineTargetAdapter
    )
    assert wiring.policy.enabled is False
    assert wiring.policy.allowed_action_ids == frozenset() == wiring.policy.allowed_target_ids
    assert isinstance(wiring.registry, _Registry) and wiring.attention_sink is None
    for bad in ({"controller_id": "ctl_short"}, {"policy_revision": "has space"},
                {"attention_sink": object()}):  # fmt: skip
        options: dict[str, Any] = {"controller_id": CONTROLLER, "policy_revision": "r1"}
        options.update(bad)
        with pytest.raises(ContractError):
            bridge.read_only_host_wiring(adapter, **options)


def test_a_broken_engine_is_not_reported_as_missing(
    stand_in: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An install whose own import fails says so, and keeps the cause."""

    broken = types.ModuleType("apexai_ops.runtime.wiring")

    def _fail(name: str) -> Any:
        raise ImportError("a dependency of the engine is missing")

    broken.__getattr__ = _fail  # type: ignore[method-assign]
    monkeypatch.delitem(sys.modules, "apexai_ops.runtime.wiring")
    real_import = bridge.importlib.import_module

    def importer(name: str) -> Any:
        if name == "apexai_ops.runtime.wiring":
            raise ImportError("a dependency of the engine is missing")
        return real_import(name)

    monkeypatch.setattr(bridge.importlib, "import_module", importer)
    assert not bridge.engine_available()
    with pytest.raises(bridge.EngineUnavailable) as unavailable:
        bridge.read_only_host_wiring(
            ComposeHealthAdapter({"web": ComposeService("example", "app")},
                                 socket_path="/run/docker.sock"),
            controller_id=CONTROLLER, policy_revision="r1",
        )  # fmt: skip
    assert "failed to import" in str(unavailable.value)
    assert isinstance(unavailable.value.__cause__, ImportError)


def test_an_outcome_the_contract_does_not_know_is_a_contract_error(stand_in: None) -> None:
    class Strange:
        outcome = "sideways"

    with pytest.raises(ContractError):
        bridge.from_engine_observation(Strange())
