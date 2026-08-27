# Fork regression cases

`fork_tests/cases/` contains maintained test sources for behavior that belongs
to `novkien/hermes-agent` but differs from the shared test surface in
`NousResearch/hermes-agent`.

These files are **regression cases, not snapshots**. They are reviewed and
updated like any other test source. Their path below `cases/` mirrors the path
where the test originally ran, because conftest discovery, relative imports,
fixtures, and JavaScript workspace configuration often depend on that layout.

## Why the tests are separated

The normal upstream test paths remain byte-identical to the commit recorded in
`fork/upstream-lock.json`. This gives each upstream merge one deterministic
rule:

- upstream changes its own shared tests without conflicting with owner-only
  assertions;
- owner-specific assertions remain visible and executable under this folder;
- CI runs the pristine shared tests first and the overlaid fork cases second.

`apps/mission-control/tests/` is already an owner-only subsystem and does not
belong to the shared surface, so it stays in place.

## Before an upstream merge

Freeze both inputs, then run the isolation planner from a clean integration
worktree:

```bash
python scripts/fork_ci/isolate_fork_tests.py \
  --owner-ref "$OWNER_BASE" \
  --upstream-ref "$UPSTREAM_TIP"
```

Review the JSON plan. Apply it explicitly:

```bash
python scripts/fork_ci/isolate_fork_tests.py \
  --owner-ref "$OWNER_BASE" \
  --upstream-ref "$UPSTREAM_TIP" \
  --apply
```

Commit that isolation before merging the frozen upstream SHA. The command does
not commit, merge, push, or resolve source conflicts.

After the semantic merge is complete, refresh the shared-test lock:

```bash
python scripts/fork_ci/update_upstream_lock.py --upstream-ref "$UPSTREAM_TIP"
python scripts/fork_ci/verify_shared_tests.py
```

## CI execution

The main Python lane runs, in order:

1. the immutable shared-test verification;
2. the complete normal Python suite;
3. the fork-isolation tooling tests;
4. Python fork cases in a disposable worktree overlay.

The JS/TS lane runs all normal workspace checks, then temporarily overlays
JavaScript fork cases and invokes the affected workspace test scripts. The
checkout is restored in a `finally` block.

The manifest at `fork_tests/manifest.json` records each case's shared path,
case path, runner, selected node IDs, checksum, and JavaScript workspace when
applicable. A changed checksum is a hard error; update the manifest through the
isolation command instead of editing metadata by hand.
