"""A custom adapter in a few lines: a service that writes a heartbeat file.

The service touches ``<root>/<target>.heartbeat`` (a JSON object with a
``beat_at`` epoch-seconds value) while it is alive. The adapter calls a target
broken when the heartbeat is older than ``stale_after_seconds``, and
unobservable when the file cannot be read. Run its contract check with
``pytest examples/custom_adapter.py``.
"""

from __future__ import annotations

import time
from pathlib import Path

from apexai_devops_sdk import Observation, Outcome, Remedy, utc_timestamp
from apexai_devops_sdk.statusfile import read_json_status
from apexai_devops_sdk.testing import assert_target_adapter

PREDICATE = "heartbeat_fresh"
CONDITION = "heartbeat_stale"


class HeartbeatAdapter:
    observation_timeout_seconds = 1.0

    def __init__(self, root: Path, targets: list[str], *, stale_after_seconds: float = 60.0):
        self._root, self._targets, self._stale = Path(root), set(targets), stale_after_seconds

    def observe(self, target_id: str, predicate_id: str) -> Observation:
        def result(outcome: Outcome, reason: str) -> Observation:
            return Observation(
                target_id=target_id, predicate_id=predicate_id, outcome=outcome,
                observed_at=utc_timestamp(), source_id="heartbeat_file", reason_code=reason,
            )  # fmt: skip

        if target_id not in self._targets or predicate_id != PREDICATE:
            return result(Outcome.UNOBSERVABLE, "not_watched")
        read = read_json_status(self._root / f"{target_id}.heartbeat", prefix="heartbeat")
        beat = None if read.document is None else read.document.get("beat_at")
        if not isinstance(beat, (int, float)) or isinstance(beat, bool):
            return result(Outcome.UNOBSERVABLE, read.refusal or "heartbeat_malformed")
        if time.time() - beat > self._stale:
            return result(Outcome.OBSERVED_BROKEN, CONDITION)
        return result(Outcome.OBSERVED_HEALTHY, "heartbeat_fresh")

    def target_fingerprint(self, target_id: str) -> str:
        return f"heartbeat-{target_id}"

    def desired_revision(self, target_id: str) -> int:
        return 0

    def select_remedy(self, target_id: str, condition_class: str) -> Remedy | None:
        return None  # detection and attention only

    def condition_class(self, observation: Observation) -> str:
        return CONDITION


def test_the_heartbeat_adapter_meets_the_contract(tmp_path: Path) -> None:
    beat = tmp_path / "api.heartbeat"
    beat.write_text('{"beat_at": 0}', encoding="utf-8")  # long stale
    beat.chmod(0o644)  # a status file only its writer can change
    adapter = HeartbeatAdapter(tmp_path, ["api"])
    assert adapter.observe("api", PREDICATE).outcome is Outcome.OBSERVED_BROKEN
    assert_target_adapter(adapter, targets=["api"], predicates=[PREDICATE])
