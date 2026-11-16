# Design note: a file-backed target fence (proposal, not built)

**Status:** a proposal for review. Nothing in this note is in the SDK yet, and nothing changes for a read-only
host: a monitor that registers no remedy needs no fence.

## What a fence is for

A remedy changes your target. Your system is the only party that sees that change commit, so it is the one that must
refuse a change that is no longer current. A **fence** is how it does that: a number your system keeps for each
target, the **floor**, which only ever goes up. The remedy's process is handed an epoch for its change, in
`OPS_OWNER_EPOCH`, with the target in `OPS_TARGET_ID`. Epochs only increase: a later change of a target never
carries a lower epoch than an earlier one. Your commit path compares that epoch with the floor, and
commits only when they are equal. Without a fence the engine does not run remedies on that target.

## The host contract

A fence answers three questions for a target identifier:

| Question | Answer |
|---|---|
| Can this target hold a floor? | `True` or `False`. `False` means no remedy runs on it. |
| Raise the floor to epoch *N*. | `True` when the floor is now *N*; `False` otherwise, and the floor is unchanged. It never lowers a floor. |
| What is the floor now? | The integer, or `None` when there is none or it cannot be read. |

And at commit: equal to the floor, commit; anything else, refuse.

## The proposal: `FileFence`

For a system whose state already lives in files on one host (a reconciler with a state file per target, a fleet
manager writing status files), the floor can be a file too. Proposed API, in a new module
`apexai_devops_sdk.fence`:

```python
fence = FileFence(
    "/var/lib/apexai-fence",        # a directory neither account's state directory contains
    owner_uid=ENGINE_UID,           # the account that raises floors; anything else owning a file is refused
    raise_wait_seconds=2.0,
    commit_wait_seconds=2.0,
)

# Given to the engine as the host's fence:
fence.fence_capability(target_id) -> bool
fence.publish_fence(target_id, epoch) -> bool
fence.fence_floor(target_id) -> int | None

# Your side, around the one place your system commits a change of that target:
with fence.committing(target_id, epoch):     # epoch: int(os.environ["OPS_OWNER_EPOCH"])
    apply_the_change()
```

### On disk

- **One directory**, created by the deployment, owned by `owner_uid`, group your system's account, mode `2750`
  (setgid, so every file in it takes that group). Its **parent must not be writable by your system's account**:
  whoever can write the parent can rename the directory away and put another in its place. So it lives outside
  both accounts' state directories (for example `/var/lib/apexai-fence`, created by root).
- **A local filesystem only** (ext4, xfs). `flock` on NFS is emulated with byte-range locks and changes meaning.
  The directory's filesystem type is read from `/proc/self/mountinfo`, on the line whose device (`major:minor`)
  equals the directory's `st_dev`, and `fence_capability` answers `False` for any type but `ext4` or `xfs`, or when
  no line matches.
- `fence_capability` also answers `False` when the directory is missing, a link, not owned by `owner_uid`, not of
  that mode, or writable by anyone but its owner; or when its **parent** is owned by anyone but root or `owner_uid`,
  or is writable by its group or others. The SDK never creates or widens either.
- **Per target, two files**, both `0640`, owned by `owner_uid`:
  - `<target>.lock`: created by the first raise with `O_CREAT | O_EXCL`, then the directory `fsync`ed; never
    replaced or removed;
  - `<target>.floor`: a decimal integer and a newline, at most 32 bytes, replaced whole (below).
- The target identifier is validated by the contract's identifier rule (`[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`) before
  it names a file; any other value is refused.
- **Every open** uses `O_NOFOLLOW | O_CLOEXEC`; the directory is opened `O_RDONLY | O_DIRECTORY` for its `fsync`. A `flock` belongs to the open file description and is inherited by a
  forked child, so a child that keeps the descriptor keeps the lock: `O_CLOEXEC` keeps it out of an exec'd program,
  and the block passed to `committing` must not fork a process that outlives it.
- A file of the wrong type, owner or mode, or larger than 32 bytes, is a refusal, never a floor.

