"""An example host distribution: a read-only monitor of Compose services with Telegram alerts.

Install it beside the engine and start the engine's runtime with this host;
the engine calls :func:`runtime` once at start. Settings (all required except
the Telegram ones):

- ``MONITOR_SERVICES``: ``target=project/service`` pairs, comma-separated;
- ``MONITOR_CONTROLLER_ID``: this monitor's identity, ``ctl_`` and 32 hex digits;
- ``MONITOR_DOCKER_SOCKET``: the daemon's socket (default ``/var/run/docker.sock``);
- ``MONITOR_TELEGRAM_CHAT_IDS``, ``MONITOR_TELEGRAM_TOKEN_FILE``,
  ``MONITOR_TELEGRAM_STATE_DIR``: when the chat ids are set, notices go there.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from apexai_devops_sdk import ContractError
from apexai_devops_sdk.compose import DEFAULT_SOCKET, ComposeHealthAdapter, ComposeService
from apexai_devops_sdk.config import SettingError, absolute_path, integers, setting
from apexai_devops_sdk.engine import read_only_host_wiring
from apexai_devops_sdk.telegram import TelegramAttentionSink


def services(environ: Mapping[str, str]) -> dict[str, ComposeService]:
    pairs: dict[str, ComposeService] = {}
    for item in setting(environ, "MONITOR_SERVICES").split(","):
        target, _equals, where = item.strip().partition("=")
        project, _slash, service = where.partition("/")
        if not (target and project and service) or target in pairs:
            raise SettingError("MONITOR_SERVICES must be distinct target=project/service pairs")
        try:
            pairs[target] = ComposeService(project, service)
        except ContractError:
            raise SettingError("MONITOR_SERVICES names a project or service it refuses") from None
    return pairs


def runtime(environ: Mapping[str, str] = os.environ) -> Any:
    adapter = ComposeHealthAdapter(
        services(environ), socket_path=setting(environ, "MONITOR_DOCKER_SOCKET", DEFAULT_SOCKET)
    )
    sink = None
    if setting(environ, "MONITOR_TELEGRAM_CHAT_IDS"):
        sink = TelegramAttentionSink(
            token_file=absolute_path(environ, "MONITOR_TELEGRAM_TOKEN_FILE"),
            chat_ids=integers(environ, "MONITOR_TELEGRAM_CHAT_IDS"),
            state_dir=absolute_path(environ, "MONITOR_TELEGRAM_STATE_DIR"),
        )
        if not sink.check():
            raise SettingError("the Telegram sink cannot deliver: check its token file and state")
    return read_only_host_wiring(
        adapter,
        controller_id=setting(environ, "MONITOR_CONTROLLER_ID"),
        policy_revision="compose-monitor-read-only-r1",
        attention_sink=sink,
    )
