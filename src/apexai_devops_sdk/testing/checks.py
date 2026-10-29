"""Contract checks a host runs against its own adapter, in its own test suite.

:func:`check_target_adapter` exercises a :class:`~apexai_devops_sdk.contract.TargetAdapter`
over the targets and predicates the host declares and returns every
violation it finds (an empty list passes); :func:`assert_target_adapter`
raises one ``AssertionError`` naming them all. They judge the contract only,
never the host's semantics: whether a given state *should* be broken is the
host's own test to write.

What is checked, for every declared target and predicate:

- ``observe`` answers twice in a row (an adapter that keeps a connection or
  a handle open between calls fails the second), each time with an
  :class:`~apexai_devops_sdk.contract.Observation` naming the same target and
  predicate, within ``observation_timeout_seconds``;
- ``observe`` also answers in a forked child, as the engine calls it
  (``forked=False`` skips this, for example where forking is unsupported);
- ``target_fingerprint`` is a stable identifier and ``desired_revision`` a
  non-negative integer;
- ``condition_class`` names a broken observation, and ``select_remedy``
  offers nothing or a remedy for that same condition, for a synthetic broken
  observation as well as for any the adapter produced itself;
- an unknown target and an unknown predicate are never observed
  conclusively.
"""

from __future__ import annotations

import contextlib
import math
import os
import select
import time
from collections.abc import Iterable

from apexai_devops_sdk.contract import (
    Observation,
    Outcome,
    Remedy,
    TargetAdapter,
    is_closed_word,
    is_identifier,
    utc_timestamp,
)

UNKNOWN_TARGET = "contract-check-unknown-target"
UNKNOWN_PREDICATE = "contract-check-unknown-predicate"
_FORK_GRACE_SECONDS = 1.0


def check_target_adapter(
    adapter: object,
    *,
    targets: Iterable[str],
    predicates: Iterable[str],
    forked: bool = True,
) -> list[str]:
    """Every contract violation of ``adapter`` over every pair of ``targets`` and ``predicates``."""

    if not isinstance(adapter, TargetAdapter):
        return ["the adapter does not implement TargetAdapter"]
    problems: list[str] = []
    try:
        timeout = adapter.observation_timeout_seconds
    except Exception as failed:  # noqa: BLE001 - a raise is itself the violation
        return [f"observation_timeout_seconds raised {type(failed).__name__}"]
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        problems.append("observation_timeout_seconds is not a positive, finite number")
        timeout = math.inf
    listed_targets, listed_predicates = tuple(targets), tuple(predicates)
    if not listed_targets or not listed_predicates:
        return [*problems, "anti-vacuity: no targets or no predicates to check"]
    for target in listed_targets:
        problems += _target(adapter, target)
        for predicate in listed_predicates:
            for _call in range(2):  # a single-use adapter fails the second call
                problems += _look(adapter, target, predicate, known=True, timeout=timeout)
            problems += _synthetic_broken(adapter, target, predicate)
    problems += _look(adapter, UNKNOWN_TARGET, listed_predicates[0], known=False, timeout=timeout)
    problems += _look(adapter, listed_targets[0], UNKNOWN_PREDICATE, known=False, timeout=timeout)
    if forked and hasattr(os, "fork"):
        problems += _forked_look(adapter, listed_targets[0], listed_predicates[0], timeout)
    return sorted(set(problems), key=problems.index)


def assert_target_adapter(
    adapter: object,
    *,
    targets: Iterable[str],
    predicates: Iterable[str],
    forked: bool = True,
) -> None:
    """Raise ``AssertionError`` listing every violation, or return."""

    problems = check_target_adapter(adapter, targets=targets, predicates=predicates, forked=forked)
    if problems:
        raise AssertionError("target adapter contract: " + "; ".join(problems))


def _target(adapter: TargetAdapter, target: str) -> list[str]:
    problems: list[str] = []
    try:
        first, second = adapter.target_fingerprint(target), adapter.target_fingerprint(target)
    except Exception as failed:  # noqa: BLE001
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


def _look(
    adapter: TargetAdapter, target: str, predicate: str, *, known: bool, timeout: float
) -> list[str]:
    where = f"{target}/{predicate}"
    began = time.monotonic()
    try:
        observation = adapter.observe(target, predicate)
    except Exception as failed:  # noqa: BLE001
        return [f"{where}: observe raised {type(failed).__name__} (answer unobservable instead)"]
    spent = time.monotonic() - began
    if not isinstance(observation, Observation):
        return [f"{where}: observe did not return an Observation"]
    problems: list[str] = []
    if spent > timeout:
        problems.append(f"{where}: observe took longer than observation_timeout_seconds")
    if (observation.target_id, observation.predicate_id) != (target, predicate):
        problems.append(f"{where}: the observation names another target or predicate")
    if not known and observation.conclusive:
        problems.append(f"{where}: an unknown target or predicate was observed conclusively")
    if observation.reason_code and not is_closed_word(observation.reason_code):
        problems.append(f"{where}: reason_code is not a closed word")
    if observation.outcome is Outcome.OBSERVED_BROKEN:
        problems += _classify(adapter, target, observation, where)
    return problems


def _synthetic_broken(adapter: TargetAdapter, target: str, predicate: str) -> list[str]:
    """``condition_class`` and ``select_remedy`` on a broken look the adapter did not make."""

    broken = Observation(
        target_id=target,
        predicate_id=predicate,
        outcome=Outcome.OBSERVED_BROKEN,
        observed_at=utc_timestamp(),
        source_id="contract-check",
    )
    return _classify(adapter, target, broken, f"{target}/{predicate} (a synthetic broken look)")


def _classify(
    adapter: TargetAdapter, target: str, observation: Observation, where: str
) -> list[str]:
    try:
        condition = adapter.condition_class(observation)
    except Exception as failed:  # noqa: BLE001
        return [f"{where}: condition_class raised {type(failed).__name__}"]
    if not is_identifier(condition):
        return [f"{where}: condition_class is not an identifier"]
    try:
        remedy = adapter.select_remedy(target, condition)
    except Exception as failed:  # noqa: BLE001
        return [f"{where}: select_remedy raised {type(failed).__name__}"]
    if remedy is not None and (
        not isinstance(remedy, Remedy) or remedy.condition_class != condition
    ):
        return [f"{where}: select_remedy returned a remedy for another condition"]
    return []


def _forked_look(adapter: TargetAdapter, target: str, predicate: str, timeout: float) -> list[str]:
    """``observe`` in a forked child, as the engine runs it: the child reports one word."""

    where = f"{target}/{predicate} (in a forked child)"
    reader, writer = os.pipe()
    pid = os.fork()
    if pid == 0:  # the child: never returns into the test runner
        code = 1
        try:
            os.close(reader)
            observation = adapter.observe(target, predicate)
            word = b"ok" if isinstance(observation, Observation) else b"not-an-observation"
            os.write(writer, word)
            code = 0
        except BaseException:  # noqa: BLE001 - anything in the child is reported as raised
            with contextlib.suppress(OSError):
                os.write(writer, b"raised")
        finally:
            os._exit(code)
    os.close(writer)
    answer = b""
    try:
        wait = (timeout if math.isfinite(timeout) else 10.0) + _FORK_GRACE_SECONDS
        ready, _, _ = select.select([reader], [], [], wait)
        answer = os.read(reader, 64) if ready else b""
    finally:
        os.close(reader)
        if not answer:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, 9)
        os.waitpid(pid, 0)
    if not answer:
        return [f"{where}: observe did not answer within its timeout"]
    if answer != b"ok":
        return [f"{where}: observe {answer.decode()}"]
    return []
