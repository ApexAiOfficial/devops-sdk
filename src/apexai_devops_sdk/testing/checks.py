"""Contract checks a host runs against its own adapter, in its own test suite.

:func:`check_target_adapter` exercises a :class:`~apexai_devops_sdk.contract.TargetAdapter`
over the targets and predicates the host declares and returns every
violation it finds (an empty list passes); :func:`assert_target_adapter`
raises one ``AssertionError`` naming them all. They judge the contract only,
never the host's semantics: whether a given state *should* be broken is the
host's own test to write.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

from apexai_devops_sdk.contract import (
    Observation,
    Outcome,
    Remedy,
    TargetAdapter,
    is_closed_word,
    is_identifier,
)

UNKNOWN_TARGET = "contract-check-unknown-target"
UNKNOWN_PREDICATE = "contract-check-unknown-predicate"


def check_target_adapter(
    adapter: object, *, targets: Iterable[str], predicates: Iterable[str]
) -> list[str]:
    """Every contract violation of ``adapter`` over every pair of ``targets`` and ``predicates``."""

    if not isinstance(adapter, TargetAdapter):
        return ["the adapter does not implement TargetAdapter"]
    problems: list[str] = []
    timeout = adapter.observation_timeout_seconds
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        problems.append("observation_timeout_seconds is not a positive, finite number")
    listed_targets, listed_predicates = tuple(targets), tuple(predicates)
    if not listed_targets or not listed_predicates:
        problems.append("anti-vacuity: no targets or no predicates to check")
    for target in listed_targets:
        problems += _target(adapter, target)
        for predicate in listed_predicates:
            problems += _look(adapter, target, predicate, known=True)
    probe_target = listed_targets[0] if listed_targets else UNKNOWN_TARGET
    problems += _look(adapter, UNKNOWN_TARGET, listed_predicates[0] if listed_predicates
                      else UNKNOWN_PREDICATE, known=False)  # fmt: skip
    problems += _look(adapter, probe_target, UNKNOWN_PREDICATE, known=False)
    return problems


def assert_target_adapter(
    adapter: object, *, targets: Iterable[str], predicates: Iterable[str]
) -> None:
    """Raise ``AssertionError`` listing every violation, or return."""

    problems = check_target_adapter(adapter, targets=targets, predicates=predicates)
    if problems:
        raise AssertionError("target adapter contract: " + "; ".join(problems))


def _target(adapter: TargetAdapter, target: str) -> list[str]:
    problems: list[str] = []
    try:
        first, second = adapter.target_fingerprint(target), adapter.target_fingerprint(target)
    except Exception as failed:  # noqa: BLE001 - a raise is itself the violation
        return [f"{target}: target_fingerprint raised {type(failed).__name__}"]
    if not is_identifier(first):
        problems.append(f"{target}: target_fingerprint is not an identifier")
    elif first != second:
        problems.append(f"{target}: target_fingerprint is not stable across two calls")
    try:
        revision = adapter.desired_revision(target)
    except Exception as failed:  # noqa: BLE001
        return [*problems, f"{target}: desired_revision raised {type(failed).__name__}"]
    if type(revision) is not int or revision < 0:
        problems.append(f"{target}: desired_revision is not a non-negative integer")
    return problems


def _look(adapter: TargetAdapter, target: str, predicate: str, *, known: bool) -> list[str]:
    where = f"{target}/{predicate}"
    try:
        observation = adapter.observe(target, predicate)
    except Exception as failed:  # noqa: BLE001
        return [f"{where}: observe raised {type(failed).__name__} (answer unobservable instead)"]
    if not isinstance(observation, Observation):
        return [f"{where}: observe did not return an Observation"]
    problems: list[str] = []
    if (observation.target_id, observation.predicate_id) != (target, predicate):
        problems.append(f"{where}: the observation names another target or predicate")
    if not known and observation.conclusive:
        problems.append(f"{where}: an unknown target or predicate was observed conclusively")
    if observation.outcome is Outcome.OBSERVED_BROKEN:
        try:
            condition = adapter.condition_class(observation)
        except Exception as failed:  # noqa: BLE001
            return [*problems, f"{where}: condition_class raised {type(failed).__name__}"]
        if not is_identifier(condition):
            problems.append(f"{where}: condition_class is not an identifier")
            return problems
        try:
            remedy = adapter.select_remedy(target, condition)
        except Exception as failed:  # noqa: BLE001
            return [*problems, f"{where}: select_remedy raised {type(failed).__name__}"]
        if remedy is not None and (
            not isinstance(remedy, Remedy) or remedy.condition_class != condition
        ):
            problems.append(f"{where}: select_remedy returned a remedy for another condition")
    if observation.reason_code and not is_closed_word(observation.reason_code):
        problems.append(f"{where}: reason_code is not a closed word")
    return problems
