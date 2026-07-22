# FINDINGS-EXPLORATION.md

## Task: Trace how skill descriptions appear in Hermes system prompt

---

### Answer to Q1: Full path from skill storage → system prompt

**Pipeline (3 layers):**

#### Layer 1: Skill storage
- Skills live as `SKILL.md` files under `~/.hermes/skills/<category>/<skill-name>/SKILL.md`
- Also scanned from external dirs configured via `skills.external_dirs` in config.yaml

#### Layer 2: Reading & parsing → `build_skills_system_prompt()`
**File:** `agent/prompt_builder.py`, function `build_skills_system_prompt` (line 1449–1710)

Two sub-paths:

**Fast path (disk snapshot)** — `prompt_builder.py:1506–1531`:
1. Reads `.skills_prompt_snapshot.json` from `get_hermes_home()` (`prompt_builder.py:1264-1265`)
2. Each skill entry carries `{"skill_name", "category", "frontmatter_name", "description", "platforms", "conditions"}` — the `description` field in the snapshot was originally populated from `_parse_skill_file()` output
3. Groups by category into `skills_by_category: dict[str, list[(name, description)]]`
4. Also reads category-level descriptions from `DESCRIPTION.md` files (`prompt_builder.py:1554–1566`)

**Cold path (full filesystem scan)** — `prompt_builder.py:1532–1573`:
1. Calls `iter_skill_index_files(skills_dir, "SKILL.md")` to walk the skills directory
2. Calls `_parse_skill_file(skill_file)` (`prompt_builder.py:1376–1399`)
3. `_parse_skill_file()` reads SKILL.md text, parses YAML frontmatter, checks platform/environment compatibility
4. Extracts description via `extract_skill_description(frontmatter)` (`agent/skill_utils.py:771–776`) — **only reads `frontmatter.get("description", "")`, no body-text fallback**
5. Results are grouped into `skills_by_category[category].append((frontmatter_name, description))`
6. A snapshot is written to disk for next cold start
7. External skill dirs are scanned the same way (lines 1576–1610)

#### Layer 3: Formatting → injection into system prompt

**Formatting** — `prompt_builder.py:1648–1701`:
- Renders the `## Skills (mandatory)` block with header instructions
- Opens `<available_skills>` tag
- Per category: renders `  <category>: <cat_desc>` then per skill: `    - <name>: <desc>` (or `    - <name>` if description is empty)
- Closes `</available_skills>` tag

**Injection into system prompt** — `agent/system_prompt.py:292–322`:
- Line 292: Checks if the agent has skills tools (`skills_list`, `skill_view`, `skill_manage`)
- Line 314: Calls `build_skills_system_prompt()` via `_r.build_skills_system_prompt(...)`
- Line 321: Appends result to `stable_parts` list
- `stable_parts` is assembled into the final system prompt elsewhere in `system_prompt.py`

---

### Answer to Q2: Exactly which fields appear?

Each skill in the `<available_skills>` block shows:

| Field | Source | How it appears |
|-------|--------|---------------|
| **skill name** | `frontmatter.get("name", dir_name)` | `- name: description` |
| **description** | `frontmatter.get("description")` | After name, separated by `: ` |
| **category** | Directory structure | As a grouping header `  category:` |

The exact format string is in `prompt_builder.py:1664–1671`:

```python
for name, desc in sorted(skills_by_category[category], key=lambda x: x[0]):
    if name in seen:
        continue
    seen.add(name)
    if desc:
        index_lines.append(f"    - {name}: {desc}")
    else:
        index_lines.append(f"    - {name}")
```

**Confirmed:** both `name` AND `description` appear in the rendered output, as `"- skillname: description text"`.

Category descriptions from `DESCRIPTION.md` frontmatter also appear on the category header line (`prompt_builder.py:1659–1663`).

---

### Answer to Q3: Is description missing, truncated, or omitted anywhere?

#### Finding A: No body-text fallback in system prompt path — vs body-text fallback in `skills_list` tool

**`_parse_skill_file()`** (`prompt_builder.py:1396`):
```python
return True, frontmatter, extract_skill_description(frontmatter)
```
→ `extract_skill_description(frontmatter)` reads ONLY `frontmatter.get("description", "")`. If the frontmatter has no `description:` field, returns `""` and the skill renders as `- name` (no description) in the system prompt.

**`_find_all_skills()`** (`tools/skills_tool.py:743–749`):
```python
description = frontmatter.get("description", "")
if not description:
    for line in body.strip().split("\n"):
        line = line.strip()
        if line and not line.startswith("#"):
            description = line
            break
```
→ Falls back to the first non-heading body line as description. This path is used by `skills_list()` tool output (visible to the LLM via tool use), but NOT by the system prompt pipeline.

