"""A local, file-backed epoch fence for a host's commit path.

``FileFence`` is deliberately fail closed.  It trusts only the configured
owner, the exact on-disk modes, a protected parent, and Linux-local ``ext4``
or ``xfs`` mounts.  Raising a floor takes an exclusive ``flock``; commits
hold a shared lock from the floor check through the caller's commit block.
"""

from __future__ import annotations

import fcntl
import math
import os
import secrets
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Final

from apexai_devops_sdk.contract import ContractError, is_identifier

_DIRECTORY_MODE: Final = 0o2750
_FILE_MODE: Final = 0o640
_MAX_FLOOR_BYTES: Final = 32
_MAX_MOUNTINFO_BYTES: Final = 4 * 1024 * 1024
_LOCK_POLL_SECONDS: Final = 0.01
_LOCAL_FILESYSTEMS: Final = frozenset({b"ext4", b"xfs"})
# The only source of the filesystem type. It is not a constructor argument: a check
# that a caller could point at another file would not be a check.
_MOUNTINFO_PATH: Final = "/proc/self/mountinfo"

_CLOSE_ON_EXEC: Final = os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS: Final = os.O_RDONLY | os.O_NONBLOCK | _CLOSE_ON_EXEC
_DIRECTORY_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | _CLOSE_ON_EXEC


class FenceUnavailable(ContractError):
    """The commit lock could not be safely opened or acquired in time."""


class FenceStale(ContractError):
    """The proposed commit epoch is not the target's current floor."""


class _FloorKind(Enum):
    ABSENT = auto()
    INVALID = auto()
    VALUE = auto()


@dataclass(frozen=True, slots=True)
class _FloorRead:
    kind: _FloorKind
    value: int | None = None


_ABSENT_FLOOR: Final = _FloorRead(_FloorKind.ABSENT)
_INVALID_FLOOR: Final = _FloorRead(_FloorKind.INVALID)


