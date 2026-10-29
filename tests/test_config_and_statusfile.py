"""Settings helpers and the bounded status-file read."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apexai_devops_sdk import ContractError
from apexai_devops_sdk.config import (
    SettingError,
    absolute_path,
    environment_key,
    identifiers,
    integers,
    private_directory,
    read_secret_file,
    setting,
)
from apexai_devops_sdk.statusfile import read_json_status


def _secret(tmp: Path, text: str, mode: int = 0o600) -> Path:
    path = tmp / "secret.env"
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


def test_settings_parse_or_refuse_without_echoing() -> None:
    env = {"A": " /srv/x ", "IDS": "web, worker", "CHATS": "1,2", "BAD": "a b", "REL": "x/y"}
    assert setting(env, "A") == "/srv/x" and setting(env, "MISSING", "d") == "d"
    assert absolute_path(env, "A") == Path("/srv/x")
    assert identifiers(env, "IDS") == ("web", "worker")
    assert integers(env, "CHATS") == (1, 2)
    assert environment_key("MY_KEY", name="K") == "MY_KEY"
    for call in (
        lambda: absolute_path(env, "REL"),
        lambda: identifiers(env, "BAD"),
        lambda: identifiers({"IDS": "a,a"}, "IDS"),
        lambda: integers({"C": "1,x"}, "C"),
        lambda: integers({"C": "0"}, "C"),
        lambda: environment_key("lower", name="K"),
    ):
        with pytest.raises(SettingError) as refused:
            call()
        assert "a b" not in str(refused.value) and "x/y" not in str(refused.value)


def test_a_secret_is_read_only_from_an_owner_only_file(tmp_path: Path) -> None:
    path = _secret(tmp_path, "# comment\nKEY=value-1\nOTHER=x\n")
    assert read_secret_file(path, "KEY") == "value-1"
    assert read_secret_file(path, "MISSING") is None
    assert read_secret_file(path, "KEY", pattern=__import__("re").compile(r"\d+")) is None
    path.chmod(0o640)
    assert read_secret_file(path, "KEY") is None, "a group-readable file is not ours alone"


def test_a_secret_file_that_is_a_link_or_too_large_is_refused(tmp_path: Path) -> None:
    real = _secret(tmp_path, "KEY=value\n")
    link = tmp_path / "link.env"
    link.symlink_to(real)
    assert read_secret_file(link, "KEY") is None
    big = _secret(tmp_path, "KEY=" + "x" * 5000 + "\n")
    assert read_secret_file(big, "KEY") is None
    assert read_secret_file(real, "lower") is None


def test_a_private_directory(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    assert private_directory(directory)
    os.chmod(directory, 0o770)
    assert not private_directory(directory)
    assert not private_directory(tmp_path / "missing")


def test_a_status_file_is_read_or_refused_by_a_closed_code(tmp_path: Path) -> None:
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"observed": "running"}), encoding="utf-8")
    good.chmod(0o644)  # only its owner can write it
    assert read_json_status(good).document == {"observed": "running"}
    cases = {
        "missing": (tmp_path / "absent.json", "status_missing"),
        "directory": (tmp_path, "status_not_a_file"),
    }
    (tmp_path / "list.json").write_text("[1]", encoding="utf-8")
    cases["not-object"] = (tmp_path / "list.json", "status_malformed")
    (tmp_path / "bad.json").write_bytes(b"\xff{")
    cases["not-utf8"] = (tmp_path / "bad.json", "status_malformed")
    (tmp_path / "big.json").write_text(json.dumps({"x": "y" * 200}), encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(good)
    cases["link"] = (link, "status_unreadable")
    for written in tmp_path.glob("*.json"):
        if not written.is_symlink():
            written.chmod(0o644)  # only their owner can write them, so each refusal is its own
    for name, (path, refusal) in cases.items():
        read = read_json_status(path)
        assert (read.ok, read.refusal) == (False, refusal), name
    assert read_json_status(tmp_path / "big.json", max_bytes=64).refusal == "status_oversized"
    assert read_json_status(tmp_path / "absent.json", prefix="state").refusal == "state_missing"
    with pytest.raises(ContractError):
        read_json_status(good, prefix="Bad Prefix")


def test_a_status_file_others_can_write_is_unsafe(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o664)
    assert read_json_status(path).refusal == "status_unsafe"
    path.chmod(0o644)
    assert read_json_status(path).ok
    assert read_json_status(path, trusted_owners={os.geteuid() + 1}).refusal == "status_unsafe"
    assert read_json_status(path, trusted_owners={os.geteuid()}).ok


def test_a_prefix_that_would_overflow_a_closed_word_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ContractError):
        read_json_status(tmp_path / "x.json", prefix="p" * 53)
    assert read_json_status(tmp_path / "x.json", prefix="p" * 52).refusal == "p" * 52 + "_missing"
