"""Settings helpers for hosts: closed parsing, and secrets read from owner-only files.

Two rules run through all of it:

- **Refuse, never guess.** A setting that does not parse raises
  :class:`SettingError` naming the setting, never echoing its value.
- **Secrets live in files, not in the environment.** :func:`read_secret_file`
  reads one ``KEY=value`` line from a regular file owned by this user with no
  group or other permission bits, at read time, and returns ``None`` rather
  than a secret it cannot vouch for.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from apexai_devops_sdk.contract import IDENTIFIER

MAX_SECRET_FILE_BYTES: Final = 4096
_KEY: Final = re.compile(r"[A-Z][A-Z0-9_]{0,63}")


class SettingError(ValueError):
    """A setting the host refuses. The message names the setting, never its value."""


def setting(environ: Mapping[str, str], name: str, default: str = "") -> str:
    """``environ[name]`` stripped, or ``default`` when unset or blank."""

    value = environ.get(name, "").strip()
    return value or default


def absolute_path(environ: Mapping[str, str], name: str, default: str = "") -> Path:
    """An absolute path setting; refuses a relative or empty one."""

    value = setting(environ, name, default)
    if not value or not os.path.isabs(value):
        raise SettingError(f"{name} must be an absolute path")
    return Path(value)


def identifiers(
    environ: Mapping[str, str], name: str, *, pattern: re.Pattern[str] = IDENTIFIER
) -> tuple[str, ...]:
    """A comma-separated list of distinct identifiers; refuses an empty or malformed list."""

    items = tuple(part.strip() for part in setting(environ, name).split(",") if part.strip())
    if not items or len(set(items)) != len(items) or not all(pattern.fullmatch(i) for i in items):
        raise SettingError(f"{name} must be distinct, comma-separated identifiers")
    return items


def integers(environ: Mapping[str, str], name: str) -> tuple[int, ...]:
    """A comma-separated list of distinct, non-zero integers (chat ids, for example)."""

    parts = [part.strip() for part in setting(environ, name).split(",") if part.strip()]
    try:
        values = tuple(int(part) for part in parts)
    except ValueError:
        raise SettingError(f"{name} must be comma-separated integers") from None
    if not values or len(set(values)) != len(values) or 0 in values:
        raise SettingError(f"{name} must be distinct, non-zero integers")
    return values


def environment_key(value: str, *, name: str) -> str:
    """An environment-style key (``UPPER_CASE``), as used inside a secret file."""

    if not _KEY.fullmatch(value):
        raise SettingError(f"{name} must be an environment-style name")
    return value


def read_secret_file(
    path: Path | str,
    key: str,
    *,
    pattern: re.Pattern[str] | None = None,
    max_bytes: int = MAX_SECRET_FILE_BYTES,
) -> str | None:
    """The value of ``key`` in an owner-only secret file, or ``None``.

    The file must be a regular file (a symbolic link is not followed), owned
    by this process's user, with no group or other permission bits, at most
    ``max_bytes``, holding plain ``KEY=value`` lines (no ``export``, no
    quotes; ``#`` comments and blank lines are skipped). When ``pattern`` is
    given the value must match it entirely. Every failure is ``None``: the
    caller decides what a missing secret means, and nothing is echoed.
    """

    if not _KEY.fullmatch(key):
        return None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
        ):
            return None
        raw = os.read(descriptor, max_bytes + 1)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    if len(raw) > max_bytes:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if separator and name.strip() == key:
            secret = value.strip()
            if not secret or (pattern is not None and not pattern.fullmatch(secret)):
                return None
            return secret
    return None


def private_directory(path: Path | str) -> bool:
    """Whether ``path`` is a real directory owned by this user and writable by no one else."""

    try:
        info = os.lstat(path)
    except OSError:
        return False
    return (
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.geteuid()
        and not info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    )