**Impact:** A skill whose `SKILL.md` has no `description:` in frontmatter will appear **without description** in the `<available_skills>` system prompt block, but **with a description** (body-text fallback) in `skills_list()` tool results. This is a minor inconsistency — most bundled skills DO have description in frontmatter.

#### Finding B: Truncation exists only in `skills_tool.py`, NOT in the system prompt path

`tools/skills_tool.py:751–752`:
```python
if len(description) > MAX_DESCRIPTION_LENGTH:  # MAX_DESCRIPTION_LENGTH = 1024
    description = description[:MAX_DESCRIPTION_LENGTH - 3] + "..."
```
→ `skills_list()` and `get_available_skills()` truncate descriptions >1024 chars.

**No truncation exists in `build_skills_system_prompt()`** — the description passes through as-is from `extract_skill_description()`. This means system prompt descriptions can be arbitrarily long, while `skills_list` tool descriptions are soft-truncated at 1024 chars.

#### Finding C: `get_available_skills()` for TUI gateway drops descriptions entirely

`hermes_cli/banner.py:105–108`:
```python
for skill in all_skills:
    category = skill.get("category") or "general"
    skills_by_category.setdefault(category, []).append(skill["name"])
```
→ This function is used by the TUI gateway's `/tui/api/info` endpoint (`tui_gateway/server.py:3412-3414`), not the system prompt. The `description` field is discarded. Harmless for its use case (CLI welcome banner), but means the web/TUI info API never exposes skill descriptions.

#### Finding D: `_parse_skill_file` reads only first 4000 bytes in the `_find_all_skills` path

`tools/skills_tool.py:728`:
```python
content = skill_md.read_text(encoding="utf-8")[:4000]
```
→ `_find_all_skills()` truncates SKILL.md at 4KB before parsing frontmatter. If the YAML frontmatter itself exceeds 4KB (extremely unlikely for skill metadata), the description would be lost. The `build_skills_system_prompt` path reads the full file with no length limit (`prompt_builder.py:1383`).

---

### Summary table

| Pipeline | Description source | Body-text fallback? | Truncation? |
|----------|-------------------|---------------------|-------------|
| `<available_skills>` system prompt (fast snapshot) | `snapshot.entry.description` (from `extract_skill_description`) | **NO** | **NO** |
| `<available_skills>` system prompt (cold scan) | `_parse_skill_file` → `extract_skill_description(frontmatter)` | **NO** | **NO** |
| `skills_list()` tool | `_find_all_skills` → frontmatter, then body fallback | **YES** — first non-heading body line | **YES** — 1024 char limit |
| `get_available_skills()` (CLI/TUI banner) | `_find_all_skills` → **description dropped**, only name kept | N/A | N/A |

### Key files & line ranges

| File | Lines | Role |
|------|-------|------|
| `agent/prompt_builder.py` | 1449–1710 | Main function `build_skills_system_prompt` — collects, formats, and returns skill index |
| `agent/prompt_builder.py` | 1376–1399 | `_parse_skill_file` — reads SKILL.md, returns (compat, frontmatter, description) |
| `agent/prompt_builder.py` | 1342–1369 | `_build_snapshot_entry` — builds metadata dict for snapshot |
| `agent/prompt_builder.py` | 1305–1320 | `_load_skills_snapshot` — loads pre-parsed snapshot from disk |
| `agent/prompt_builder.py` | 1650–1701 | Formatting: renders `<available_skills>...</available_skills>` block |
| `agent/system_prompt.py` | 292–322 | Injection: calls `build_skills_system_prompt` and appends to `stable_parts` |
| `agent/skill_utils.py` | 771–776 | `extract_skill_description` — gets description only from frontmatter (no body fallback, no truncation) |
| `tools/skills_tool.py` | 669–777 | `_find_all_skills` — used by `skills_list()` tool, has body-text fallback + 1024-char truncation |
| `tools/skills_tool.py` | 161–163 | Constants `MAX_NAME_LENGTH=64`, `MAX_DESCRIPTION_LENGTH=1024` |
| `hermes_cli/banner.py` | 92–109 | `get_available_skills` — drops descriptions for CLI/TUI banner use |
| `tools/skill_manager_tool.py` | 170–171, 553–554 | Validates description length at skill create time (MAX_DESCRIPTION_LENGTH=1024) |

### Recommendations

1. **No description bug:** If the user sees a skill in `<available_skills>` without a description (just `"- skillname"`), the root cause is that `SKILL.md` has no `description:` field in its YAML frontmatter. Fix: add `description: ...` to the skill's frontmatter. (The body-text fallback used by `skills_list()` does not apply to the system prompt pipeline.)

2. **Truncation inconsistency:** `build_skills_system_prompt` has no description truncation at all. Consider adding the same 1024-char limit as `_find_all_skills` to prevent a single skill with a very long description from bloating the system prompt.
