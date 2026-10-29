"""The bridge against the real engine, where its licensed package is installed.

Skipped elsewhere (this repository's CI does not have the engine). A host
that deploys with the engine runs this file in its own environment to prove
the SDK and the engine it pins agree.
"""

from __future__ import annotations

import pytest

pytest.importorskip("apexai_ops.runtime.wiring", reason="the DevOps engine is not installed")

from apexai_devops_sdk import Outcome
from apexai_devops_sdk import engine as bridge
from apexai_devops_sdk.compose import ComposeHealthAdapter, ComposeService

CONTROLLER = "ctl_" + "1" * 32


def test_the_engine_accepts_the_wiring_and_the_converted_values() -> None:
    from apexai_ops.control.observation import ObservationOutcome, TargetObservation

    adapter = ComposeHealthAdapter({"web": ComposeService("example", "app")},
                                   socket_path="/nonexistent/docker.sock")  # fmt: skip
    wiring = bridge.read_only_host_wiring(adapter, controller_id=CONTROLLER, policy_revision="r1")
    look = wiring.adapter.observe("web", "service_running")
    assert type(look) is TargetObservation
    assert look.outcome is ObservationOutcome.UNOBSERVABLE
    assert bridge.from_engine_observation(look).outcome is Outcome.UNOBSERVABLE
