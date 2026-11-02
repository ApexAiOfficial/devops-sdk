"""The bridge against the real engine, where it is present.

The engine runs only in environments ApexAi operates, so this file is skipped
everywhere else, including this repository's CI. Where the engine is present,
it proves that the SDK and that engine version agree: a conclusive look
converts both ways, the engine accepts the wiring, and the sink takes the
engine's own payload.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("apexai_ops.runtime.wiring", reason="the DevOps engine is not installed")

from apexai_devops_sdk import Outcome, Watch
from apexai_devops_sdk import engine as bridge
from apexai_devops_sdk.compose import ComposeHealthAdapter, ComposeService
from apexai_devops_sdk.telegram import TelegramAttentionSink
from apexai_devops_sdk.testing import (
    FakeDockerDaemon,
    FakeTelegramApi,
    compose_container,
    http_response,
)

CONTROLLER = "ctl_" + "1" * 32


def test_a_broken_look_converts_and_is_classified_through_the_engine(tmp_path: Path) -> None:
    from apexai_ops.control.observation import ObservationOutcome, TargetObservation

    with FakeDockerDaemon(tmp_path) as daemon:
        document = compose_container("exited", None, project="example", service="app")
        socket_path = daemon.serve(http_response(200, json.dumps(document).encode()))
        adapter = ComposeHealthAdapter({"web": ComposeService("example", "app")},
                                       socket_path=socket_path)  # fmt: skip
        wiring = bridge.read_only_host_wiring(
            adapter, controller_id=CONTROLLER, policy_revision="r1"
        )
        look = wiring.adapter.observe("web", "service_running")
    assert type(look) is TargetObservation
    assert look.outcome is ObservationOutcome.OBSERVED_BROKEN
    assert look.target_fingerprint == adapter.target_fingerprint("web")
    assert wiring.adapter.condition_class(look) == "service_down"
    assert bridge.from_engine_observation(look).outcome is Outcome.OBSERVED_BROKEN


def test_the_sink_takes_the_engine_s_own_payload(tmp_path: Path) -> None:
    from apexai_ops.control.attention import AttentionPayload, AttentionSink, notification_id

    state = tmp_path / "delivered"
    state.mkdir(mode=0o700)
    token = tmp_path / "bot.env"
    token.write_text("TELEGRAM_BOT_TOKEN=123456789:" + "A" * 35 + "\n", encoding="utf-8")
    token.chmod(0o600)
    work = "wrk_" + "3" * 32
    payload = AttentionPayload(
        notification_id=notification_id(work), work_id=work, attention_kind="rearmable_blocker",
        incident_id="inc_" + "4" * 32, desired_revision=1, target_id="web",
        decision_state="blocked", reason_code="remedy_unresolvable", recovery_kind=None,
        attempt=1, settled_at="2026-10-29T07:00:00Z",
    )  # fmt: skip
    with FakeTelegramApi(tmp_path) as api:
        sink = TelegramAttentionSink(token_file=token, chat_ids=[100000001], state_dir=state,
                                     api_base=api.base_url)  # fmt: skip
        assert isinstance(sink, AttentionSink)
        assert sink.offer(payload) == "accepted"
        assert len(api.requests()) == 1
    wiring = bridge.read_only_host_wiring(
        ComposeHealthAdapter({"web": ComposeService("example", "app")},
                             socket_path="/nonexistent/docker.sock"),
        controller_id=CONTROLLER, policy_revision="r1", attention_sink=sink,
    )  # fmt: skip
    assert wiring.attention_sink is sink


def test_the_engine_takes_the_declared_watches() -> None:
    """The real engine's types and rules agree with what the bridge builds for declared watches."""

    from apexai_ops.control.rearm import WatchAct, is_safe_attribution_id
    from apexai_ops.control.supervision import inventory_set_digest

    assert bridge.engine_available(watches=True)
    watches = (Watch("web", "service_running", 60), Watch("api", "service_healthy", 30))
    wiring = bridge.read_only_host_wiring(
        ComposeHealthAdapter({"web": ComposeService("example", "app")}),
        controller_id=CONTROLLER,
        policy_revision="r1",
        watches=watches,
    )
    snapshot = wiring.watch_inventory.snapshot()
    assert snapshot.set_digest == inventory_set_digest(snapshot.declared_watches)
    assert is_safe_attribution_id(snapshot.source_id), snapshot.source_id
    assert is_safe_attribution_id(snapshot.complete_snapshot_token)
    decide = wiring.watch_authorizer.authorize_watch
    assert decide(
        actor_id=CONTROLLER, target_id="web", detection_predicate_id="service_running",
        act=WatchAct.DECLARE,
    ).authorized  # fmt: skip
    assert not decide(
        actor_id=CONTROLLER, target_id="web", detection_predicate_id="service_running",
        act=WatchAct.RETIRE,
    ).authorized  # fmt: skip
    empty = bridge.read_only_host_wiring(
        ComposeHealthAdapter({"web": ComposeService("example", "app")}),
        controller_id=CONTROLLER,
        policy_revision="r1",
        watches=(),
    )
    assert empty.watch_inventory.snapshot().declared_watches == ()
