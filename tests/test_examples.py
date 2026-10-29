"""The examples stay correct: their contract checks pass and their settings parse."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from apexai_devops_sdk.config import SettingError

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
sys.path.insert(0, str(EXAMPLES))
sys.path.insert(0, str(EXAMPLES / "compose_monitor"))

from compose_monitor.wiring import services  # noqa: E402
from custom_adapter import test_the_heartbeat_adapter_meets_the_contract  # noqa: E402, F401


def test_the_compose_monitor_parses_its_services() -> None:
    parsed = services({"MONITOR_SERVICES": "web=shop/app, jobs=shop/worker"})
    assert {target: where.container for target, where in parsed.items()} == {
        "web": "shop-app-1",
        "jobs": "shop-worker-1",
    }
    for bad in ("", "web=shop", "web=shop/app,web=shop/app", "=shop/app"):
        with pytest.raises(SettingError):
            services({"MONITOR_SERVICES": bad})
