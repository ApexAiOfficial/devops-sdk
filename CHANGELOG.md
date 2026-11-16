# Changelog

All notable changes to this SDK. The format follows [Keep a Changelog](https://keepachangelog.com/),
and versions follow [Semantic Versioning](https://semver.org/) (before 1.0, a minor version may
change the API).

## Unreleased

### Added
- `apexai_devops_sdk.fence`: `FileFence`, a target fence kept in files on one host, built as
  `docs/design/file-fence.md` decides. `fence_capability`, `publish_fence` and `fence_floor` answer the fence's three
  questions; your commit path runs inside `committing(target_id, epoch)`, which raises `FenceStale` unless the
  epoch equals the floor and `FenceUnavailable` when the lock cannot be obtained within its wait. Both are
  `ContractError`s. Both waits are required, finite and above zero. Standard library only; it logs nothing.

### Documentation
- `docs/design/file-fence.md`: built. Two rules added while building it: the filesystem check reads only
  `/proc/self/mountinfo`, and a raise whose directory `fsync` fails after the rename answers `False` while the
  floor may already read the new epoch (never a lower one).

## 0.3.0 (2026-11-16)

### Added
- Operator acts: `contract.OperatorDecision` and four authorizer protocols, `RetirementAuthorizer`,
  `AcknowledgementAuthorizer`, `ClearanceAuthorizer` and `InventoryConfirmationAuthorizer`.
  `engine.read_only_host_wiring` takes each as an optional argument of the same name in snake case and passes it to
  the engine in the engine's own type; without one, that act is refused. An answer that is not an
  `OperatorDecision` is a refusal (`sdk-invalid-answer`), and an engine without the slot raises `EngineUnavailable`.
  The policy still allows nothing. See `docs/contract.md`, "Operator acts".

### Changed
- `read_only_host_wiring(watches=())` raises `ContractError` at once: the engine refuses to start a monitor whose
  set is empty, and now the SDK says so before it starts. `None` still declares no inventory.
- `contract.AUTHORIZER_ID` itself bounds a label to 32 characters, as its docstring said.
- `engine.engine_available()` answers `False` for an engine whose wiring module has no `HostWiring`, and raises
  `ContractError`, never `TypeError`, for an `authorizers` entry that is not a slot's name.

### Documentation
- `docs/design/file-fence.md`: the design of a file-backed target fence for a host whose state lives in files on
  one host. Decided, not built; it is built with the first product that needs it.
- How an authorizer runs: in the engine's own process, under the lock every act and cycle waits on, unbounded,
  so it answers from approvals collected beforehand and never opens the engine's state. What the inventory
  confirmation's `inventory_generation` and `retirement_count` are. See `docs/contract.md`, "Operator acts".
- A collaborator never opens the engine's database or evidence, and never reads the engine through its
  inspection interface, inside a call the engine makes to it: such a call can run in a forked child. A sink's
  `offer` follows the adapter rules too. See `docs/contract.md`.
- `max_unconfirmed_retirements` counts watches, not targets. When one start would remove more
  watches than the bound, the engine removes none of them, and they stay watched until an
  `inventory_confirmation_authorizer` confirms the removal (without one, they stay watched). The README, `docs/contract.md` and the docstrings now say so, and
  advise setting the bound to the number of watches declared per target.
- An empty watch set is not reconciled: the engine refuses to start a monitor that declares
  none. The earlier text said it stopped watching everything.
- Adapters: `UNOBSERVABLE` never raises attention, so a source that can stop answering needs a
  predicate of its own that is conclusively broken when the source is stale or gone.

## 0.2.0 (2026-11-02)

### Added
- `contract.Watch`: one look a monitor declares (target, predicate, interval of 1 s to 1 day).
- `engine.read_only_host_wiring(watches=..., max_unconfirmed_retirements=1)`: a monitor declares
  its complete set of watches (an empty set included; `None`, the default, declares none). The
  engine reconciles it once at start, as this monitor, from the monitor's own inventory source
  (`engine.watch_source_id`), and retires watches no longer declared, at most the bound without
  confirmation. Needs an engine that reconciles a host's watch inventory; an older one raises
  `EngineUnavailable`.
- `engine.engine_available(watches=True)` also checks the parts declared watches need.

### Changed
- `telegram.render`: a `repeated_failure` notice is about a decision that has not settled; it now
  reads "is still <state> after repeated failed attempts", and any other notice without
  `settled_at` reads "is <state>, not settled". Neither presents the decision as settled.

## 0.1.1 (2026-10-29)

### Fixed
- `testing.check_target_adapter` reported nothing and raised instead, on Python 3.11, for an
  adapter whose `observation_timeout_seconds` raises: Python 3.11 evaluates properties during
  `isinstance` against a runtime-checkable protocol. The property is now read first and a raise is
  reported as a violation on every supported Python. Found by this repository's CI (3.11 job).

## 0.1.0 (2026-10-29)

First release of the integration layer. Host contract version `1`.

### Added
- `contract`: `Observation`, `Outcome`, `Remedy`, `AttentionNotice`, `SinkOutcome`, the
  `TargetAdapter` and `AttentionSink` protocols, identifier and closed-word rules,
  `utc_timestamp`.
- `compose.ComposeHealthAdapter`: Docker Compose service health through the Docker Engine API.
- `statusfile.read_json_status`: a bounded, link-safe JSON status-file read that refuses a
  file others can write.
- `telegram.TelegramAttentionSink`: at-least-once, de-duplicated attention delivery to Telegram.
- `config`: settings parsing that refuses rather than guesses; owner-only secret files.
- `testing`: `assert_target_adapter` contract checks (repeat calls, the time bound, a forked
  call, synthetic broken looks), `FakeDockerDaemon`, `FakeTelegramApi`.
- `engine`: the run-time bridge to the engine and `read_only_host_wiring` for monitors.
