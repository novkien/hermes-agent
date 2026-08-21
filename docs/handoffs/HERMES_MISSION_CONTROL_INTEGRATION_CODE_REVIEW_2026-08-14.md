# Hermes Mission Control Integration — Code Review Handoff

**Handoff ID:** `HERMES-MISSION-CONTROL-INTEGRATION-CODE-REVIEW-2026-08-14-01`  
**Owner:** Le Kien  
**Repository:** `novkien/hermes-agent`  
**Pull request branch:** `agent/integrate-full-mission-control`  
**Review mode:** `CODE_REVIEW`  
**Merge authority:** Owner / explicitly assigned merge agent only

## Objective

Review the pull request that imports the complete tracked tree of
`novkien/agent-mission-control` into `apps/mission-control/` and makes it a native
Hermes application while preserving an independent runtime process from the Hermes
gateway.

Do not redesign the dashboard, omit imported files, split it back into another
repository, deploy it, or merge the pull request as part of this review. Return
findings and a merge recommendation to the owner.

## Locked owner decisions

1. `novkien/hermes-agent` becomes the canonical source repository for Mission Control.
2. Every tracked file from source commit
   `42a9c191fdebc66ace4aac98a1e581d9ab7a13d1` must exist under
   `apps/mission-control/`; no source file may be silently omitted.
3. The old `novkien/agent-mission-control` repository is source provenance only after
   merge; there is no ongoing subtree/submodule/runtime dependency on it.
4. Mission Control moves off the Pi when the merged code is deployed. This PR does not
   claim deployment or Pi cleanup.
5. Gateway and Mission Control remain separate processes, PIDs, cgroups, logs and
   restart domains.
6. Starting or restarting the gateway must start Mission Control when it is inactive.
7. Restarting Mission Control must not restart or stop the gateway.
8. Restarting the gateway while Mission Control is already active must not restart
   Mission Control.
9. Do not introduce `Requires=`, `PartOf=` or `BindsTo=` coupling between the services.
10. Do not merge this PR during review. The owner will decide after receiving the
    review.

## Source parity review

Verify the import against the locked source commit:

```bash
git fetch https://github.com/novkien/agent-mission-control.git \
  42a9c191fdebc66ace4aac98a1e581d9ab7a13d1

source_commit=42a9c191fdebc66ace4aac98a1e581d9ab7a13d1
source_count=$(git ls-tree -r --name-only "$source_commit" | wc -l)
missing=0
while IFS= read -r path; do
  test -e "apps/mission-control/$path" || {
    printf 'MISSING %s\n' "$path"
    missing=$((missing + 1))
  }
done < <(git ls-tree -r --name-only "$source_commit")

printf 'source_count=%s missing=%s\n' "$source_count" "$missing"
test "$missing" -eq 0
```

Read `apps/mission-control/SOURCE_IMPORT.json` and independently confirm its source
commit, tree SHA, tracked-file count and manifest digest. Additional Hermes integration
files are allowed; missing original tracked files are not.

## Architecture review

Review these surfaces closely:

- `hermes_cli/mission_control.py`
- `hermes_cli/subcommands/mission_control.py`
- `hermes_cli/gateway.py`
- `hermes_cli/main.py`
- `hermes_cli/_parser.py`
- `tests/hermes_cli/test_mission_control.py`
- `apps/mission-control/HERMES_INTEGRATION.md`
- `apps/mission-control/deploy/hermes-mission-control.env.example`
- root `AGENTS.md` and `README.md`

Confirm:

- the active unit is `hermes-mission-control.service`;
- Mission Control runs through `python -m hermes_cli.mission_control`;
- its data files live under the stable Hermes root, outside the Git checkout;
- its dedicated environment file is optional and no secret values are committed;
- the gateway unit has only `Wants=hermes-mission-control.service`;
- gateway start/restart calls Mission Control `start`, never Mission Control `restart`;
- Mission Control lifecycle commands target only `hermes-mission-control`;
- a Mission Control startup failure is visible but does not prevent the gateway from
  starting;
- the old Pi service files are retained only as imported provenance and are not used by
  the Hermes installer/runtime;
- no runtime database, WAL/SHM, `.env`, credential, cookie, log, cache, virtualenv or
  `node_modules` content was imported.

## Required source checks

Run from the `hermes-agent` repository root:

```bash
python -m compileall -q \
  apps/mission-control/agent_mission_control \
  hermes_cli/mission_control.py \
  hermes_cli/subcommands/mission_control.py

(
  cd apps/mission-control
  python tests/test_runtime_contracts.py
  python tests/test_static_repair_surface.py
  node tests/frontend_contracts.mjs
  node tests/skills_surface.mjs
)

python -m pytest -q tests/hermes_cli/test_mission_control.py
git diff --check origin/main...HEAD
```

Also inspect the normal pull-request CI result. Do not waive a failing required check
without identifying the exact failure and whether it is caused by this PR.

## Runtime acceptance plan for post-merge deployment

This review does not claim runtime execution. Confirm that the deployment plan can
prove the following later on the Hermes host:

```text
A. gateway stopped + Mission Control stopped
   hermes gateway start
   => both active, different PIDs/cgroups

B. record gateway PID A and Mission Control PID B
   hermes mission-control restart
   => gateway PID remains A; Mission Control PID changes

C. kill Mission Control PID
   => systemd restarts Mission Control; gateway PID and messaging remain available

D. hermes mission-control stop
   => Mission Control inactive; gateway remains active

E. Mission Control active with PID B
   hermes gateway restart
   => gateway PID changes; Mission Control PID remains B

F. force Mission Control startup failure
   hermes gateway start
   => gateway still becomes active; Mission Control failure is observable
```

## Review output contract

Return:

1. `VERDICT`: `APPROVE`, `REQUEST_CHANGES`, or `BLOCKED_BY_EVIDENCE`.
2. Findings ordered by severity: `CRITICAL`, `HIGH`, `MEDIUM`, `LOW`.
3. For each finding: exact file/path, line or symbol, evidence, impact, and concrete
   remediation.
4. Source parity result with tracked count and missing-file count.
5. Test/CI results with literal command outcomes.
6. Confirmation that no merge or deployment was performed.

Do not report style preferences as blockers unless they create a concrete correctness,
security, lifecycle, maintainability or repository-policy risk.
