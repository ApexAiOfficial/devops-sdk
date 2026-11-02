# The host contract, version 1

The engine asks a host two kinds of things, and the host offers one kind of thing back.

## Targets and predicates

A **target** is something you operate: a service, a store, a queue. A **predicate** is one
yes-or-no question about it ("is it running?", "does its health check pass?"). The engine decides
which targets to watch and how often from the watches you declare. Your adapter only answers the
question it is asked, when it is asked.

## `TargetAdapter`

| Member | Contract |
|---|---|
| `observation_timeout_seconds` | How long one observation may take. The engine may allow less, never more. |
| `observe(target_id, predicate_id)` | A fresh `Observation` of that predicate on that target, now. |
| `target_fingerprint(target_id)` | A stable identifier of what the target currently *is*. Change it when the target is replaced by something different (for example, the same store re-created for another bot). |
| `desired_revision(target_id)` | The revision of the target's desired state, or `0` if your system has none. A new revision is a new decision. |
| `condition_class(observation)` | For a broken observation, the durable name of the condition (`service_down`). |
| `select_remedy(target_id, condition_class)` | A registered `Remedy`, or `None` for detection only. |

Every member runs inside the engine's time bound, possibly in a forked child process:

- **Bounded and read-only.** Never write, never start a thread, never keep a connection open
  between calls. Open what you need when you are called.
- **Never raise for the target's sake.** An exception is an error, never a finding. When you
  cannot tell, return `UNOBSERVABLE` with a closed reason code.
- **Never guess.** Only `OBSERVED_BROKEN` can lead to a decision. A value you do not recognise is
  `UNOBSERVABLE`, not broken and not healthy.
- **Echo nothing.** `detail` holds closed words and small primitives only (at most 4 KiB as JSON).
  Logs, error output, environment and response bodies can hold secrets, and observations may be
  shown to operators.

## `Observation`

| Field | Type | Rule |
|---|---|---|
| `target_id`, `predicate_id`, `source_id` | identifier | `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}` |
| `outcome` | `Outcome` | `observed_healthy`, `observed_broken`, `unobservable`, `error` |
| `observed_at` | timestamp | ISO 8601 in UTC, ending in `Z` (`utc_timestamp()`) |
| `reason_code` | closed word | `[a-z0-9][a-z0-9_.:-]{0,63}`, or empty |
| `target_fingerprint` | identifier or `None` | |
| `duration_ms` | int ≥ 0 | |
| `detail` | mapping | identifier keys, primitive values, ≤ 4096 bytes |

## `Remedy`

`condition_class`, `action_id` and `verification_predicate_id` (identifiers), and
`expected_healthy` (bool). The action must be one the host registered with the engine; the engine
alone decides whether, when and under what authority it runs, and then observes
`verification_predicate_id` to confirm the repair.

## `Watch`

One look the engine owes on cadence: `target_id` and `predicate_id` (identifiers) and
`interval_seconds` (explicit, finite, 1 to 86400). A monitor passes its **complete** set to
`read_only_host_wiring(..., watches=[...])`. At start the engine declares each watch as that
monitor, and stops watching a target removed from the set, at most `max_unconfirmed_retirements`
(default 1) without confirmation. An empty set is a set; leaving `watches` out declares nothing.
A watch grants nothing: it never authorizes a remedy, and the engine may refuse an interval its
deployment does not allow.

## Attention: `AttentionSink` and `AttentionNotice`

When a decision needs a person (a blocker, a recovery that could not finish, or a decision that
has not settled and keeps failing: `repeated_failure`), the engine offers an
`AttentionNotice` to your sink: identifiers, closed words and timestamps only, never adapter text.
Its `settled_at` is when the decision settled. It is absent (`None`) while the decision has not
settled, as for `repeated_failure`: such a notice is about something still happening.

`offer(payload)` returns `accepted`, `refused` or `failed`:

- `accepted`: delivered, or already delivered;
- `refused`: can never be delivered; not offered again;
- `failed`: offered again later. So is any other answer, an exception, or a timeout.

Offers are **at least once**: the same notice can arrive again after a crash. De-duplicate on
`notification_id` (`ntf_` followed by hex digits) in durable storage, not memory.
`AttentionNotice.from_payload` reads the notice from the engine's payload, a mapping, or any object
with the fields.
