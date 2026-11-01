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

from apexai_devops_sdk import ContractError, Observation, Outcome, Remedy
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
