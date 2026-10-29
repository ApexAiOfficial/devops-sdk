"""The Telegram attention sink, against a fake Bot API process (offline)."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from apexai_devops_sdk import AttentionNotice, ContractError
from apexai_devops_sdk.telegram import TelegramAttentionSink, render
from apexai_devops_sdk.testing import TELEGRAM_OK, FakeTelegramApi

TOKEN = "123456789:" + "A" * 35  # a synthetic token in the Bot API's shape
KEY = "EXAMPLE_BOT_TOKEN"
FIRST, SECOND = 100000001, 100000002  # synthetic chat ids


@pytest.fixture
def api(tmp_path: Path) -> Iterator[FakeTelegramApi]:
    with FakeTelegramApi(tmp_path) as served:
        yield served


def _token_file(tmp: Path, *, mode: int = 0o600, text: str | None = None) -> Path:
    path = tmp / "bot.env"
    path.write_text(text if text is not None else f"# a comment\n{KEY}={TOKEN}\n", encoding="utf-8")
    path.chmod(mode)
    return path


def _sink(tmp: Path, api: FakeTelegramApi, *chats: int, **kwargs: Any) -> TelegramAttentionSink:
    state = tmp / "delivered"
    state.mkdir(mode=0o700, exist_ok=True)
    options: dict[str, Any] = {
        "token_key": KEY,
        "chat_ids": chats or (FIRST,),
        "state_dir": state,
        "api_base": api.base_url,
    }
    options.update(kwargs)
    if "token_file" not in options:  # a test's own token file is never overwritten
        options["token_file"] = _token_file(tmp)
    return TelegramAttentionSink(**options)


def _notice(work: str = "1", attempt: int = 1) -> dict[str, Any]:
    return {
        "notification_id": "ntf_" + work * 32,
        "work_id": "wrk_" + work * 32,
        "attention_kind": "rearmable_blocker",
        "incident_id": "inc_" + "2" * 32,
        "desired_revision": 3,
        "target_id": "example-target",
        "decision_state": "blocked",
        "reason_code": "remedy_unresolvable",
        "recovery_kind": None,
        "attempt": attempt,
        "settled_at": "2026-10-29T07:00:00Z",
    }


def test_an_offer_is_delivered_once_and_then_de_duplicated(
    tmp_path: Path, api: FakeTelegramApi
) -> None:
    sink = _sink(tmp_path, api)
    assert sink.offer(_notice()) == "accepted"
    [request] = api.requests()
    assert request["path"] == f"/bot{TOKEN}/sendMessage"
    assert request["body"]["chat_id"] == FIRST
    assert request["body"]["text"] == render(AttentionNotice.from_payload(_notice()))
    assert "parse_mode" not in request["body"], "plain text: nothing is read as markup"
    assert sink.offer(_notice(attempt=2)) == "accepted"
    assert len(api.requests()) == 1, "a notice already delivered is never sent again"


def test_a_refusing_chat_is_refused_and_never_retried(tmp_path: Path, api: FakeTelegramApi) -> None:
    api.answer(TELEGRAM_OK, **{str(SECOND): [403, {"ok": False}]})
    sink = _sink(tmp_path, api, FIRST, SECOND)
    assert sink.offer(_notice()) == "accepted"
    assert sink.offer(_notice()) == "accepted"
    assert sorted(r["body"]["chat_id"] for r in api.requests()) == [FIRST, SECOND]
    api.answer([400, {"ok": False}])
    other = tmp_path / "other"
    other.mkdir()
    lonely = _sink(other, api, SECOND)
    assert lonely.offer(_notice("3")) == "refused"


def test_a_transient_failure_is_retried_without_starving_other_chats(
    tmp_path: Path, api: FakeTelegramApi
) -> None:
    api.answer(TELEGRAM_OK, **{str(FIRST): [502, "bad gateway"]})
    sink = _sink(tmp_path, api, FIRST, SECOND)
    assert sink.offer(_notice()) == "failed"
    assert {r["body"]["chat_id"] for r in api.requests()} == {FIRST, SECOND}
    api.answer(TELEGRAM_OK)
    assert sink.offer(_notice()) == "accepted"
    sent = [r["body"]["chat_id"] for r in api.requests()]
    assert sent.count(SECOND) == 1 and sent.count(FIRST) == 2


def test_a_stalled_api_fails_within_the_deadline(tmp_path: Path, api: FakeTelegramApi) -> None:
    api.answer(["stall", None])
    began = time.monotonic()
    assert _sink(tmp_path, api, deadline_seconds=0.5).offer(_notice()) == "failed"
    assert time.monotonic() - began < 2.0


@pytest.mark.parametrize(
    ("mode", "text"),
    [
        (0o640, None),
        (0o600, "EXAMPLE_BOT_TOKEN=not-a-token\n"),
        (0o600, "OTHER_KEY=123456789:" + "A" * 35 + "\n"),
        (0o600, "# EXAMPLE_BOT_TOKEN=123456789:" + "A" * 35 + "\n"),
    ],
    ids=["group-readable", "malformed", "other-key", "commented"],
)
def test_an_unusable_token_file_sends_nothing(
    tmp_path: Path, api: FakeTelegramApi, mode: int, text: str | None
) -> None:
    sink = _sink(tmp_path, api, token_file=_token_file(tmp_path, mode=mode, text=text))
    assert not sink.check()
    assert sink.offer(_notice()) == "failed" and api.requests() == []


def test_an_unsafe_state_directory_sends_nothing(tmp_path: Path, api: FakeTelegramApi) -> None:
    sink = _sink(tmp_path, api)
    os.chmod(tmp_path / "delivered", 0o777)
    assert not sink.check()
    assert sink.offer(_notice()) == "failed" and api.requests() == []


def test_a_foreign_object_at_a_marker_s_name_is_never_taken_as_proof(
    tmp_path: Path, api: FakeTelegramApi
) -> None:
    sink = _sink(tmp_path, api)
    (tmp_path / "delivered" / f"ntf_{'1' * 32}.{FIRST}").mkdir()
    assert sink.offer(_notice()) == "failed" and api.requests() == []


def test_a_payload_outside_the_contract_sends_nothing_and_is_retried(
    tmp_path: Path, api: FakeTelegramApi
) -> None:
    assert _sink(tmp_path, api).offer({"notification_id": "nope"}) == "failed"
    assert api.requests() == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"chat_ids": ()},
        {"chat_ids": (FIRST, FIRST)},
        {"chat_ids": (0,)},
        {"token_key": "lower"},
        {"api_base": "http://example.com"},
        {"api_base": "https://user@api.example.com"},
        {"api_base": "https://api.example.com/path"},
        {"deadline_seconds": 2.5},
        {"token_file": "relative.env"},
    ],
    ids=["no-chats", "repeated-chat", "zero-chat", "bad-key", "plain-http", "userinfo",
         "a-path", "long-deadline", "relative-token"],
)  # fmt: skip
def test_a_bad_configuration_is_refused(
    tmp_path: Path, api: FakeTelegramApi, kwargs: dict[str, Any]
) -> None:
    with pytest.raises(ContractError):
        _sink(tmp_path, api, **kwargs)


def test_the_token_never_appears_in_the_sink_s_repr(tmp_path: Path, api: FakeTelegramApi) -> None:
    sink = _sink(tmp_path, api)
    assert TOKEN not in repr(sink) and str(tmp_path) not in repr(sink)


def test_a_stalling_chat_cannot_starve_the_one_after_it(
    tmp_path: Path, api: FakeTelegramApi
) -> None:
    """Each retry starts at a different chat, so the second chat is told on a retry."""

    api.answer(TELEGRAM_OK, **{str(FIRST): ["stall", None]})
    sink = _sink(tmp_path, api, FIRST, SECOND, deadline_seconds=0.5)
    assert sink.offer(_notice(attempt=0)) == "failed"  # FIRST stalls and spends the deadline
    assert sink.offer(_notice(attempt=1)) == "failed"  # SECOND first now: told, FIRST still owed
    assert any(r["body"]["chat_id"] == SECOND for r in api.requests()), api.requests()
    assert (tmp_path / "delivered" / f"ntf_{'1' * 32}.{SECOND}").is_file()


def test_a_payload_the_sdk_cannot_read_is_retried_never_dropped(
    tmp_path: Path, api: FakeTelegramApi
) -> None:
    assert _sink(tmp_path, api).offer({**_notice(), "attempt": "two"}) == "failed"


def test_markers_prove_nothing_in_a_directory_others_can_write(
    tmp_path: Path, api: FakeTelegramApi
) -> None:
    sink = _sink(tmp_path, api)
    assert sink.offer(_notice()) == "accepted"
    os.chmod(tmp_path / "delivered", 0o777)
    assert sink.offer(_notice()) == "failed", "a forged marker could suppress an alert"


def test_a_malformed_api_base_is_a_contract_error(tmp_path: Path, api: FakeTelegramApi) -> None:
    for base in ("https://api.example.com:99999", "https://[::1"):
        with pytest.raises(ContractError):
            _sink(tmp_path, api, api_base=base)
