# Owner-fork upstream merge procedure

This repository merges `NousResearch/hermes-agent` into
`novkien/hermes-agent` through an ordinary two-parent merge. The workflow is
deliberately local and review-driven; GitHub Actions validates candidates but
does not create, update, close, or merge pull requests.

## Immutable inputs and recovery

Fetch and capture exact SHAs before changing source:

```bash
git fetch --prune origin master
git fetch --no-tags https://github.com/NousResearch/hermes-agent.git \
  main:refs/remotes/upstream/main
OWNER_BASE=$(git rev-parse refs/remotes/origin/master)
UPSTREAM_TIP=$(git rev-parse refs/remotes/upstream/main)
```

Create and publish a uniquely named backup ref from `OWNER_BASE`, then create
an isolated integration worktree. Never perform the merge in the production
checkout, and never use a moving `upstream/main` name after capturing
`UPSTREAM_TIP`.

## Isolate fork test deltas before merging

Run the planner and review every selected path:

```bash
python scripts/fork_ci/isolate_fork_tests.py \
  --owner-ref "$OWNER_BASE" \
  --upstream-ref "$UPSTREAM_TIP"
```

Then run it again with `--apply` and commit the result. The shared test surface
is restored to the common base while owner-modified and owner-only tests are
kept as executable fork regression cases under `fork_tests/cases/`. See
`fork_tests/README.md` for the layout and wording contract.

This step prevents test-path conflicts; it does not suppress source conflicts
or reinterpret failing tests. A shared test changed by upstream remains an
upstream test and must pass unmodified after the merge.

The isolation manifest works at semantic node granularity. If the fork changes
shared test support code such as fixtures, imports, helpers, constants, or
non-test class members, inherited common-base nodes run from the fork overlay
so they receive that support context. Exact newly added upstream nodes in the
same file continue to run from the pristine shared surface; never replace a
whole file merely because one support definition changed.

## Merge source semantically

Preview and merge the captured SHA:

```bash
git merge-tree --write-tree HEAD "$UPSTREAM_TIP"
git merge --no-ff --no-commit "$UPSTREAM_TIP"
```

Resolve every conflict by comparing base, owner, and upstream behavior. Record
the path, conflict kind, both behaviors, the composed resolution, tests, and
remaining uncertainty in the pull request. Do not use blanket ours/theirs
strategies.

Before committing the merge, update the shared-test lock to the same immutable
SHA:

```bash
python scripts/fork_ci/update_upstream_lock.py --upstream-ref "$UPSTREAM_TIP"
```

Both `OWNER_BASE` and `UPSTREAM_TIP` must be ancestors of the final candidate.

## Validate and publish

At minimum run:

```bash
python scripts/fork_ci/verify_shared_tests.py
python -m pytest -q fork_tests/test_*.py
python scripts/fork_ci/run_python_cases.py --python .venv/bin/python
python scripts/fork_ci/run_javascript_cases.py
git diff --check "$OWNER_BASE"..HEAD
```

Also run focused tests for every conflict and the maintained full CI lanes
implicated by the upstream delta. Push the integration branch non-force, open
or update its PR, and wait for exact-head review and CI. Merging the child PR,
updating the parent `novkien/hermes` gitlink, running `Sync Hermes`, and proving
runtime code SHA are separate stages.
