"""The Compose health adapter, against a fake daemon process (offline)."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from apexai_devops_sdk import ContractError, Outcome, Remedy
from apexai_devops_sdk.compose import (
    MAX_RESPONSE_BYTES,
    ComposeHealthAdapter,
    ComposePredicates,
    ComposeService,
)
from apexai_devops_sdk.testing import (
    FakeDockerDaemon,
    assert_target_adapter,
    compose_container,
    http_response,
)

TARGET = "web"
WHERE = ComposeService("example", "app")
CANARY = "canary-secret-value"
H, B, U = Outcome.OBSERVED_HEALTHY, Outcome.OBSERVED_BROKEN, Outcome.UNOBSERVABLE
RUNNING, HEALTHY = "service_running", "service_healthy"


def _clock() -> datetime:
    return datetime(2026, 10, 29, 7, 0, tzinfo=UTC)


def _answer(*args: Any, **kwargs: Any) -> bytes:
    return http_response(200, json.dumps(compose_container(*args, **kwargs)).encode())


@pytest.fixture
def daemon(tmp_path: Path) -> Iterator[FakeDockerDaemon]:
    with FakeDockerDaemon(tmp_path) as served:
        yield served


def _adapter(socket_path: str, **kwargs: Any) -> ComposeHealthAdapter:
    return ComposeHealthAdapter({TARGET: WHERE}, socket_path=socket_path, clock=_clock, **kwargs)


@pytest.mark.parametrize(
    ("status", "health", "running", "healthy"),
    [
        ("running", "healthy", H, H),
        ("running", "unhealthy", H, B),
        ("running", "starting", H, U),
        ("running", None, H, U),
        ("exited", "unhealthy", B, U),
        ("dead", None, B, U),
        ("restarting", "starting", B, U),
        ("created", None, U, U),
        ("paused", "healthy", U, U),
        ("removing", None, U, U),
    ],
)
def test_every_container_state_maps_to_its_outcome(
    daemon: FakeDockerDaemon, status: str, health: str | None, running: Outcome, healthy: Outcome
) -> None:
    adapter = _adapter(daemon.serve(_answer(status, health)))
    for predicate, expected in ((RUNNING, running), (HEALTHY, healthy)):
        observation = adapter.observe(TARGET, predicate)
        assert observation.outcome is expected, (predicate, observation)
        assert observation.source_id == "docker_container_state"
        assert observation.observed_at == "2026-10-29T07:00:00Z"
        if expected is B:
            assert adapter.condition_class(observation) == observation.reason_code
    request = b"GET /containers/example-app-1/json HTTP/1.0\r\nHost: docker\r\n\r\n"
    assert daemon.requests() == request * 2  # one GET per look, nothing else


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (http_response(404, b'{"message": "No such container"}'), "container_missing"),
        (http_response(500, b'{"message": "boom"}'), "daemon_refused"),
        (http_response(200, b"{not json"), "response_malformed"),
        (http_response(200, b"[1, 2]"), "response_malformed"),
        (http_response(200, b"\xff\xfe"), "response_malformed"),
        (b"garbage without a head", "response_malformed"),
        (http_response(200, b"{}", extra_headers=b"Transfer-Encoding: chunked\r\n"),
         "response_malformed"),
        (http_response(200, b" " * MAX_RESPONSE_BYTES), "response_oversized"),
        (_answer(project="other"), "container_mismatch"),
        (_answer(service="db"), "container_mismatch"),
        (_answer("exploded"), "state_unrecognised"),
        (_answer(health="sideways"), "state_unrecognised"),
        (http_response(200, json.dumps({"State": {"Status": "running"}, "Config": {}}).encode()),
         "container_mismatch"),
        (http_response(b"+200", json.dumps(compose_container(project="example",
                                                             service="app")).encode()),
         "response_malformed"),
        (http_response(200, b"{}", extra_headers=b"Content-Length: 500\r\n"), "response_malformed"),
        (http_response(200, b"{}", extra_headers=b"Content-Length: 2\r\nContent-Length: 2\r\n"),
         "response_malformed"),
    ],
    ids=["missing", "refused", "not-json", "not-object", "not-utf8", "no-head", "chunked",
         "oversized", "other-project", "other-service", "unknown-state", "unknown-health",
         "no-labels", "signed-status", "short-body", "two-lengths"],
)  # fmt: skip
def test_an_unreadable_answer_is_unobservable_never_broken(
    daemon: FakeDockerDaemon, answer: bytes, reason: str
) -> None:
    adapter = _adapter(daemon.serve(answer))
    for predicate in (RUNNING, HEALTHY):
        observation = adapter.observe(TARGET, predicate)
        assert (observation.outcome, observation.reason_code) == (U, reason), observation
        assert dict(observation.detail) == {}, "a refused look carries nothing the daemon said"


def test_an_endless_answer_stops_at_the_cap(daemon: FakeDockerDaemon) -> None:
    flood = _adapter(daemon.serve(http_response(200, b" " * 65536), mode="flood"))
    observation = flood.observe(TARGET, RUNNING)
    assert (observation.outcome, observation.reason_code) == (U, "response_oversized")


def test_a_kept_open_answer_ends_at_its_content_length(daemon: FakeDockerDaemon) -> None:
    body = json.dumps(compose_container("running", "unhealthy", project="example",
                                        service="app")).encode()  # fmt: skip
    answer = http_response(200, body, extra_headers=b"Content-Length: %d\r\n" % len(body))
    observation = _adapter(daemon.serve(answer, mode="hold")).observe(TARGET, HEALTHY)
    assert (observation.outcome, observation.reason_code) == (B, "service_unhealthy")


def test_an_absent_or_silent_daemon_is_unobservable_within_the_deadline(
    daemon: FakeDockerDaemon, tmp_path: Path
) -> None:
    missing = _adapter(str(tmp_path / "no-such.sock")).observe(TARGET, RUNNING)
    assert (missing.outcome, missing.reason_code) == (U, "daemon_unreachable")
    adapter = _adapter(daemon.serve(b"", mode="stall"), deadline_seconds=0.5)
    began = time.monotonic()
    silent = adapter.observe(TARGET, RUNNING)
    assert (silent.outcome, silent.reason_code) == (U, "daemon_timeout")
    assert time.monotonic() - began < adapter.observation_timeout_seconds


def test_a_dripping_daemon_is_cut_off_at_the_deadline(daemon: FakeDockerDaemon) -> None:
    answer = _answer(project="example", service="app")
    adapter = _adapter(daemon.serve(answer, mode="drip"), deadline_seconds=0.5)
    began = time.monotonic()
    dripped = adapter.observe(TARGET, RUNNING)
    assert (dripped.outcome, dripped.reason_code) == (U, "daemon_timeout"), dripped
    assert time.monotonic() - began < adapter.observation_timeout_seconds


def test_nothing_the_daemon_returned_is_echoed(daemon: FakeDockerDaemon) -> None:
    adapter = _adapter(daemon.serve(_answer("exited", "unhealthy", project="example",
                                            service="app", secret=CANARY)))  # fmt: skip
    for predicate in (RUNNING, HEALTHY):
        observation = adapter.observe(TARGET, predicate)
        assert CANARY not in repr(observation) and CANARY not in json.dumps(
            dict(observation.detail)
        )


def test_the_host_names_its_predicates_and_conditions(daemon: FakeDockerDaemon) -> None:
    names = ComposePredicates("store_running", "store_health", "store_error", "store_unhealthy")
    adapter = _adapter(daemon.serve(_answer("exited", None, project="example", service="app")),
                       predicates=names)  # fmt: skip
    observation = adapter.observe(TARGET, "store_running")
    assert (observation.outcome, observation.reason_code) == (B, "store_error")
    assert adapter.condition_class(observation) == "store_error"
    assert adapter.observe(TARGET, RUNNING).reason_code == "predicate_unknown"


def test_identity_is_the_service_not_the_container(daemon: FakeDockerDaemon) -> None:
    adapter = _adapter(daemon.serve(_answer(project="example", service="app")))
    fingerprint = adapter.target_fingerprint(TARGET)
    assert adapter.observe(TARGET, RUNNING).target_fingerprint == fingerprint
    other = ComposeHealthAdapter({TARGET: ComposeService("example", "worker")},
                                 socket_path="/run/x.sock")  # fmt: skip
    assert other.target_fingerprint(TARGET) != fingerprint
    assert adapter.desired_revision(TARGET) == 0


def test_remedies_are_offered_only_for_their_own_condition() -> None:
    restart = Remedy("service_down", "restart_service", RUNNING)
    adapter = ComposeHealthAdapter({TARGET: WHERE}, socket_path="/run/x.sock",
                                   remedies={"service_down": restart})  # fmt: skip
    assert adapter.select_remedy(TARGET, "service_down") is restart
    assert adapter.select_remedy(TARGET, "service_unhealthy") is None
    assert adapter.select_remedy("other", "service_down") is None
    with pytest.raises(ContractError):
        ComposeHealthAdapter({TARGET: WHERE}, socket_path="/run/x.sock",
                             remedies={"service_unhealthy": restart})  # fmt: skip


@pytest.mark.parametrize(
    "kwargs",
    [
        {"targets": {}},
        {"targets": {"bad id": WHERE}},
        {"targets": {TARGET: ("example", "app")}},
        {"socket_path": "relative.sock"},
        {"deadline_seconds": 5.0},
        {"deadline_seconds": 0},
        {"source_id": "bad source"},
    ],
    ids=["no-targets", "bad-target", "not-a-service", "relative-socket", "deadline-too-long",
         "no-deadline", "bad-source"],
)  # fmt: skip
def test_a_bad_configuration_is_refused(kwargs: dict[str, Any]) -> None:
    options: dict[str, Any] = {"targets": {TARGET: WHERE}, "socket_path": "/run/x.sock"}
    options.update(kwargs)
    targets = options.pop("targets")
    with pytest.raises(ContractError):
        ComposeHealthAdapter(targets, **options)


def test_compose_names_are_checked() -> None:
    with pytest.raises(ContractError):
        ComposeService("Bad Project", "app")
    with pytest.raises(ContractError):
        ComposeService("example", "app", index=0)
    with pytest.raises(ContractError):
        ComposePredicates(down_condition="Service Down")
    assert ComposeService("example", "app", 2).container == "example-app-2"


def test_the_adapter_meets_the_contract(daemon: FakeDockerDaemon) -> None:
    adapter = _adapter(daemon.serve(_answer("running", "unhealthy", project="example",
                                            service="app")))  # fmt: skip
    assert_target_adapter(adapter, targets=[TARGET], predicates=[RUNNING, HEALTHY])


def test_the_two_condition_classes_must_differ() -> None:
    with pytest.raises(ContractError):
        ComposePredicates(down_condition="service_bad", unhealthy_condition="service_bad")
