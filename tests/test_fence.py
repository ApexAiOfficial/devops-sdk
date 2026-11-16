"""The file-backed target fence, including its filesystem and lock boundaries."""

from __future__ import annotations

import fcntl
import math
import os
import stat
import threading
from pathlib import Path
from typing import Any

import pytest

import apexai_devops_sdk.fence as fence_module
from apexai_devops_sdk import ContractError
from apexai_devops_sdk.fence import FenceStale, FenceUnavailable, FileFence

TARGET = "store-1"
OWNER = os.geteuid()


@pytest.fixture(autouse=True)
def _mountinfo_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the fence's only filesystem-type source at this test's own file."""

    path = tmp_path / "mountinfo"
    monkeypatch.setattr(fence_module, "_MOUNTINFO_PATH", str(path))
    return path


def _write_mountinfo(path: Path, mounted: Path, filesystem: str = "ext4") -> Path:
    device = mounted.stat().st_dev
    path.write_text(
        f"20 1 {os.major(device)}:{os.minor(device)} / / rw - {filesystem} device rw\n",
        encoding="utf-8",
    )
    return path


def _signal_first_wait(monkeypatch: pytest.MonkeyPatch, thread_name: str) -> threading.Event:
    """An Event set when the named thread first finds the lock taken."""

    blocked = threading.Event()
    real_flock = fcntl.flock

    def flock(descriptor: int, operation: int) -> None:
        try:
            real_flock(descriptor, operation)
        except BlockingIOError:
            if threading.current_thread().name == thread_name:
                blocked.set()
            raise

    monkeypatch.setattr(fence_module.fcntl, "flock", flock)
    return blocked


def _fence(
    tmp_path: Path,
    *,
    filesystem: str = "ext4",
    raise_wait: float = 0.5,
    commit_wait: float = 0.5,
) -> tuple[FileFence, Path, Path]:
    directory = tmp_path / "fence"
    directory.mkdir()
    directory.chmod(0o2750)
    mountinfo = _write_mountinfo(tmp_path / "mountinfo", directory, filesystem)
    return (
        FileFence(
            directory,
            owner_uid=OWNER,
            raise_wait_seconds=raise_wait,
            commit_wait_seconds=commit_wait,
        ),
        directory,
        mountinfo,
    )


def _refuse_reading(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Make every open of ``name`` inside a directory descriptor fail with EACCES."""

    real_open = os.open

    def refusing_open(
        path: Any, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        if dir_fd is not None and os.fspath(path) == name:
            raise PermissionError(13, "Permission denied", name)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", refusing_open)


def _write_file(path: Path, raw: bytes, mode: int = 0o640) -> None:
    path.write_bytes(raw)
    path.chmod(mode)


def _replace_uid(info: os.stat_result, uid: int) -> os.stat_result:
    fields = list(info)
    fields[stat.ST_UID] = uid
    return os.stat_result(fields)


def _make_fstat_report_foreign(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    real_fstat = os.fstat
    expected = str(path)

    def altered_fstat(descriptor: int) -> os.stat_result:
        info = real_fstat(descriptor)
        try:
            opened = os.readlink(f"/proc/self/fd/{descriptor}")
        except OSError:
            return info
        return _replace_uid(info, OWNER + 1) if opened == expected else info

    monkeypatch.setattr(os, "fstat", altered_fstat)


@pytest.mark.parametrize("filesystem", ["ext4", "xfs"])
def test_a_safe_local_directory_has_fence_capability(tmp_path: Path, filesystem: str) -> None:
    fence, _, _ = _fence(tmp_path, filesystem=filesystem)

    assert fence.fence_capability(TARGET)


def test_fence_errors_are_contract_errors() -> None:
    assert issubclass(FenceUnavailable, ContractError)
    assert issubclass(FenceStale, ContractError)


def test_a_commit_at_the_floor_runs(tmp_path: Path) -> None:
    fence, _, _ = _fence(tmp_path)
    assert fence.publish_fence(TARGET, 7)
    committed: list[str] = []

    with fence.committing(TARGET, 7):
        committed.append("yes")

    assert committed == ["yes"]


def test_committing_logs_nothing(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    fence, _, _ = _fence(tmp_path)
    assert fence.publish_fence(TARGET, 1)
    caplog.clear()

    with fence.committing(TARGET, 1):
        pass

    assert caplog.records == []


def test_a_commit_below_the_floor_is_stale(tmp_path: Path) -> None:
    fence, _, _ = _fence(tmp_path)
    assert fence.publish_fence(TARGET, 7)

    with pytest.raises(FenceStale), fence.committing(TARGET, 6):
        raise AssertionError("a stale block ran")


def test_a_commit_above_the_floor_is_stale(tmp_path: Path) -> None:
    fence, _, _ = _fence(tmp_path)
    assert fence.publish_fence(TARGET, 7)

    with pytest.raises(FenceStale), fence.committing(TARGET, 8):
        raise AssertionError("a stale block ran")


def test_a_commit_with_a_lock_but_no_floor_is_stale(tmp_path: Path) -> None:
    fence, directory, _ = _fence(tmp_path)
    _write_file(directory / f"{TARGET}.lock", b"")

    with pytest.raises(FenceStale), fence.committing(TARGET, 1):
        raise AssertionError("a floorless block ran")


def test_a_commit_before_the_first_raise_is_unavailable(tmp_path: Path) -> None:
    fence, _, _ = _fence(tmp_path)

    with pytest.raises(FenceUnavailable), fence.committing(TARGET, 1):
        raise AssertionError("an unavailable block ran")


def test_a_raise_never_lowers_and_equal_is_already_published(tmp_path: Path) -> None:
    fence, _, _ = _fence(tmp_path)

    assert fence.publish_fence(TARGET, 10)
    assert not fence.publish_fence(TARGET, 9)
    assert fence.fence_floor(TARGET) == 10
    assert fence.publish_fence(TARGET, 10)
    assert fence.fence_floor(TARGET) == 10


@pytest.mark.parametrize("damage", ["unreadable", "oversized", "malformed"])
def test_a_raise_writes_nothing_over_a_damaged_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    fence, directory, _ = _fence(tmp_path)
    assert fence.publish_fence(TARGET, 1)
    floor = directory / f"{TARGET}.floor"
    before = floor.read_bytes()
    if damage == "oversized":
        _write_file(floor, b"9" * 32 + b"\n")
        before = floor.read_bytes()
    else:
        _write_file(floor, b"not-a-floor\n")
        before = floor.read_bytes()
    before_mode = stat.S_IMODE(floor.stat().st_mode)
    if damage == "unreadable":
        _refuse_reading(monkeypatch, floor.name)

    assert not fence.publish_fence(TARGET, 2)
    assert stat.S_IMODE(floor.stat().st_mode) == before_mode
    assert fence.fence_floor(TARGET) is None
    monkeypatch.undo()
    assert floor.read_bytes() == before


def test_a_raise_waits_for_a_commit_and_new_commits_see_the_new_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fence, _, _ = _fence(tmp_path, raise_wait=2.0, commit_wait=1.0)
    assert fence.publish_fence(TARGET, 1)
    raise_blocked = _signal_first_wait(monkeypatch, "raiser")
    commit_entered = threading.Event()
    release_commit = threading.Event()
    raise_started = threading.Event()
    commit_errors: list[BaseException] = []
    raise_results: list[bool] = []

    def commit() -> None:
        try:
            with fence.committing(TARGET, 1):
                commit_entered.set()
                if not release_commit.wait(2.0):
                    raise AssertionError("test did not release commit")
        except BaseException as error:  # noqa: BLE001 - report thread failure in parent
            commit_errors.append(error)

    def raise_floor() -> None:
        raise_started.set()
        raise_results.append(fence.publish_fence(TARGET, 2))

    commit_thread = threading.Thread(target=commit)
    raise_thread = threading.Thread(target=raise_floor, name="raiser")
    commit_thread.start()
    assert commit_entered.wait(1.0)
    raise_thread.start()
    assert raise_started.wait(1.0)
    assert raise_blocked.wait(1.0), "the raise never contended with the commit"
    assert fence.fence_floor(TARGET) == 1
    release_commit.set()
    commit_thread.join(2.0)
    raise_thread.join(2.0)

    assert not commit_thread.is_alive() and not raise_thread.is_alive()
    assert commit_errors == [] and raise_results == [True]
    assert fence.fence_floor(TARGET) == 2
    with pytest.raises(FenceStale), fence.committing(TARGET, 1):
        raise AssertionError("the old epoch committed")


def test_two_concurrent_raises_leave_the_higher_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fence, _, _ = _fence(tmp_path, raise_wait=2.0)
    assert fence.publish_fence(TARGET, 1)
    lower_blocked = _signal_first_wait(monkeypatch, "lower")
    higher_holds_lock = threading.Event()
    release_higher = threading.Event()
    lower_started = threading.Event()
    results: dict[int, bool] = {}
    errors: list[BaseException] = []
    real_token_hex = fence_module.secrets.token_hex

    def pause_after_higher_takes_lock(size: int) -> str:
        higher_holds_lock.set()
        if not release_higher.wait(2.0):
            raise AssertionError("test did not release the higher raise")
        return real_token_hex(size)

    monkeypatch.setattr(fence_module.secrets, "token_hex", pause_after_higher_takes_lock)

    def publish(epoch: int) -> None:
        try:
            if epoch == 2:
                lower_started.set()
            results[epoch] = fence.publish_fence(TARGET, epoch)
        except BaseException as error:  # noqa: BLE001 - report thread failure in parent
            errors.append(error)

    high = threading.Thread(target=publish, args=(3,), name="higher")
    low = threading.Thread(target=publish, args=(2,), name="lower")
    high.start()
    assert higher_holds_lock.wait(1.0)
    low.start()
    assert lower_started.wait(1.0)
    assert lower_blocked.wait(1.0), "the lower raise never contended with the higher"
    release_higher.set()
    low.join(2.0)
    high.join(2.0)

    assert not low.is_alive() and not high.is_alive()
    assert errors == [] and results == {2: False, 3: True}
    assert fence.fence_floor(TARGET) == 3


def test_two_overlapping_commits_share_the_lock(tmp_path: Path) -> None:
    fence, _, _ = _fence(tmp_path, commit_wait=1.0)
    assert fence.publish_fence(TARGET, 1)
    first_entered = threading.Event()
    second_entered = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def commit(entered: threading.Event) -> None:
        try:
            with fence.committing(TARGET, 1):
                entered.set()
                if not release.wait(2.0):
                    raise AssertionError("test did not release commits")
        except BaseException as error:  # noqa: BLE001 - report thread failure in parent
            errors.append(error)

    first = threading.Thread(target=commit, args=(first_entered,))
    second = threading.Thread(target=commit, args=(second_entered,))
    first.start()
    assert first_entered.wait(1.0)
    second.start()
    assert second_entered.wait(1.0), "the second shared lock waited for the first"
    release.set()
    first.join(2.0)
    second.join(2.0)

    assert not first.is_alive() and not second.is_alive() and errors == []


def test_a_caller_exception_releases_the_commit_lock(tmp_path: Path) -> None:
    fence, _, _ = _fence(tmp_path)
    assert fence.publish_fence(TARGET, 1)

    with pytest.raises(RuntimeError, match="caller failed"), fence.committing(TARGET, 1):
        raise RuntimeError("caller failed")

    assert fence.publish_fence(TARGET, 2)


def test_lock_deadlines_refuse_on_both_sides_without_writing(tmp_path: Path) -> None:
    fence, directory, _ = _fence(tmp_path, raise_wait=0.05, commit_wait=0.05)
    assert fence.publish_fence(TARGET, 1)
    lock = directory / f"{TARGET}.lock"
    descriptor = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    before = {entry.name: entry.read_bytes() for entry in directory.iterdir()}
    try:
        assert not fence.publish_fence(TARGET, 2)
        with pytest.raises(FenceUnavailable), fence.committing(TARGET, 1):
            raise AssertionError("a timed-out commit ran")
    finally:
        os.close(descriptor)

    after = {entry.name: entry.read_bytes() for entry in directory.iterdir()}
    assert after == before and fence.fence_floor(TARGET) == 1


def test_a_raise_starved_by_overlapping_commits_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fence, _, _ = _fence(tmp_path, raise_wait=0.15, commit_wait=0.5)
    assert fence.publish_fence(TARGET, 1)
    raise_blocked = _signal_first_wait(monkeypatch, "raiser")
    first_entered = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()
    release_second = threading.Event()
    raise_started = threading.Event()
    raise_done = threading.Event()
    raise_results: list[bool] = []
    errors: list[BaseException] = []

    def commit(entered: threading.Event, release: threading.Event) -> None:
        try:
            with fence.committing(TARGET, 1):
                entered.set()
                if not release.wait(2.0):
                    raise AssertionError("test did not release a commit")
        except BaseException as error:  # noqa: BLE001 - report thread failure in parent
            errors.append(error)

    def raise_floor() -> None:
        raise_started.set()
        raise_results.append(fence.publish_fence(TARGET, 2))
        raise_done.set()

    first = threading.Thread(target=commit, args=(first_entered, release_first))
    second = threading.Thread(target=commit, args=(second_entered, release_second))
    raiser = threading.Thread(target=raise_floor, name="raiser")
    first.start()
    assert first_entered.wait(1.0)
    raiser.start()
    assert raise_started.wait(1.0)
    assert raise_blocked.wait(1.0), "the raise never contended with the first commit"
    second.start()
    assert second_entered.wait(0.5), "a later shared commit was not admitted"
    release_first.set()
    assert raise_done.wait(1.0)
    release_second.set()
    first.join(2.0)
    second.join(2.0)
    raiser.join(2.0)

    assert not first.is_alive() and not second.is_alive() and not raiser.is_alive()
    assert errors == [] and raise_results == [False]
    assert fence.fence_floor(TARGET) == 1


def test_unsafe_directory_shapes_have_no_capability(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    _write_mountinfo(tmp_path / "mountinfo", tmp_path)
    missing_fence = FileFence(
        missing,
        owner_uid=OWNER,
        raise_wait_seconds=0.2,
        commit_wait_seconds=0.2,
    )
    ordinary = tmp_path / "ordinary"
    _write_file(ordinary, b"not a directory", 0o640)
    ordinary_fence = FileFence(
        ordinary,
        owner_uid=OWNER,
        raise_wait_seconds=0.2,
        commit_wait_seconds=0.2,
    )

    assert not missing_fence.fence_capability(TARGET)
    assert not ordinary_fence.fence_capability(TARGET)


def test_a_linked_fence_directory_has_no_capability(tmp_path: Path) -> None:
    fence, directory, _ = _fence(tmp_path)
    assert fence.fence_capability(TARGET)
    linked = tmp_path / "linked"
    linked.symlink_to(directory, target_is_directory=True)
    through_link = FileFence(
        linked,
        owner_uid=OWNER,
        raise_wait_seconds=0.2,
        commit_wait_seconds=0.2,
    )

    assert not through_link.fence_capability(TARGET)


def test_a_foreign_owned_fence_directory_has_no_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fence, directory, _ = _fence(tmp_path)
    _make_fstat_report_foreign(monkeypatch, directory)

    assert not fence.fence_capability(TARGET)


@pytest.mark.parametrize("mode", [0o750, 0o2770, 0o1750])
def test_a_wrong_mode_group_writable_or_non_setgid_directory_has_no_capability(
    tmp_path: Path, mode: int
) -> None:
    fence, directory, _ = _fence(tmp_path)
    directory.chmod(mode)

    assert not fence.fence_capability(TARGET)


def test_a_group_writable_parent_has_no_capability(tmp_path: Path) -> None:
    fence, _, _ = _fence(tmp_path)
    original_mode = stat.S_IMODE(tmp_path.stat().st_mode)
    try:
        tmp_path.chmod(original_mode | stat.S_IWGRP)
        assert not fence.fence_capability(TARGET)
    finally:
        tmp_path.chmod(original_mode)


def test_a_foreign_owned_parent_has_no_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fence, _, _ = _fence(tmp_path)
    _make_fstat_report_foreign(monkeypatch, tmp_path)

    assert not fence.fence_capability(TARGET)


def test_a_linked_parent_has_no_capability(tmp_path: Path) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir(mode=0o750)
    directory = real_parent / "fence"
    directory.mkdir()
    directory.chmod(0o2750)
    _write_mountinfo(tmp_path / "mountinfo", directory)
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    through_linked_parent = FileFence(
        linked_parent / "fence",
        owner_uid=OWNER,
        raise_wait_seconds=0.2,
        commit_wait_seconds=0.2,
    )

    assert FileFence(
        directory, owner_uid=OWNER, raise_wait_seconds=0.2, commit_wait_seconds=0.2
    ).fence_capability(TARGET)
    assert not through_linked_parent.fence_capability(TARGET)


@pytest.mark.parametrize(
    ("filesystem", "capable"), [("ext4", True), ("xfs", True), ("nfs4", False)]
)
def test_real_mountinfo_lines_with_optional_fields_are_parsed(
    tmp_path: Path, _mountinfo_path: Path, filesystem: str, capable: bool
) -> None:
    fence, directory, _ = _fence(tmp_path)
    device = directory.stat().st_dev
    other = f"{os.major(device) + 1}:{os.minor(device)}"
    _mountinfo_path.write_text(
        "1 0 0:1 / / rw shared:1 - rootfs rootfs rw\n"
        f"22 1 {other} / /boot rw,relatime shared:7 - xfs /dev/vdz1 rw\n"
        f"25 1 {os.major(device)}:{os.minor(device)} /srv /srv\\040data rw,relatime"
        f" shared:12 master:3 - {filesystem} /dev/vdz2 rw,errors=remount-ro\n",
        encoding="utf-8",
    )

    assert fence.fence_capability(TARGET) is capable


def test_a_nonlocal_filesystem_has_no_capability(tmp_path: Path) -> None:
    fence, _, _ = _fence(tmp_path, filesystem="nfs4")

    assert not fence.fence_capability(TARGET)


def test_a_mountinfo_without_the_directory_device_has_no_capability(tmp_path: Path) -> None:
    fence, _, mountinfo = _fence(tmp_path)
    mountinfo.write_text("", encoding="utf-8")

    assert not fence.fence_capability(TARGET)


@pytest.mark.parametrize("filename", [f"{TARGET}.lock", f"{TARGET}.floor"])
def test_a_foreign_owned_lock_or_floor_is_refused_on_both_sides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str
) -> None:
    fence, directory, _ = _fence(tmp_path)
    assert fence.publish_fence(TARGET, 1)
    _make_fstat_report_foreign(monkeypatch, directory / filename)

    assert not fence.publish_fence(TARGET, 2)
    if filename.endswith(".lock"):
        with pytest.raises(FenceUnavailable), fence.committing(TARGET, 1):
            raise AssertionError("a foreign lock was trusted")
    else:
        assert fence.fence_floor(TARGET) is None
        with pytest.raises(FenceStale), fence.committing(TARGET, 1):
            raise AssertionError("a foreign floor was trusted")


@pytest.mark.parametrize("filename", [f"{TARGET}.lock", f"{TARGET}.floor"])
def test_a_wrong_type_lock_or_floor_is_refused(tmp_path: Path, filename: str) -> None:
    fence, directory, _ = _fence(tmp_path)
    if filename.endswith(".floor"):
        _write_file(directory / f"{TARGET}.lock", b"")
    (directory / filename).mkdir()

    assert not fence.publish_fence(TARGET, 1)
    if filename.endswith(".lock"):
        with pytest.raises(FenceUnavailable), fence.committing(TARGET, 1):
            raise AssertionError("a directory was used as a lock")
    else:
        assert fence.fence_floor(TARGET) is None
        with pytest.raises(FenceStale), fence.committing(TARGET, 1):
            raise AssertionError("a directory was used as a floor")


@pytest.mark.parametrize("filename", [f"{TARGET}.lock", f"{TARGET}.floor"])
def test_a_wrong_mode_lock_or_floor_is_refused(tmp_path: Path, filename: str) -> None:
    fence, directory, _ = _fence(tmp_path)
    if filename.endswith(".floor"):
        _write_file(directory / f"{TARGET}.lock", b"")
        _write_file(directory / filename, b"1\n", 0o660)
    else:
        _write_file(directory / filename, b"", 0o660)

    assert not fence.publish_fence(TARGET, 2)
    if filename.endswith(".lock"):
        with pytest.raises(FenceUnavailable), fence.committing(TARGET, 1):
            raise AssertionError("an unsafe lock was trusted")
    else:
        assert fence.fence_floor(TARGET) is None
        with pytest.raises(FenceStale), fence.committing(TARGET, 1):
            raise AssertionError("an unsafe floor was trusted")


def test_commit_descriptors_are_close_on_exec(tmp_path: Path) -> None:
    fence, directory, _ = _fence(tmp_path)
    assert fence.publish_fence(TARGET, 1)
    expected = {str(directory), str(directory / f"{TARGET}.lock")}

    with fence.committing(TARGET, 1):
        held: dict[str, int] = {}
        for raw_descriptor in os.listdir("/proc/self/fd"):
            if not raw_descriptor.isdigit():
                continue
            descriptor = int(raw_descriptor)
            try:
                opened = os.readlink(f"/proc/self/fd/{descriptor}")
            except OSError:
                continue
            if opened in expected:
                held[opened] = descriptor
        assert set(held) == expected
        assert all(not os.get_inheritable(descriptor) for descriptor in held.values())


def test_a_crashed_raise_temporary_is_removed_by_the_next_raise(tmp_path: Path) -> None:
    fence, directory, _ = _fence(tmp_path)
    assert fence.publish_fence(TARGET, 1)
    abandoned = directory / f".apexai-fence-tmp@{TARGET}@crashed"
    other_target = directory / f".apexai-fence-tmp@{TARGET}-other@crashed"
    _write_file(abandoned, b"2\n")
    _write_file(other_target, b"2\n")

    assert fence.publish_fence(TARGET, 1)
    assert not abandoned.exists()
    assert other_target.exists()


@pytest.mark.parametrize("target", ["", "../victim", "a/b", "has space", "a" * 129, 4])
def test_an_invalid_target_is_refused_before_any_file_is_named(tmp_path: Path, target: Any) -> None:
    fence, directory, _ = _fence(tmp_path)
    before = set(directory.iterdir())

    with pytest.raises(ContractError):
        fence.fence_capability(target)
    with pytest.raises(ContractError):
        fence.publish_fence(target, 1)
    with pytest.raises(ContractError):
        fence.fence_floor(target)
    with pytest.raises(ContractError), fence.committing(target, 1):
        raise AssertionError("an invalid target committed")

    assert set(directory.iterdir()) == before


@pytest.mark.parametrize("owner_uid", [True, -1, 1.5, "1", None])
def test_constructor_refuses_an_invalid_owner_uid(tmp_path: Path, owner_uid: Any) -> None:
    with pytest.raises(ContractError):
        FileFence(
            tmp_path / "fence",
            owner_uid=owner_uid,
            raise_wait_seconds=1.0,
            commit_wait_seconds=1.0,
        )


@pytest.mark.parametrize(
    "wait",
    [True, "1", None, 0, -0.1, math.nan, math.inf, -math.inf, 10**400],
)
@pytest.mark.parametrize("field", ["raise_wait_seconds", "commit_wait_seconds"])
def test_constructor_refuses_an_invalid_wait(tmp_path: Path, field: str, wait: Any) -> None:
    arguments: dict[str, Any] = {
        "owner_uid": OWNER,
        "raise_wait_seconds": 1.0,
        "commit_wait_seconds": 1.0,
    }
    arguments[field] = wait

    with pytest.raises(ContractError):
        FileFence(tmp_path / "fence", **arguments)


@pytest.mark.parametrize(
    "arguments",
    [
        {"owner_uid": OWNER, "raise_wait_seconds": 1.0},
        {"owner_uid": OWNER, "commit_wait_seconds": 1.0},
    ],
)
def test_constructor_requires_both_waits(tmp_path: Path, arguments: dict[str, Any]) -> None:
    with pytest.raises(TypeError):
        FileFence(tmp_path / "fence", **arguments)


@pytest.mark.parametrize("epoch", [True, -1, 1.5, "1", 10**32])
def test_an_invalid_epoch_is_a_contract_error_before_writing(tmp_path: Path, epoch: Any) -> None:
    fence, directory, _ = _fence(tmp_path)

    with pytest.raises(ContractError):
        fence.publish_fence(TARGET, epoch)
    with pytest.raises(ContractError), fence.committing(TARGET, epoch):
        raise AssertionError("an invalid epoch committed")

    assert list(directory.iterdir()) == []


def test_the_first_raise_creates_the_decided_layout(tmp_path: Path) -> None:
    fence, directory, _ = _fence(tmp_path)

    assert fence.publish_fence(TARGET, 123)

    lock = directory / f"{TARGET}.lock"
    floor = directory / f"{TARGET}.floor"
    assert lock.is_file() and floor.read_bytes() == b"123\n"
    assert stat.S_IMODE(lock.stat().st_mode) == 0o640
    assert stat.S_IMODE(floor.stat().st_mode) == 0o640
    assert lock.stat().st_uid == OWNER and floor.stat().st_uid == OWNER


@pytest.mark.parametrize("dangling", [False, True])
@pytest.mark.parametrize("filename", [f"{TARGET}.lock", f"{TARGET}.floor"])
def test_a_linked_lock_or_floor_is_refused_on_both_sides(
    tmp_path: Path, filename: str, dangling: bool
) -> None:
    fence, directory, _ = _fence(tmp_path)
    assert fence.publish_fence(TARGET, 1)
    elsewhere = tmp_path / "elsewhere"
    if not dangling:
        _write_file(elsewhere, b"1\n")
    (directory / filename).unlink()
    (directory / filename).symlink_to(elsewhere)

    assert not fence.publish_fence(TARGET, 2)
    assert (directory / filename).is_symlink(), "a link was replaced"
    if filename.endswith(".lock"):
        with pytest.raises(FenceUnavailable), fence.committing(TARGET, 1):
            raise AssertionError("a linked lock was trusted")
    else:
        # A dangling link is not an absent floor: the raise above refused it.
        assert fence.fence_floor(TARGET) is None
        with pytest.raises(FenceStale), fence.committing(TARGET, 1):
            raise AssertionError("a linked floor was trusted")
    if not dangling:
        assert elsewhere.read_bytes() == b"1\n"


def test_a_failed_directory_fsync_after_the_rename_answers_false_and_never_lowers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fence, _, _ = _fence(tmp_path)
    assert fence.publish_fence(TARGET, 1)
    renamed = threading.Event()
    real_rename = os.rename
    real_fsync = os.fsync

    def rename(*args: Any, **kwargs: Any) -> None:
        real_rename(*args, **kwargs)
        renamed.set()

    def fsync(descriptor: int) -> None:
        if renamed.is_set() and stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(5, "Input/output error")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "rename", rename)
    monkeypatch.setattr(os, "fsync", fsync)

    assert not fence.publish_fence(TARGET, 2)
    assert fence.fence_floor(TARGET) == 2, "the replacement was visible before the failure"
    monkeypatch.undo()
    assert fence.publish_fence(TARGET, 2)
    assert fence.fence_floor(TARGET) == 2


def test_every_open_the_fence_makes_refuses_links_and_closes_on_exec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # os.get_inheritable cannot tell (PEP 446 makes every os.open non-inheritable),
    # so the flags the fence passes are what is checked.
    fence, _, _ = _fence(tmp_path)
    opened: list[tuple[str, int]] = []
    real_open = os.open

    def recording_open(
        path: Any, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        opened.append((os.fspath(path), flags))
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", recording_open)
    assert fence.publish_fence(TARGET, 1)
    assert fence.publish_fence(TARGET, 2)
    with fence.committing(TARGET, 2):
        pass
    assert fence.fence_floor(TARGET) == 2
    monkeypatch.undo()

    assert opened
    for path, flags in opened:
        assert flags & os.O_CLOEXEC, path
        assert flags & os.O_NOFOLLOW, path
    directories = [path for path, flags in opened if flags & os.O_DIRECTORY]
    assert str(tmp_path) in directories and "fence" in directories


def test_the_lock_never_exists_with_another_mode_under_a_strict_umask(tmp_path: Path) -> None:
    fence, directory, _ = _fence(tmp_path)
    previous = os.umask(0o077)
    try:
        assert fence.publish_fence(TARGET, 1)
    finally:
        os.umask(previous)

    names = {entry.name for entry in directory.iterdir()}
    assert names == {f"{TARGET}.lock", f"{TARGET}.floor"}
    for name in names:
        assert stat.S_IMODE((directory / name).stat().st_mode) == 0o640, name
    with fence.committing(TARGET, 1):
        pass


def test_lock_temporaries_left_by_a_crash_are_removed_by_the_next_raise(tmp_path: Path) -> None:
    fence, directory, _ = _fence(tmp_path)
    # Crashed before the link: a temporary with the umask's mode, and no lock.
    unlinked = directory / f".apexai-fence-tmp@{TARGET}@beforelink"
    _write_file(unlinked, b"", 0o600)

    assert fence.publish_fence(TARGET, 1)
    assert not unlinked.exists()
    lock = directory / f"{TARGET}.lock"
    assert stat.S_IMODE(lock.stat().st_mode) == 0o640

    # Crashed after the link: the temporary is a second name of the lock itself.
    linked = directory / f".apexai-fence-tmp@{TARGET}@afterlink"
    os.link(lock, linked)
    assert fence.publish_fence(TARGET, 2)
    assert not linked.exists()
    assert lock.stat().st_nlink == 1
    with fence.committing(TARGET, 2):
        pass
