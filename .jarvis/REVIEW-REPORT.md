# Review Report: ComfyUI OOM detection fix — poll_history condition

**Operation:** fix-defect | Size: standard  
**Reviewer:** reviewer profile

---

## Summary

Two source files reviewed:
- **Runner** — `~/.hermes/skills/creative/comfyui-general/scripts/run_nsfw_via_generate.py`
- **Proxy** — `/home/pi/llama-proxy/src/llama_proxy/comfyui_proxy.py` (SSH)
- **Tests** — `test_run_nsfw.py` (20 tests, all pass)

---

## Condition logic audit

| Scenario | completed | status_str | old (`and`) | new (`or`) | Notes |
|----------|-----------|-----------|-------------|------------|-------|
| Normal success | True | "success" | ✅ match | ✅ match | unchanged |
| Normal error | True | "error" | ✅ match | ✅ match | unchanged |
| **OOM** | **False** | **"error"** | **❌ miss** | **✅ match** | **the fix** |
| Still running | False | "" | ❌ miss | ❌ miss | unchanged |
| Weird state | True | "" | ❌ miss | ✅ match | see below |

The `completed is True or status_str == "error"` fix correctly catches the OOM case where `completed=False, status_str="error"`. The Weird-state case (`completed=True, status_str=""`) is a speculative edge case not known to occur in ComfyUI's API — when `completed=True`, ComfyUI always sets a meaningful `status_str`. In any case, returning the entry on a non-error terminal state is safe: `find_outputs` handles it normally.

**Verdict on condition logic:** ✅ correct.

---

## OOM idle timer fix (proxy)

`_poll_history_until_done` at `/home/pi/llama-proxy/src/llama_proxy/comfyui_proxy.py:485`:

- **Before**: `IDLE_TIMER.reset()` at the start of each loop iteration (before the `if not entry: continue` guard). This reset the proxy's idle shutdown timer on every poll interval, even when polling a prompt not yet in history.
- **After**: `IDLE_TIMER.reset()` moved to inside the entry-found branch, right before the completion check.

This prevents the proxy's idle shutdown timer from being kept alive by empty poll cycles — correct behaviour. **Verdict:** ✅

---

## Findings

### [HIGH] OOM error swallowed by main() — wrong JSON output on stdout

**File:** `run_nsfw_via_generate.py:354-366`  
**Trigger:** ComfyUI OOM during generation  
**Failure mode:**

When `poll_history()` detects OOM (line 107), it returns `{"error": "ComfyUI OOM: CUDA out of memory..."}`. However, `main()` (lines 353-366) does not check for this error dict:

```python
entry = poll_history(pid, timeout=poll_timeout)   # returns {"error": "OOM: ..."}
if not entry:                                      # truthy dict → no match
    ...
status = entry.get("status", {}).get("status_str", "unknown")
# → "unknown" (no "status" key in error dict)

outputs = find_outputs(entry)                      # entry has no "outputs" → []
if not outputs:
    print(json.dumps({"error": "no output images found in history"}))
    return 1
```

**Outcome:** The JSON printed to stdout says `"no output images found in history"` instead of `"ComfyUI OOM: ..."`. The OOM message IS logged to stderr (`OOM_DETECTED: ...` on line 106), but any tooling/automation that parses stdout JSON (which is the contract of this script) receives a misleading error message. This could cause wasteful retries (same parameters → same OOM) instead of prompting the user to free VRAM or reduce batch size.

**Fix:** Add an explicit check after `poll_history()` returns:

```python
entry = poll_history(pid, timeout=poll_timeout)
if not entry:
    print(json.dumps({"error": f"poll timeout for prompt_id={pid}"}))
    return 1
if "error" in entry:
    print(json.dumps(entry))
    return 1
```

Place this before the `status` reassignment on line 359.

**Severity:** HIGH — the OOM detection code works correctly at the low level, but its output is discarded before reaching the user. The feature's primary purpose (telling callers "this failed because OOM") is defeated.

---

### [MEDIUM] Non-OOM error also returns error dict (inherited concern)

**File:** `run_nsfw_via_generate.py:98-108`  
**Issue:** When a non-OOM error has `status_str="error"` but no `execution_error` messages, or the error type/message is not OOM-related, `poll_history` returns the raw entry dict. This is correct behaviour. When it IS an OOM, it returns `{"error": ...}`. The caller (`main()`) has no type discriminator for the two return shapes — a function that sometimes returns `{"status": {...}, "outputs": {...}}` and sometimes returns `{"error": "..."}`.

This is not a new defect (it's by design), but it makes the error-handling more fragile. The recommended fix above (checking `"error" in entry`) resolves this.

---

## Test coverage audit

| Area | Status |
|------|--------|
| OOM with `completed=False, status_str="error"` | ✅ `test_oom_detected_when_completed_false_status_str_error` |
| OOM message parsing | ✅ `test_oom_message_parsing_detected` |
| Non-OOM error passes through | ✅ `test_non_oom_error_passes_through` |
| Normal success | ✅ `test_poll_returns_success_entry` |
| Normal error (completed=True) | ✅ `test_poll_returns_error_entry_when_completed_true` |
| HTTP retries | ✅ `test_poll_retries_on_http_exception` |
| Empty/None responses | ✅ `test_poll_returns_none_on_empty_data`, `test_poll_returns_none_on_timeout_no_entry` |
| **main() OOM propagation** | **❌ Not tested** — `test_oom_message_parsing_detected` tests `poll_history` only |
| `find_outputs` variations | ✅ 3 tests covering 0/1/3 images |
| JSON output schema | ✅ 6 tests covering arrays, batch size, empty outputs, seed passthrough |

**Gap:** None of the 20 tests verify that `main()` correctly propagates an OOM error dict to stdout JSON. The `TestMainOutputSchema` tests mock `http()` to return a success entry, so they never exercise the OOM error path end-to-end.

---

## Security review

No security concerns:
- No credentials hardcoded
- No user input passed to shell (SCP commands use fixed SSH host + pre-validated filenames, `subprocess.run` with list-args → no shell injection)
- No SQL or path traversal surface

---

## Overall assessment

**1 HIGH issue** — the OOM detection works at the `poll_history` level but the error dict is lost in `main()` before it reaches stdout JSON. The fix is adding 3 lines after `poll_history` returns.

**Test gap** — no end-to-end test validates OOM propagation through `main()`.

Everything else (condition logic, idle timer placement, proxy parity, test coverage) is correct.

---

## Verdict

| Severity | Count | Status |
|----------|-------|--------|
| CRITICAL | 0     | pass   |
| HIGH     | 1     | warn   |
| MEDIUM   | 1     | info   |
| LOW      | 0     | pass   |

**Verdict: WARNING** — 1 HIGH issue (OOM error swallowed in main()) should be fixed before merge.
