"""The four operator-act authorizers: the contract's decision, and the bridge's pass-through.

Engine-free, against the stand-in of ``test_engine_bridge.py``; ``test_engine_live.py`` checks the
real engine's slots where it is present.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any

import pytest
from test_engine_bridge import (  # noqa: F401 - the stand-in's fixtures, used by name
    CONTROLLER,
    _HostWiring,
    _RearmDecision,
    stand_in,
    watch_stand_in,
)

from apexai_devops_sdk import ContractError, OperatorDecision
from apexai_devops_sdk import engine as bridge
from apexai_devops_sdk.compose import ComposeHealthAdapter, ComposeService

SLOTS = (
    "retirement_authorizer",
    "acknowledgement_authorizer",
    "clearance_authorizer",
    "inventory_confirmation_authorizer",
)


@dataclass
class _OperatorHostWiring(_HostWiring):
    retirement_authorizer: Any = None
    acknowledgement_authorizer: Any = None
    clearance_authorizer: Any = None
    inventory_confirmation_authorizer: Any = None


@pytest.fixture
def operator_stand_in(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """The stand-in, as an engine whose wiring has every operator-act slot."""

    request.getfixturevalue("watch_stand_in")
    monkeypatch.setattr(sys.modules["apexai_ops.runtime.wiring"], "HostWiring", _OperatorHostWiring)


class _Policy:
    """Allows exactly one actor, and records every question it was asked."""

    def __init__(self, allowed: str = "alice", answer: object = None) -> None:
        self.allowed = allowed
        self.answer = answer
        self.asked: list[tuple[str, dict[str, object]]] = []

    def _decide(self, act: str, **question: object) -> object:
        self.asked.append((act, question))
        if self.answer is not None:
            return self.answer
        return OperatorDecision(
            authorized=question["actor_id"] == self.allowed, authorizer_id="oncall-policy"
        )

    def authorize_retirement(self, *, actor_id: str, target_id: str, work_id: str) -> object:
        return self._decide("retire", actor_id=actor_id, target_id=target_id, work_id=work_id)

    def authorize_acknowledgement(self, *, actor_id: str, target_id: str, work_id: str) -> object:
        return self._decide("ack", actor_id=actor_id, target_id=target_id, work_id=work_id)

    def authorize_clearance(self, *, actor_id: str, target_id: str, work_id: str) -> object:
        return self._decide("clear", actor_id=actor_id, target_id=target_id, work_id=work_id)

    def authorize_inventory_retirement(
        self, *, actor_id: str, source_id: str, inventory_generation: int, retirement_count: int
    ) -> object:
        return self._decide(
            "confirm",
            actor_id=actor_id,
            source_id=source_id,
            inventory_generation=inventory_generation,
            retirement_count=retirement_count,
        )


def _wiring(**kwargs: Any) -> Any:
    adapter = ComposeHealthAdapter({"web": ComposeService("example", "app")},
                                   socket_path="/run/docker.sock")  # fmt: skip
    return bridge.read_only_host_wiring(
        adapter, controller_id=CONTROLLER, policy_revision="r1", **kwargs
    )


# ------------------------------------------------------------------ the decision


def test_a_decision_is_a_bool_a_short_label_and_a_known_kind() -> None:
    decision = OperatorDecision(authorized=True, authorizer_id="oncall-policy")
    assert decision.actor_kind == "human"
    assert OperatorDecision(False, "ops2", "service").actor_kind == "service"


@pytest.mark.parametrize(
    ("authorized", "authorizer_id", "actor_kind"),
    [
        (1, "oncall-policy", "human"),  # not a bool
        (True, "Oncall", "human"),  # uppercase
        (True, "oncall_policy", "human"),  # underscore
        (True, "on.call", "human"),  # dot
        (True, "a" * 33, "human"),  # too long
        (True, "-oncall", "human"),  # not a label
        (True, "oncall", "robot"),  # unknown kind
        (True, 7, "human"),  # not text
    ],
)
def test_a_decision_the_engine_could_not_record_is_refused(
    authorized: object, authorizer_id: object, actor_kind: object
) -> None:
    with pytest.raises(ContractError):
        OperatorDecision(authorized, authorizer_id, actor_kind)  # type: ignore[arg-type]


# ------------------------------------------------------------------ the bridge


def test_without_authorizers_the_wiring_sets_none(operator_stand_in: None) -> None:
    wiring = _wiring()
    assert all(getattr(wiring, slot) is None for slot in SLOTS)


@pytest.mark.parametrize("slot", SLOTS)
def test_each_authorizer_reaches_the_engine_in_its_own_type(
    operator_stand_in: None, slot: str
) -> None:
    policy = _Policy()
    method = bridge.OPERATOR_AUTHORIZER_SLOTS[slot][1]
    answer = getattr(getattr(_wiring(**{slot: policy}), slot), method)
    if slot == "inventory_confirmation_authorizer":
        question: dict[str, object] = {
            "source_id": "sdk-0", "inventory_generation": 3, "retirement_count": 5
        }  # fmt: skip
    else:
        question = {"target_id": "web", "work_id": "wrk_1"}
    allowed = answer(actor_id="alice", **question)
    refused = answer(actor_id="mallory", **question)
    assert type(allowed) is _RearmDecision and type(refused) is _RearmDecision
    assert (allowed.authorized, allowed.authorizer_id, allowed.actor_kind) == (
        True, "oncall-policy", "human"
    )  # fmt: skip
    assert refused.authorized is False
    assert [asked[1] for asked in policy.asked] == [
        {"actor_id": "alice", **question}, {"actor_id": "mallory", **question}
    ]  # fmt: skip


@pytest.mark.parametrize("answer", [True, "yes", {"authorized": True}, 0])
def test_an_answer_that_is_not_a_decision_refuses(operator_stand_in: None, answer: object) -> None:
    authorizer = _wiring(retirement_authorizer=_Policy(answer=answer)).retirement_authorizer
    decision = authorizer.authorize_retirement(actor_id="alice", target_id="web", work_id="wrk_1")
    assert decision.authorized is False
    assert decision.authorizer_id == bridge.INVALID_ANSWER_AUTHORIZER_ID


@pytest.mark.parametrize("slot", SLOTS)
def test_a_class_or_an_object_without_the_method_is_refused(
    operator_stand_in: None, slot: str
) -> None:
    for wrong in (_Policy, object(), "alice"):
        with pytest.raises(ContractError, match=slot):
            _wiring(**{slot: wrong})


@pytest.mark.usefixtures("watch_stand_in")
@pytest.mark.parametrize("slot", ["retirement_authorizer", "acknowledgement_authorizer"])
def test_an_engine_without_the_slot_says_so(slot: str) -> None:
    with pytest.raises(bridge.EngineUnavailable, match=slot):
        _wiring(**{slot: _Policy()})


def test_the_policy_still_allows_nothing(operator_stand_in: None) -> None:
    wiring = _wiring(**{slot: _Policy() for slot in SLOTS})
    assert wiring.policy.enabled is False
    assert wiring.policy.allowed_action_ids == frozenset()
