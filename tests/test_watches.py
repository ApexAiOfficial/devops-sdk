"""Declared watches: the engine-neutral value, and the wiring's own checks.

Everything here runs without the engine: the wiring refuses a malformed set
before it needs any part of it. ``test_engine_bridge.py`` drives the declared
watches through a stand-in, and ``test_engine_live.py`` through the real engine.
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
        ("web", "store_health", 10**400),
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


@pytest.mark.parametrize(
    "kwargs",
    [
        {"watches": (WATCHES[0], WATCHES[0])},
        {"watches": (("web", "store_health", 60),)},
        {"watches": 5},
        {"watches": WATCHES, "max_unconfirmed_retirements": -1},
        {"watches": WATCHES, "max_unconfirmed_retirements": True},
    ],
)
def test_a_malformed_watch_set_is_refused_before_the_engine_is_needed(
    kwargs: dict[str, Any],
) -> None:
    from apexai_devops_sdk import engine as bridge

    with pytest.raises(ContractError):
        bridge.read_only_host_wiring(
            _Adapter(), controller_id=CONTROLLER, policy_revision="r1", **kwargs
        )


def test_each_monitor_has_its_own_stable_source() -> None:
    from apexai_devops_sdk.engine import watch_source_id

    source = watch_source_id(CONTROLLER)
    assert source == watch_source_id(CONTROLLER) != watch_source_id("ctl_" + "3" * 32)
    assert source.startswith("sdk-") and len(source) <= 32 and source == source.lower()
    with pytest.raises(ContractError):
        watch_source_id("ctl_short")
