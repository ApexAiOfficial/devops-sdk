"""Test helpers for hosts: contract checks and offline fakes."""

from apexai_devops_sdk.testing.checks import assert_target_adapter, check_target_adapter
from apexai_devops_sdk.testing.fakes import (
    TELEGRAM_OK,
    FakeDockerDaemon,
    FakeTelegramApi,
    compose_container,
    http_response,
)

__all__ = [
    "TELEGRAM_OK",
    "FakeDockerDaemon",
    "FakeTelegramApi",
    "assert_target_adapter",
    "check_target_adapter",
    "compose_container",
    "http_response",
]