### Raising the floor (`publish_fence`)

1. Open `<target>.lock` (creating it on the first raise, as above), and take an exclusive `flock` within
   `raise_wait_seconds`, polling with `LOCK_NB`. At the deadline answer `False` with nothing written.
2. Read the floor, capped at 32 bytes. **Only an absent file is "no floor".** A file that cannot be read, is too
   large, or does not hold a decimal integer is a refusal: answer `False` and write nothing, so a damaged floor is
   never replaced by a lower one. If the floor is above `epoch`, answer `False`; if it equals `epoch`, answer `True`.
3. Otherwise write `epoch` to a new file with a unique name (`O_CREAT | O_EXCL`, mode `0640`) in the same directory,
   `fsync` it, `rename` it over `<target>.floor`, `fsync` the directory, release the lock and answer `True`. A
   temporary file left by a crash is removed by the next raise, under the lock.

`fence_floor` reads `<target>.floor` without the lock, capped at 32 bytes (the rename makes every read whole), and
answers `None` for an absent, unreadable or malformed file.

### Committing (`committing`)

1. Open `<target>.lock` read-only, check its owner and mode, and take a **shared** `flock` within
   `commit_wait_seconds`. If the lock file does not exist yet, or the deadline passes, raise `FenceUnavailable`;
   the caller must not commit.
2. Read the floor as `fence_floor` does. Unless it **equals** `epoch`, raise `FenceStale`.
3. Run the caller's block while holding the shared lock; release it on every exit.

Commits share the lock, so they do not wait for each other. A raise that obtains the lock exclusively has waited for
every commit in progress, and every commit that starts after it reads the new floor and refuses the old epoch.

**Starvation.** Linux `flock` has no fairness: while commits keep overlapping, a shared request can be granted ahead
of a waiting exclusive one, so a raise can miss its deadline. It then answers `False`, the floor is unchanged, and no
remedy runs: that fails closed. Keep commit blocks short, and size `raise_wait_seconds` for the longest one.

## What it does not do

- It fences only what runs inside `committing`. A part of a change made before the block, or by a process that does
  not use the fence, is unfenced. Keep the block to the commit itself.
- It is one host's fence, on one local filesystem. Several hosts changing one target need a shared floor (a database
  row, a lease service) answering the same three questions.
- It makes nothing atomic but the floor check against the raise. The change inside the block is your system's to make
  safe against a crash.
- Deleting the directory makes every floor "none": the next raise writes it again, and a commit before that raise
  refuses. Do not delete it while remedies may run.

## Tests the module would ship with (engine-free)

- a commit at the floor's epoch runs; one below, above, or with no floor raises `FenceStale`;
- a commit before the first raise (no `<target>.lock`) raises `FenceUnavailable`;
- a raise never lowers a floor, answers `True` for an equal one, and answers `False` with nothing written for a
  floor file that is unreadable, oversized or malformed;
- a raise waits for a commit in progress, and a commit started after the raise refuses the old epoch;
- two concurrent raises to different epochs leave the higher one, and the lower raise answers `False`;
- two overlapping commits at the floor's epoch both run, neither waiting for the other;
- the lock is released when the caller's block raises, and a raise after it obtains the lock;
- a lock not obtained within its wait refuses on both sides, and nothing is written; a raise starved by overlapping
  commits answers `False`;
- a link, a wrong owner or mode, a group-writable or non-setgid directory, a writable or foreign-owned parent, or a
  filesystem type other than `ext4` or `xfs` makes `fence_capability` `False`;
- a lock file or floor file owned by another account is a refusal on both sides;
- a descriptor is not inherited by an exec'd child (`O_CLOEXEC`);
- a temporary file left by a crashed raise is removed by the next raise;
- a target identifier outside the contract's rule is refused before any file is named.

## Open for review

- The module and class names, and whether `committing` should log the target and epoch it admitted.
- Whether the two waits need defaults, or should be required.
- The first product to use it, and which of its changes sits inside one commit block.
