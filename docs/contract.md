# The host contract, version 1

The engine asks a host two kinds of things, and the host offers one kind of thing back. A host may also decide
operator acts on its store (the last section).

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
- **Never touch the engine's own state.** Never open the engine's database or its evidence files, and
  never read the engine through its inspection interface, from inside one of these calls. A call can run
  in a forked child of the engine, where opening the engine's database is unsafe for the engine.
- **Never raise for the target's sake.** An exception is an error, never a finding. When you
  cannot tell, return `UNOBSERVABLE` with a closed reason code.
- **Never guess.** Only `OBSERVED_BROKEN` can lead to a decision. A value you do not recognise is
  `UNOBSERVABLE`, not broken and not healthy.
- **Watch your source too.** `UNOBSERVABLE` never raises attention, so a target whose every look
  is unobservable is silent. If your source can stop answering, give it its own predicate that is
  conclusively broken when the source is stale or gone (a status file older than its bound, say),
  and declare a watch for it.
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
monitor, and stops watching a watch removed from the set. `max_unconfirmed_retirements`
(default 1) counts watches, not targets: when one start would remove more than that, the engine
removes none, and they stay watched unless an `inventory_confirmation_authorizer` confirms them
("Operator acts", below). Set it to the
number of watches you declare per target. The engine refuses to start a monitor whose set is
empty; leaving `watches` out declares nothing.
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

`offer` runs inside the engine's time bound like an adapter's members, under the same rules: bounded,
no thread, no connection kept, and never the engine's own state.

## Operator acts: `OperatorDecision` and the four authorizers

Some acts on a monitor's store are a person's, not the monitor's: retiring one of the engine's decisions about a
target, acknowledging a notice, clearing a remedy's recorded failure, and confirming a retirement of more watches
than `max_unconfirmed_retirements` allows. You decide each one; the engine records only who decided. Give
`read_only_host_wiring` an authorizer for each act you allow; without one, that act is refused.

| Argument | Its one method | Asked with |
|---|---|---|
| `retirement_authorizer` | `authorize_retirement` | `actor_id`, `target_id`, `work_id` |
| `acknowledgement_authorizer` | `authorize_acknowledgement` | `actor_id`, `target_id`, `work_id` |
| `clearance_authorizer` | `authorize_clearance` | `actor_id`, `target_id`, `work_id` |
| `inventory_confirmation_authorizer` | `authorize_inventory_retirement` | `actor_id`, `source_id`, `inventory_generation`, `retirement_count` |

Each method answers an `OperatorDecision(authorized, authorizer_id, actor_kind="human")`, that class itself (a
subclass is refused):

- `authorized`: a `bool`;
- `authorizer_id`: the authority that decided (your policy, your rota), never the person; a short lowercase label,
  because it is recorded and shown: lowercase letters and digits, starting with a letter, parts joined by single
  hyphens (`oncall-policy`, `ops-2`), at most 32 characters, and never starting with `sdk-`, which the SDK's own
  attributions use;
- `actor_kind`: the kind of the actor who asked (the question's `actor_id`), `human` or `service`.

Anything else your method returns is a refusal. **If your method raises, the act fails and nothing is done**; the
engine records only the exception's class, never its text. An authorizer must be an instance, not a class.

**How an authorizer runs.** Unlike an adapter's members, it runs in the engine's own process, while the engine
holds the lock that every other act and every autonomous cycle waits on, and the engine does not bound the call.
So answer promptly from what your system already holds: collect a person's approval before the act is requested,
and only look it up here; never wait on a person or a network call that can hang; never start a thread; and never
open the engine's database or its evidence, or read the engine through its inspection interface, from inside the
method.

The inventory confirmation's arguments: `source_id` is the inventory source whose watches would go (below);
`inventory_generation` is the number of the reconciliation that staged the retirement, one more at every start
that reconciles; `retirement_count` is how many watches it would retire. A confirmation applies exactly that
staged retirement.

**Confirm only your own watches.** `authorize_inventory_retirement` is asked with the `source_id` whose watches
would go. A monitor's own source is `engine.watch_source_id(controller_id)`; confirming any other source applies
another monitor's removals, so refuse it unless that is what you mean.

None of them lets the monitor act on a target: its policy still allows nothing. An engine without the slot for an
authorizer you give raises `EngineUnavailable`; `engine.engine_available(authorizers=[...])` asks first.
