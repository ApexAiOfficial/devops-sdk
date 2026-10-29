"""A bounded, read-only read of a JSON status file another process writes.

Many hosts already keep a small status document per target (a reconciler's
state file, a sidecar's last result). Reading it is the least-privileged way
to observe: one file read, no subprocess, no network, no daemon access.

:func:`read_json_status` opens the file without following a symbolic link,
requires a regular file, caps the read, and parses one JSON object. Every
failure is a closed refusal code, ``<prefix>_missing``, ``_unreadable``,
``_not_a_file``, ``_oversized`` or ``_malformed``, and nothing the file holds
is ever put into a refusal. Interpreting the document is the host's: the
values that mean healthy or broken belong to the system that writes it.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from apexai_devops_sdk.contract import CLOSED_WORD, ContractError

DEFAULT_MAX_BYTES: Final = 64 * 1024


@dataclass(frozen=True, slots=True)
class StatusRead:
    """A status document, or ``None`` and the closed reason it could not be read."""

    document: Mapping[str, Any] | None
    refusal: str = ""

    @property
    def ok(self) -> bool:
        return self.document is not None


def read_json_status(
    path: Path | str, *, prefix: str = "status", max_bytes: int = DEFAULT_MAX_BYTES
) -> StatusRead:
    """One JSON object from ``path``, or a closed refusal. Never raises for the file's sake."""

    if not CLOSED_WORD.fullmatch(prefix):
        raise ContractError("prefix is not a closed word")
    if type(max_bytes) is not int or max_bytes < 1:
        raise ContractError("max_bytes must be a positive integer")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return StatusRead(None, f"{prefix}_missing")
    except OSError:
        return StatusRead(None, f"{prefix}_unreadable")  # a link (ELOOP), permissions, ...
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return StatusRead(None, f"{prefix}_not_a_file")
        raw = os.read(descriptor, max_bytes + 1)
    except OSError:
        return StatusRead(None, f"{prefix}_unreadable")
    finally:
        os.close(descriptor)
    if len(raw) > max_bytes:
        return StatusRead(None, f"{prefix}_oversized")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return StatusRead(None, f"{prefix}_malformed")
    if not isinstance(document, dict):
        return StatusRead(None, f"{prefix}_malformed")
    return StatusRead(document)