class FileFence:
    """A monotonically increasing target floor on one local filesystem."""

    def __init__(
        self,
        directory: Path | str,
        *,
        owner_uid: int,
        raise_wait_seconds: float,
        commit_wait_seconds: float,
    ) -> None:
        if type(owner_uid) is not int or owner_uid < 0:
            raise ContractError("file fence: owner_uid must be a non-negative integer")
        self._directory = Path(os.path.abspath(directory))
        self._owner_uid = owner_uid
        self._raise_wait = self._validated_wait("raise_wait_seconds", raise_wait_seconds)
        self._commit_wait = self._validated_wait("commit_wait_seconds", commit_wait_seconds)

    @staticmethod
    def _validated_wait(name: str, value: object) -> float:
        refused = ContractError(f"file fence: {name} must be a finite number above zero")
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise refused
        try:
            seconds = float(value)
        except OverflowError:
            raise refused from None
        if not math.isfinite(seconds) or seconds <= 0.0:
            raise refused
        return seconds

    @staticmethod
    def _target_names(target_id: object) -> tuple[str, str, str]:
        if not is_identifier(target_id):
            raise ContractError("file fence: target_id is not an identifier")
        assert isinstance(target_id, str)
        return target_id, f"{target_id}.lock", f"{target_id}.floor"

    @staticmethod
    def _epoch_bytes(epoch: object) -> tuple[int, bytes]:
        if type(epoch) is not int or epoch < 0:
            raise ContractError("file fence: epoch must be a non-negative integer")
        assert isinstance(epoch, int)
        raw = f"{epoch}\n".encode("ascii")
        if len(raw) > _MAX_FLOOR_BYTES:
            raise ContractError("file fence: epoch is too large for a floor")
        return epoch, raw

    def fence_capability(self, target_id: str) -> bool:
        """Whether the configured directory can safely hold this target's floor."""

        self._target_names(target_id)
        descriptor = self._open_directory()
        if descriptor is None:
            return False
        self._close(descriptor)
        return True

    def publish_fence(self, target_id: str, epoch: int) -> bool:
        """Raise ``target_id``'s floor to ``epoch``, never lowering or repairing it.

        ``True`` means the floor is ``epoch``, durably. ``False`` never lowers the
        floor and, with one exception, leaves it unchanged: when the directory's
        ``fsync`` fails after the new floor replaced the old one, the floor may
        already read ``epoch``, and a later raise to ``epoch`` answers ``True``.
        """

        target, lock_name, floor_name = self._target_names(target_id)
        epoch, raw_epoch = self._epoch_bytes(epoch)
        directory_descriptor = self._open_directory()
        if directory_descriptor is None:
            return False
        lock_descriptor: int | None = None
        temporary_name: str | None = None
        try:
            lock_descriptor = self._open_lock(directory_descriptor, target, lock_name, create=True)
            if lock_descriptor is None or not self._take_lock(
                lock_descriptor, fcntl.LOCK_EX, self._raise_wait
            ):
                return False
            if not self._remove_abandoned_temporaries(directory_descriptor, target):
                return False
            floor = self._read_floor(directory_descriptor, floor_name)
            if floor.kind is _FloorKind.INVALID:
                return False
            if floor.kind is _FloorKind.VALUE:
                if floor.value is not None and floor.value > epoch:
                    return False
                if floor.value == epoch:
                    return True

            created = self._create_temporary(directory_descriptor, target)
            if created is None:
                return False
            temporary_descriptor, temporary_name = created
            try:
                if not self._write_all(temporary_descriptor, raw_epoch):
                    return False
                os.fsync(temporary_descriptor)
            except OSError:
                return False
            finally:
                self._close(temporary_descriptor)

            try:
                os.rename(
                    temporary_name,
                    floor_name,
                    src_dir_fd=directory_descriptor,
                    dst_dir_fd=directory_descriptor,
                )
                temporary_name = None
                # A failure here leaves the new floor in place but not durable: answer
                # False (fail closed); the floor is never lower than it was.
                os.fsync(directory_descriptor)
            except OSError:
                return False
            return True
        finally:
            if temporary_name is not None:
                self._unlink_temporary(directory_descriptor, temporary_name)
            if lock_descriptor is not None:
                self._close(lock_descriptor)
            self._close(directory_descriptor)

    def fence_floor(self, target_id: str) -> int | None:
        """Return a safe, complete floor, or ``None`` for every refusal."""

        _, _, floor_name = self._target_names(target_id)
        directory_descriptor = self._open_directory()
        if directory_descriptor is None:
            return None
        try:
            floor = self._read_floor(directory_descriptor, floor_name)
            return floor.value if floor.kind is _FloorKind.VALUE else None
        finally:
            self._close(directory_descriptor)

    @contextmanager
    def committing(self, target_id: str, epoch: int) -> Iterator[None]:
        """Hold a shared target lock while a current-epoch commit runs."""

        target, lock_name, floor_name = self._target_names(target_id)
        epoch, _ = self._epoch_bytes(epoch)
        directory_descriptor = self._open_directory()
        if directory_descriptor is None:
            raise FenceUnavailable("file fence: safe directory is unavailable")
        lock_descriptor: int | None = None
        try:
            lock_descriptor = self._open_lock(directory_descriptor, target, lock_name, create=False)
            if lock_descriptor is None or not self._take_lock(
                lock_descriptor, fcntl.LOCK_SH, self._commit_wait
            ):
                raise FenceUnavailable("file fence: commit lock is unavailable")
            floor = self._read_floor(directory_descriptor, floor_name)
            if floor.kind is not _FloorKind.VALUE or floor.value != epoch:
                raise FenceStale("file fence: commit epoch does not equal the floor")
            yield
        finally:
            if lock_descriptor is not None:
                self._close(lock_descriptor)
            self._close(directory_descriptor)

    # ------------------------------------------------------------ trust boundary

    def _open_directory(self) -> int | None:
        name = self._directory.name
        if not name:
            return None
        try:
            parent_descriptor = os.open(self._directory.parent, _DIRECTORY_FLAGS)
        except OSError:
            return None
        try:
            try:
                parent = os.fstat(parent_descriptor)
            except OSError:
                return None
            if (
                not stat.S_ISDIR(parent.st_mode)
                or parent.st_uid not in (0, self._owner_uid)
                or parent.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            ):
                return None
            # By name inside the parent that was checked, so a parent swapped after
            # its check cannot supply the directory.
            try:
                descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_descriptor)
            except OSError:
                return None
        finally:
            self._close(parent_descriptor)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != self._owner_uid
                or stat.S_IMODE(info.st_mode) != _DIRECTORY_MODE
                or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                or not self._is_local_filesystem(info.st_dev)
            ):
                self._close(descriptor)
                return None
        except OSError:
            self._close(descriptor)
            return None
        return descriptor

    def _is_local_filesystem(self, device: int) -> bool:
        try:
            descriptor = os.open(_MOUNTINFO_PATH, _READ_FLAGS)
        except OSError:
            return False
        try:
            raw = self._read_at_most(descriptor, _MAX_MOUNTINFO_BYTES)
        finally:
            self._close(descriptor)
        if raw is None:
            return False
        wanted = f"{os.major(device)}:{os.minor(device)}".encode("ascii")
        for line in raw.splitlines():
            fields = line.split()
            if len(fields) < 7 or fields[2] != wanted:
                continue
            try:
                separator = fields.index(b"-", 6)
            except ValueError:
                return False
            return separator + 1 < len(fields) and fields[separator + 1] in _LOCAL_FILESYSTEMS
        return False

    def _open_lock(
        self, directory_descriptor: int, target: str, name: str, *, create: bool
    ) -> int | None:
        try:
            descriptor = os.open(name, _READ_FLAGS, dir_fd=directory_descriptor)
        except FileNotFoundError:
            if not create or not self._publish_lock(directory_descriptor, target, name):
                return None
            try:
                descriptor = os.open(name, _READ_FLAGS, dir_fd=directory_descriptor)
            except OSError:
                return None
        except OSError:
            return None
        try:
            if self._safe_regular_file(descriptor):
                return descriptor
        except OSError:
            pass
        self._close(descriptor)
        return None

    def _publish_lock(self, directory_descriptor: int, target: str, name: str) -> bool:
        """Give the target its lock file, which never exists with another mode.

        The file is made under a temporary name, given its mode, and only then
        linked into place: a crash at any point leaves either no lock or a whole
        one, and at most a temporary that the next raise removes under the lock.
        """

        created = self._create_temporary(directory_descriptor, target)
        if created is None:
            return False
        temporary_descriptor, temporary_name = created
        try:
            try:
                os.fsync(temporary_descriptor)
            finally:
                self._close(temporary_descriptor)
            try:
                os.link(
                    temporary_name,
                    name,
                    src_dir_fd=directory_descriptor,
                    dst_dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
            except FileExistsError:
                pass  # another first raise linked its lock first; use that one
            except FileNotFoundError:
                # The temporary is gone: a raise holding the lock removed it, so
                # the lock exists. The open that follows finds out.
                pass
            self._unlink_temporary(directory_descriptor, temporary_name)
            os.fsync(directory_descriptor)
        except OSError:
            self._unlink_temporary(directory_descriptor, temporary_name)
            return False
        return True

    def _safe_regular_file(self, descriptor: int) -> bool:
        info = os.fstat(descriptor)
        return (
            stat.S_ISREG(info.st_mode)
            and info.st_uid == self._owner_uid
            and stat.S_IMODE(info.st_mode) == _FILE_MODE
        )

    def _read_floor(self, directory_descriptor: int, name: str) -> _FloorRead:
        try:
            descriptor = os.open(name, _READ_FLAGS, dir_fd=directory_descriptor)
        except FileNotFoundError:
            return _ABSENT_FLOOR
        except OSError:
            return _INVALID_FLOOR
        try:
            try:
                info = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != self._owner_uid
                    or stat.S_IMODE(info.st_mode) != _FILE_MODE
                    or info.st_size > _MAX_FLOOR_BYTES
                ):
                    return _INVALID_FLOOR
                raw = self._read_at_most(descriptor, _MAX_FLOOR_BYTES)
            except OSError:
                return _INVALID_FLOOR
        finally:
            self._close(descriptor)
        if raw is None or len(raw) < 2 or not raw.endswith(b"\n") or not raw[:-1].isdigit():
            return _INVALID_FLOOR
        try:
            value = int(raw[:-1])
        except ValueError:
            return _INVALID_FLOOR
        return _FloorRead(_FloorKind.VALUE, value)

    # ------------------------------------------------------------ bounded locking

    @staticmethod
    def _take_lock(descriptor: int, operation: int, wait_seconds: float) -> bool:
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
                return True
            except (BlockingIOError, InterruptedError):
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    return False
                time.sleep(min(_LOCK_POLL_SECONDS, remaining))
            except OSError:
                return False

    # ------------------------------------------------------------ atomic replacement

    @staticmethod
    def _temporary_prefix(target: str) -> str:
        # A leading dot cannot collide with a target filename, and ``@`` cannot
        # occur inside an identifier, so one target's prefix cannot contain another's.
        return f".apexai-fence-tmp@{target}@"

    def _remove_abandoned_temporaries(self, directory_descriptor: int, target: str) -> bool:
        prefix = self._temporary_prefix(target)
        try:
            names = os.listdir(directory_descriptor)
        except OSError:
            return False
        removed = False
        for name in names:
            if not name.startswith(prefix):
                continue
            try:
                os.unlink(name, dir_fd=directory_descriptor)
                removed = True
            except FileNotFoundError:
                continue
            except OSError:
                return False
        if removed:
            try:
                os.fsync(directory_descriptor)
            except OSError:
                return False
        return True

    def _create_temporary(self, directory_descriptor: int, target: str) -> tuple[int, str] | None:
        name = f"{self._temporary_prefix(target)}{secrets.token_hex(16)}"
        try:
            descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _CLOSE_ON_EXEC,
                _FILE_MODE,
                dir_fd=directory_descriptor,
            )
        except OSError:
            return None
        try:
            os.fchmod(descriptor, _FILE_MODE)
            if not self._safe_regular_file(descriptor):
                self._close(descriptor)
                self._unlink_temporary(directory_descriptor, name)
                return None
        except OSError:
            self._close(descriptor)
            self._unlink_temporary(directory_descriptor, name)
            return None
        return descriptor, name

    @staticmethod
    def _write_all(descriptor: int, raw: bytes) -> bool:
        written = 0
        while written < len(raw):
            try:
                count = os.write(descriptor, raw[written:])
            except InterruptedError:
                continue
            except OSError:
                return False
            if count <= 0:
                return False
            written += count
        return True

    @staticmethod
    def _read_at_most(descriptor: int, limit: int) -> bytes | None:
        chunks: list[bytes] = []
        size = 0
        while size <= limit:
            try:
                chunk = os.read(descriptor, limit + 1 - size)
            except InterruptedError:
                continue
            except OSError:
                return None
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                return None
        return None

    @staticmethod
    def _unlink_temporary(directory_descriptor: int, name: str) -> None:
        with suppress(OSError):
            os.unlink(name, dir_fd=directory_descriptor)

    @staticmethod
    def _close(descriptor: int) -> None:
        with suppress(OSError):
            os.close(descriptor)


__all__ = ["FenceStale", "FenceUnavailable", "FileFence"]
