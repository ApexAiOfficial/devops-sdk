"""The contract checks find the violations a host adapter can commit."""

from __future__ import annotations

from typing import Any

import pytest

from apexai_devops_sdk import Observation, Outcome, Remedy, utc_timestamp
from apexai_devops_sdk.testing import assert_target_adapter, check_target_adapter


class _Adapter:
    """A minimal, correct adapter; each test breaks one thing."""

    observation_timeout_seconds = 2.0
    targets = ("web",)

    def observe(self, target_id: str, predicate_id: str) -> Observation:
        known = target_id in self.targets and predicate_id == "up"
        return Observation(
            target_id=target_id, predicate_id=predicate_id,
            outcome=Outcome.OBSERVED_BROKEN if known else Outcome.UNOBSERVABLE,
            observed_at=utc_timestamp(), source_id="example",
            reason_code="down" if known else "unknown",
        )  # fmt: skip

    def target_fingerprint(self, target_id: str) -> str:
        return "fp-" + target_id

    def desired_revision(self, target_id: str) -> int:
        return 0

    def select_remedy(self, target_id: str, condition_class: str) -> Remedy | None:
        return Remedy(condition_class, "restart", "up")

    def condition_class(self, observation: Observation) -> str:
        return "down"


def test_a_correct_adapter_passes() -> None:
    assert check_target_adapter(_Adapter(), targets=["web"], predicates=["up"]) == []
    assert_target_adapter(_Adapter(), targets=["web"], predicates=["up"])


def _broken(**members: Any) -> object:
    adapter = _Adapter()
    for name, value in members.items():
        setattr(adapter, name, value)
    return adapter


def _raises(*_args: Any) -> Any:
    raise RuntimeError("boom")


@pytest.mark.parametrize(
    ("adapter", "found"),
    [
        (object(), "does not implement TargetAdapter"),
        (_broken(observation_timeout_seconds=0), "observation_timeout_seconds"),
        (_broken(observe=_raises), "observe raised RuntimeError"),
        (_broken(observe=lambda t, p: "healthy"), "did not return an Observation"),
        (_broken(target_fingerprint=lambda t: "has space"), "not an identifier"),
        (_broken(desired_revision=lambda t: -1), "desired_revision"),
        (_broken(condition_class=_raises), "condition_class raised"),
        (_broken(select_remedy=lambda t, c: Remedy("other", "restart", "up")),
         "remedy for another condition"),
        (_broken(observe=lambda t, p: Observation(
            target_id=t, predicate_id=p, outcome=Outcome.OBSERVED_HEALTHY,
            observed_at=utc_timestamp(), source_id="example")),
         "unknown target or predicate was observed conclusively"),
    ],
    ids=["not-an-adapter", "timeout", "observe-raises", "not-an-observation", "fingerprint",
         "revision", "condition-raises", "wrong-remedy", "guesses-the-unknown"],
)  # fmt: skip
def test_each_violation_is_found(adapter: object, found: str) -> None:
    problems = check_target_adapter(adapter, targets=["web"], predicates=["up"])
    assert any(found in problem for problem in problems), problems
    with pytest.raises(AssertionError):
        assert_target_adapter(adapter, targets=["web"], predicates=["up"])


def test_nothing_to_check_is_itself_a_violation() -> None:
    assert check_target_adapter(_Adapter(), targets=[], predicates=[])


class _SingleUse(_Adapter):
    def __init__(self) -> None:
        self.used = False

    def observe(self, target_id: str, predicate_id: str) -> Observation:
        if self.used:
            raise ConnectionError("the handle was closed after the first look")
        self.used = True
        return super().observe(target_id, predicate_id)


class _Slow(_Adapter):
    observation_timeout_seconds = 0.05

    def observe(self, target_id: str, predicate_id: str) -> Observation:
        import time

        time.sleep(0.1)
        return super().observe(target_id, predicate_id)


class _ForkHostile(_Adapter):
    def __init__(self) -> None:
        import os

        self.parent = os.getpid()

    def observe(self, target_id: str, predicate_id: str) -> Observation:
        import os

        if os.getpid() != self.parent:
            raise RuntimeError("not fork-safe")
        return super().observe(target_id, predicate_id)


class _NeverBroken(_Adapter):
    """Never observes broken, so only the synthetic look reaches condition_class."""

    def observe(self, target_id: str, predicate_id: str) -> Observation:
        return Observation(
            target_id=target_id, predicate_id=predicate_id, outcome=Outcome.UNOBSERVABLE,
            observed_at=utc_timestamp(), source_id="example",
        )  # fmt: skip

    def condition_class(self, observation: Observation) -> str:
        return "not an identifier"


class _RaisingTimeout(_Adapter):
    @property
    def observation_timeout_seconds(self) -> float:  # type: ignore[override]
        raise OSError("the config file is gone")


@pytest.mark.parametrize(
    ("adapter", "found"),
    [
        (_SingleUse(), "observe raised ConnectionError"),
        (_Slow(), "longer than observation_timeout_seconds"),
        (_ForkHostile(), "(in a forked child): observe raised"),
        (_NeverBroken(), "a synthetic broken look"),
        (_RaisingTimeout(), "observation_timeout_seconds raised OSError"),
    ],
    ids=["single-use", "slow", "not-fork-safe", "condition-never-seen", "timeout-raises"],
)
def test_each_harder_violation_is_found(adapter: object, found: str) -> None:
    problems = check_target_adapter(adapter, targets=["web"], predicates=["up"])
    assert any(found in problem for problem in problems), problems


def test_the_fork_check_can_be_skipped() -> None:
    assert (
        check_target_adapter(_ForkHostile(), targets=["web"], predicates=["up"], forked=False) == []
    )
