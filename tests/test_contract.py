"""The host contract's values refuse what the engine could not carry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from apexai_devops_sdk import (
    CONTRACT_VERSION,
    AttentionNotice,
    AttentionSink,
    ContractError,
    Observation,
    Outcome,
    Remedy,
    TargetAdapter,
    utc_timestamp,
)

NOTICE: dict[str, Any] = {
    "notification_id": "ntf_" + "a" * 32,
    "work_id": "wrk_" + "1" * 32,
    "attention_kind": "rearmable_blocker",
    "incident_id": "inc_" + "2" * 32,
    "desired_revision": 3,
    "target_id": "example-target",
    "decision_state": "blocked",
    "reason_code": "remedy_unresolvable",
    "recovery_kind": None,
    "attempt": 1,
    "settled_at": "2026-10-29T07:00:00Z",
}


def _observation(**changes: Any) -> Observation:
    values: dict[str, Any] = {
        "target_id": "example-target",
        "predicate_id": "service_running",
        "outcome": Outcome.OBSERVED_HEALTHY,
        "observed_at": "2026-10-29T07:00:00Z",
        "source_id": "example_source",
        "reason_code": "running",
        "detail": {"state": "running"},
    }
    values.update(changes)
    return Observation(**values)


def test_the_contract_version_is_one() -> None:
    assert CONTRACT_VERSION == "1"


def test_a_well_formed_observation_is_accepted() -> None:
    observation = _observation()
    assert observation.conclusive and observation.outcome.value == "observed_healthy"
    assert not _observation(outcome=Outcome.UNOBSERVABLE).conclusive


@pytest.mark.parametrize(
    "changes",
    [
        {"target_id": "has space"},
        {"target_id": ""},
        {"predicate_id": "x" * 200},
        {"outcome": "observed_broken"},
        {"observed_at": "2026-10-29 07:00:00"},
        {"observed_at": "2026-10-29T07:00:00+00:00"},
        {"source_id": "bad/source"},
        {"reason_code": "Contains Free Text"},
        {"target_fingerprint": "has space"},
        {"duration_ms": -1},
        {"duration_ms": 1.5},
        {"detail": {"nested": {"a": 1}}},
        {"detail": {"bad key": "x"}},
        {"detail": {"nan": float("nan")}},
        {"detail": {"big": "x" * 5000}},
    ],
    ids=lambda changes: next(iter(changes)),
)
def test_an_observation_outside_the_contract_is_refused(changes: dict[str, Any]) -> None:
    with pytest.raises(ContractError) as refused:
        _observation(**changes)
    for value in changes.values():  # the message names the field, never the value
        if isinstance(value, str) and len(value) > 3:
            assert value not in str(refused.value)


def test_utc_timestamp_is_the_contract_s_shape() -> None:
    moment = datetime(2026, 10, 29, 3, 0, tzinfo=timezone(timedelta(hours=-4)))
    assert utc_timestamp(moment) == "2026-10-29T07:00:00Z"
    assert utc_timestamp().endswith("Z")
    with pytest.raises(ContractError):
        utc_timestamp(datetime(2026, 10, 29))  # naive


def test_a_remedy_is_identifiers_and_a_bool() -> None:
    remedy = Remedy("service_down", "restart_service", "service_running")
    assert remedy.expected_healthy is True
    with pytest.raises(ContractError):
        Remedy("service_down", "restart service", "service_running")
    with pytest.raises(ContractError):
        Remedy("service_down", "restart_service", "service_running", expected_healthy=1)  # type: ignore[arg-type]


class _SafeRecord:
    def safe_record(self) -> dict[str, Any]:
        return dict(NOTICE)


class _Attributes:
    def __init__(self) -> None:
        for name, value in NOTICE.items():
            setattr(self, name, value)


@pytest.mark.parametrize(
    "payload", [NOTICE, _SafeRecord(), _Attributes(), AttentionNotice(**NOTICE)],
    ids=["mapping", "safe-record", "attributes", "notice"],
)  # fmt: skip
def test_a_notice_is_read_from_any_payload_shape(payload: object) -> None:
    notice = AttentionNotice.from_payload(payload)
    assert notice.record() == NOTICE


@pytest.mark.parametrize(
    "changes",
    [
        {"notification_id": "ntf_XYZ"},
        {"work_id": None},
        {"attention_kind": "Free Text"},
        {"reason_code": "has space"},
        {"attempt": -1},
        {"desired_revision": "3"},
        {"settled_at": "yesterday"},
    ],
    ids=lambda changes: next(iter(changes)),
)
def test_a_notice_outside_the_contract_is_refused(changes: dict[str, Any]) -> None:
    with pytest.raises(ContractError):
        AttentionNotice.from_payload({**NOTICE, **changes})


def test_the_protocols_are_structural() -> None:
    class Sink:
        def offer(self, payload: Any) -> str:
            return "accepted"

    assert isinstance(Sink(), AttentionSink)
    assert not isinstance(object(), TargetAdapter)


def test_timestamps_with_microseconds_are_accepted() -> None:
    moment = datetime(2026, 10, 29, 7, 0, 0, 123456, tzinfo=UTC)
    assert _observation(observed_at=utc_timestamp(moment)).observed_at.endswith(".123456Z")


def test_the_package_version_is_the_project_s() -> None:
    import tomllib
    from pathlib import Path

    import apexai_devops_sdk

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    assert apexai_devops_sdk.__version__ == declared
