"""Declared watches: the engine-neutral value, and the wiring the engine reconciles at start.

The value is checked everywhere. The wiring half runs only where the engine
is installed, as ``test_engine_live.py`` does.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from apexai_devops_sdk import ContractError, Watch


@pytest.mark.parametrize(
    ("target", "predicate", "interval"),
    [
        ("", "store_health", 60),
        ("web 1", "store_health", 60),
        ("web", "", 60),
        ("web", "store_health", 0.5),
        ("web", "store_health", 86401),
        ("web", "store_health", math.inf),
        ("web", "store_health", math.nan),
        ("web", "store_health", True),
        ("web", "store_health", "60"),
    ],
)
def test_a_malformed_watch_is_refused(target: str, predicate: str, interval: Any) -> None:
    with pytest.raises(ContractError):
        Watch(target, predicate, interval)


def test_a_watch_interval_is_a_float() -> None:
    watch = Watch("web", "store_health", 60)
    assert watch.interval_seconds == 60.0 and type(watch.interval_seconds) is float


class _Adapter:
    observation_timeout_seconds = 2.0

    def observe(self, target_id: str, predicate_id: str) -> Any:  # pragma: no cover
        raise AssertionError("not called")

    def target_fingerprint(self, target_id: str) -> str:  # pragma: no cover
        return "fp"

    def desired_revision(self, target_id: str) -> int:  # pragma: no cover
        return 0

    def select_remedy(self, target_id: str, condition_class: str) -> None:  # pragma: no cover
        return None

    def condition_class(self, observation: Any) -> str:  # pragma: no cover
        return "broken"


CONTROLLER = "ctl_" + "2" * 32
WATCHES = (Watch("web", "store_health", 60), Watch("api", "store_running", 30))


def _wiring(**kwargs: Any) -> Any:
    pytest.importorskip("apexai_ops.control.inventory", reason="engine without watch inventory")
    from apexai_devops_sdk import engine as bridge

    return bridge.read_only_host_wiring(
        _Adapter(), controller_id=CONTROLLER, policy_revision="r1", **kwargs
    )


def test_without_watches_the_wiring_declares_nothing() -> None:
    wiring = _wiring()
    assert wiring.watch_inventory is None and wiring.watch_authorizer is None


def test_the_inventory_is_one_complete_snapshot_with_a_fresh_token() -> None:
    wiring = _wiring(watches=WATCHES)
    from apexai_ops.control.supervision import inventory_set_digest

    first, second = wiring.watch_inventory.snapshot(), wiring.watch_inventory.snapshot()
    items = (("api", "store_running", 30.0), ("web", "store_health", 60.0))
    assert first.declared_watches == items and first.set_digest == inventory_set_digest(items)
    assert (first.page_count, first.pages_received) == (1, 1)
    assert first.source_id == second.source_id == "sdk-declared"
    assert first.complete_snapshot_token != second.complete_snapshot_token
    # The engine screens both as durable identifiers before it writes anything.
    from apexai_ops.control.rearm import is_safe_attribution_id

    assert is_safe_attribution_id(first.source_id), first.source_id
    assert is_safe_attribution_id(first.complete_snapshot_token), first.complete_snapshot_token
    assert wiring.inventory_retirement_bound == 1


def test_the_authorizer_allows_only_the_monitor_its_own_set() -> None:
    authorize = _wiring(watches=WATCHES).watch_authorizer.authorize_watch
    from apexai_ops.control.rearm import WatchAct

    def allowed(actor: str, target: str, predicate: str, act: WatchAct) -> bool:
        decision = authorize(
            actor_id=actor, target_id=target, detection_predicate_id=predicate, act=act
        )
        assert decision.authorizer_id == "sdk-declared-watches"
        return bool(decision.authorized)

    assert allowed(CONTROLLER, "web", "store_health", WatchAct.DECLARE)
    assert not allowed("ctl_" + "3" * 32, "web", "store_health", WatchAct.DECLARE)
    assert not allowed(CONTROLLER, "web", "store_running", WatchAct.DECLARE)
    assert allowed(CONTROLLER, "gone", "store_health", WatchAct.RETIRE)
    assert not allowed(CONTROLLER, "web", "store_health", WatchAct.RETIRE)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"watches": (WATCHES[0], WATCHES[0])},
        {"watches": (("web", "store_health", 60),)},
        {"watches": WATCHES, "max_unconfirmed_retirements": -1},
        {"watches": WATCHES, "max_unconfirmed_retirements": True},
    ],
)
def test_a_malformed_watch_set_is_refused(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ContractError):
        _wiring(**kwargs)
