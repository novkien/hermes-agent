#!/usr/bin/env python3
"""
Phase 3+4 — translate.py: ModularBatchTranslator + llama-proxy call for Ren'Py translation.

Reads .parsed.yaml files from game/tl/<lang>/, translates untranslated blocks
via llama-proxy HTTP endpoint, and saves back to .parsed.yaml (vi: field filled).

Usage:
    python scripts/v2/translate.py --game-root GAME_ROOT
    python scripts/v2/translate.py --game-root GAME_ROOT --limit N

Adapted from translate-renpy/scripts/translate.py — major changes:
  - Mod 1: llama-proxy HTTP call instead of local llama.cpp binding
  - Mod 2: retry only for hanzi leak detection (not empty-output or token sig)
  - Mod 3: no batch mode (single-item translate always)
  - Mod 4: incremental save every 100 blocks
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional

import requests
import yaml

from renpy_models import ParsedBlock, is_separator_block, parse_block_id
from translation_validation import validate_translation


# ── Hanzi regex (Chinese characters) ──────────────────────────────
# Used for retry detection — if output contains any of these, we retry
HANZI_RE_STR = r'[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]'
HANZI_RE = re.compile(HANZI_RE_STR)


# ══════════════════════════════════════════════════════════════════
#  ModularBatchTranslator
# ══════════════════════════════════════════════════════════════════

class ModularBatchTranslator:
    """
    Batch translator for parsed YAML files with custom context logic.

    Cloned from translate-renpy/scripts/translate.py:40-273.
    Changes:
      - Init takes proxy params instead of translator object
      - translate_file calls _translate_one() instead of self.translator.translate()
      - Incremental save every 100 blocks
      - Concurrency accepted but serial 1-by-1 (same as original flow)
    """

    def __init__(
        self,
        characters: Dict,
        lang_code: str,
        context_before: int = 3,
        context_after: int = 1,
        base_url: str = "http://192.168.0.140:8082",
        model: str = "Qwen-3.6-35B-A3B-nr",
        concurrency: int = 4,
        system_prompt: str = "",
        glossary: Optional[Dict[str, str]] = None,
        http_timeout: int = 600,
        max_blocks: int = 0,
        source_lang: str = "english",
        batch_size: int = 5,
        context_resolver: Optional['ContextResolver'] = None,
    ):
        self.characters = characters
        self.target_lang_code = lang_code
        self.context_before = context_before
        self.context_after = context_after
        self.base_url = base_url.rstrip('/')
        self.model = model
        self.concurrency = concurrency
        self.http_timeout = http_timeout
        self.max_blocks = max_blocks
        self.glossary = glossary
        self.source_lang = source_lang
        # Optional context resolver (Phase 0-1 context YAML pipeline)
        self.context_resolver = context_resolver
        # Concurrency counts HTTP batch requests. batch_size counts source fields
        # inside one request; 32 workers × 5 fields means up to 160 fields in flight.
        self.batch_size = max(1, int(batch_size or 1))

        # Source language display mapping
        lang_display = {
            'english': ('English', 'EN', ''),
            'thai': ('Thai', 'TH', '/Thai'),
        }
        source_display, source_upper, blacklist_suffix = lang_display.get(
            source_lang, ('English', 'EN', '')
        )
        self.source_lang_display = source_display

        # Apply template replacements to system prompt
        system_prompt = system_prompt.replace('{SOURCE_LANG_DISPLAY}', source_display)
        system_prompt = system_prompt.replace('{SOURCE_LANG_UPPER}', source_upper)
        system_prompt = system_prompt.replace('{SOURCE_LANG_BLACKLIST}', blacklist_suffix)
        system_prompt = system_prompt.replace('{SOURCE_LANG}', source_lang)
        self.system_prompt = system_prompt

    # ── Main file translation ──────────────────────────────────

    def translate_file(
        self,
        parsed_yaml_path: Path,
        tags_yaml_path: Path,
        output_yaml_path: Optional[Path] = None,
    ) -> Dict[str, int]:
        """Translate a single .parsed.yaml file — load, identify untranslated,
        build contexts, translate block by block, save."""
        if output_yaml_path is None:
            output_yaml_path = parsed_yaml_path

        print(f"\n  Processing: {parsed_yaml_path.name}")

        # Load data
        with open(parsed_yaml_path, 'r', encoding='utf-8') as f:
            parsed_blocks: Dict[str, ParsedBlock] = yaml.safe_load(f)

        with open(tags_yaml_path, 'r', encoding='utf-8-sig') as f:
            tags_file = yaml.safe_load(f)

        metadata = tags_file['metadata']
        structure = tags_file['structure']
        block_order = structure['block_order']

        # Identify which blocks need translation
        untranslated_ids = self._identify_untranslated(parsed_blocks, self.target_lang_code)
        total_blocks = len([bid for bid in parsed_blocks if not is_separator_block(bid, parsed_blocks[bid])])
        empty_source_count = sum(
            1 for block_id, block in parsed_blocks.items()
            if not is_separator_block(block_id, block) and not str(block.get('en', '') or '').strip()
        )

        print(f"    Total blocks: {total_blocks}")
        print(f"    Source-empty (not translatable): {empty_source_count}")
        print(f"    Untranslated: {len(untranslated_ids)}")
        print(f"    Already done: {total_blocks - empty_source_count - len(untranslated_ids)}")

        if not untranslated_ids:
            print("    [OK] All blocks already translated!")
            return {'total': total_blocks, 'translated': 0, 'skipped': total_blocks, 'failed': 0}

        # Build contexts for sliding window
        # If global limit is set, cap untranslated blocks
        if self.max_blocks > 0 and len(untranslated_ids) > self.max_blocks:
            untranslated_ids = untranslated_ids[:self.max_blocks]

        contexts = self._extract_contexts(
            untranslated_ids, parsed_blocks, block_order, tags_file.get('blocks', {}),
        )

        print(f"    Translating {len(contexts)} blocks...")
        batches = [contexts[i:i + self.batch_size] for i in range(0, len(contexts), self.batch_size)]
        print(
            f"    Translating {len(contexts)} blocks in {len(batches)} batch request(s) "
            f"(batch_size={self.batch_size}, concurrency={self.concurrency})..."
        )
        translated_count = 0
        failed_count = 0
        completed_blocks = 0
        next_autosave_at = 100
        start_time = time.time()

        # Each worker executes one HTTP batch request. A malformed batch response
        # falls back to isolated single-item requests, never to positional guessing.
        with ThreadPoolExecutor(max_workers=self.concurrency) as executor:
            futures = {executor.submit(self._translate_batch, batch): batch for batch in batches}
            for future in as_completed(futures):
                batch = futures[future]
                try:
                    results = future.result()
                except Exception as e:
                    print(f"\n    [ERROR] Batch failed for {len(batch)} blocks: {e}")
                    results = {ctx['block_id']: "" for ctx in batch}

                for ctx in batch:
                    block_id = ctx['block_id']
                    translation = results.get(block_id, "")
                    if translation:
                        parsed_blocks[block_id][self.target_lang_code] = translation
                        translated_count += 1
                        if translated_count <= 3:
                            print(f"\n    [DEBUG] Block {block_id}")
                            print(f"            EN: {ctx['text'][:60]}...")
                            print(f"            VI: {translation[:60]}...")
                    else:
                        failed_count += 1

                completed_blocks += len(batch)
                elapsed = time.time() - start_time
                rate = completed_blocks / elapsed if elapsed > 0 else 0
                print(
                    f"    [{completed_blocks}/{len(contexts)}] {rate:.1f} blk/s, "
                    f"{translated_count} ok, {failed_count} fail"
                )

                if completed_blocks >= next_autosave_at:
                    try:
                        self._save_yaml(parsed_blocks, output_yaml_path, metadata)
                        print(f"    [SAVE] Autosave at block {completed_blocks}/{len(contexts)}")
                        next_autosave_at += 100
                    except Exception as e:
                        print(f"    [ERROR] Autosave failed: {e}")

        print()

        # Final save
        try:
            self._save_yaml(parsed_blocks, output_yaml_path, metadata)
            print(f"    [OK] Saved to: {output_yaml_path.name}")
        except Exception as e:
            print(f"    [ERROR] Failed to save YAML file: {e}")
            import traceback
            traceback.print_exc()
            failed_count += translated_count
            translated_count = 0

        stats = {
            'total': total_blocks,
            'translated': translated_count,
            'skipped': total_blocks - len(untranslated_ids),
            'failed': failed_count,
        }
        print(f"    [OK] Translated: {stats['translated']}, Failed: {stats['failed']}")
        return stats

    # ── Untranslated detection (identical to translate-renpy) ──

    def _identify_untranslated(
        self,
        parsed_blocks: Dict[str, ParsedBlock],
        lang_code: str,
    ) -> List[str]:
        """Return only non-empty-source blocks that are missing translations.

        Empty-source template placeholders are not LLM work items: letting them
        inherit sliding-window context creates invented/merged dialogue.
        """
        untranslated = []
        for block_id, block in parsed_blocks.items():
            if is_separator_block(block_id, block):
                continue
            source_text = str(block.get('en', '') or '')
            if not source_text.strip():
                continue
            target_text = block.get(lang_code, '')
            if not target_text or not target_text.strip():
                untranslated.append(block_id)
        return untranslated

    # ── Context extraction (identical to translate-renpy) ──────

    def _extract_contexts(
        self,
        untranslated_ids: List[str],
        parsed_blocks: Dict[str, ParsedBlock],
        block_order: List[str],
        tagged_blocks: Dict[str, dict],
    ) -> List[Dict]:
        """Build contexts using metadata, never a parsed block-id suffix.

        Menu/UI strings are choice-like, so they receive no dialogue window.
        This prevents a terse option from being expanded into surrounding lines.
        """
        contexts: List[Dict] = []
        block_index = {block_id: idx for idx, block_id in enumerate(block_order)}

        for block_id in untranslated_ids:
            idx = block_index.get(block_id)
            if idx is None:
                continue
            text_to_translate = str(parsed_blocks[block_id].get('en', '') or '')
            if not text_to_translate.strip():
                continue

            tagged = tagged_blocks.get(block_id, {})
            char_name = tagged.get('char_name') or parse_block_id(block_id)[1]
            is_choice = tagged.get('type') == 'string' or tagged.get('label') == 'strings'
            context_list: List[str] = [] if is_choice else self._extract_dialogue_context(
                block_id, idx, parsed_blocks, block_order,
            )
            contexts.append({
                'block_id': block_id,
                'character_name': char_name,
                'text': text_to_translate,
                'context': context_list,
                'is_choice': is_choice,
            })

        return contexts

    def _extract_dialogue_context(
        self,
        block_id: str,
        idx: int,
        parsed_blocks: Dict[str, ParsedBlock],
        block_order: List[str],
    ) -> List[str]:
        """
        Sliding window: context_before=3, context_after=1.
        Prefer translated text (target_lang_code) or fallback to English.
        """
        context_before: List[str] = []
        context_after: List[str] = []

        # Walk backward
        for i in range(idx - 1, max(-1, idx - self.context_before - 10), -1):
            if len(context_before) >= self.context_before:
                break
            prev_id = block_order[i]
            prev_block = parsed_blocks.get(prev_id)
            if not prev_block or is_separator_block(prev_id, prev_block):
                continue
            prev_text = prev_block.get(self.target_lang_code, '') or prev_block.get('en', '')
            if prev_text.strip():
                prev_char = parse_block_id(prev_id)[1]
                context_before.insert(0, f"{prev_char}: {prev_text}")

        # Walk forward
        for i in range(idx + 1, min(len(block_order), idx + self.context_after + 10)):
            if len(context_after) >= self.context_after:
                break
            next_id = block_order[i]
            next_block = parsed_blocks.get(next_id)
            if not next_block or is_separator_block(next_id, next_block):
                continue
            next_text = next_block.get(self.target_lang_code, '') or next_block.get('en', '')
            if next_text.strip():
                next_char = parse_block_id(next_id)[1]
                context_after.append(f"{next_char}: {next_text}")

        return context_before + context_after

    # ── YAML save (identical to translate-renpy) ───────────────

    def _save_yaml(
        self,
        parsed_blocks: Dict[str, ParsedBlock],
        output_path: Path,
        metadata: dict,
    ):
        """Write YAML with header and flush verification."""
        from datetime import datetime

        output_path.parent.mkdir(parents=True, exist_ok=True)

        header = (
            f"# {output_path.stem} - Parsed Translations\n"
            f"# Original extraction: {metadata.get('extracted_at', 'unknown')}\n"
            f"# Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            "\n"
        )

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(header)
            yaml.dump(
                parsed_blocks, f,
                allow_unicode=True,
                default_flow_style=False,
                sort_keys=False,
            )
            f.flush()

        if not output_path.exists():
            raise IOError(f"File was not created: {output_path}")
        if output_path.stat().st_size == 0:
            raise IOError(f"File was created but is empty: {output_path}")

    # ── New: translation helpers (replaces old translator.translate) ──

    def _build_user_prompt(
        self,
        text: str,
        context_lines: Optional[List[str]] = None,
        speaker: Optional[str] = None,
        glossary_hits: Optional[Dict[str, str]] = None,
    ) -> str:
        """
        Build the dynamic user message for a single translation entry.

        Order: glossary section → context section → speaker hint → English/Vietnamese lines.
        Empty sections are omitted.
        """
        parts: List[str] = []

        # Context is deliberately labeled read-only so it cannot be mistaken
        # for additional source material by the model.
        if glossary_hits:
            gloss_lines = ["GLOSSARY (apply only to SOURCE):"]
            for key, val in glossary_hits.items():
                gloss_lines.append(f"- {key} => {val}")
            parts.append("\n".join(gloss_lines))
        if context_lines:
            parts.append("CONTEXT (read-only; do not translate or reproduce):\n" + "\n".join(
                f"- {line}" for line in context_lines
            ))
        if speaker and speaker not in ('Narrator', 'Choice', ''):
            parts.append(f"SPEAKER (tone hint only): {speaker}")
        parts.append(f"SOURCE ({self.source_lang_display}; translate this exact value only):\n{text}")
        parts.append("OUTPUT (Vietnamese translation only, no label):")
        return "\n\n".join(parts)

    def _build_batch_user_prompt(self, contexts: List[dict]) -> str:
        """Build the batch payload.

        When context_resolver is set, wraps items in shared context dict:
          {"context": {...}, "items": [[source, context, speaker], ...]}

        Without context_resolver, returns old positional array:
          [[source, context, speaker], ...]
        """
        items = []
        for context_item in contexts:
            text = context_item['text']
            char_name = context_item['character_name']
            items.append([
                text,
                context_item['context'],
                char_name if char_name not in ('Narrator', 'Choice', '') else None,
            ])

        if self.context_resolver is not None:
            # Resolve shared context for this batch
            # Use first item's block_id to infer label/file
            # Block IDs like "1-Will" don't carry file/label info directly.
            # The resolver gets called here with empty file/label fallback.
            # In the future, file/label could be passed from translate_file().
            resolved = self.context_resolver.resolve(
                file="",
                label="",
                characters=[c['character_name'] for c in contexts
                           if c['character_name'] not in ('Narrator', 'Choice', '')],
            )
            return json.dumps({
                "context": resolved,
                "items": items,
            }, ensure_ascii=False, separators=(',', ':'))

        return json.dumps(items, ensure_ascii=False, separators=(',', ':'))

    def _call_proxy(self, user_prompt: str, system_prompt: str, *, max_tokens: int = 512) -> str:
        """Single OpenAI-compatible request; raw batch response is parsed separately."""
        url = f"{self.base_url}/v1/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }
        resp = requests.post(url, json=payload, timeout=self.http_timeout)
        resp.raise_for_status()
        return data['choices'][0]['message']['content'].strip() if (data := resp.json()) else ""

    @staticmethod
    def _strip_json_fence(content: str) -> str:
        content = content.strip()
        if content.startswith("```"):
            content = re.sub(r'^```(?:json)?\s*', '', content, count=1, flags=re.I)
            content = re.sub(r'\s*```$', '', content, count=1)
        return content.strip()

    def _parse_batch_response(self, content: str, contexts: List[dict]) -> Optional[Dict[str, str]]:
        """Accept only a positional JSON array with exact batch cardinality."""
        try:
            rows = json.loads(self._strip_json_fence(content))
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(rows, list) or len(rows) != len(contexts):
            return None
        result = {}
        for item, translation in zip(contexts, rows):
            if not isinstance(translation, str):
                return None
            errors = validate_translation(item['text'], translation, is_choice=item['is_choice'])
            if HANZI_RE.search(translation):
                errors.append("hanzi_leak")
            if errors:
                return None
            result[item['block_id']] = translation
        return result

    def _translate_batch(self, contexts: List[dict]) -> Dict[str, str]:
        """Translate up to batch_size fields; retry once, then isolate only failures."""
        prompt = self._build_batch_user_prompt(contexts)
        system = self.system_prompt + (
            "\n\nBATCH: Input is [[source,context,speaker],...]. "
            "Return only JSON [translation,...], exactly one string per item in order. "
            "Never omit, merge, reorder, label, or add text. Context is read-only."
        )
        for attempt in range(2):
            try:
                # 5 short fields can need more output than the single-item default.
                response = self._call_proxy(prompt, system, max_tokens=max(512, len(contexts) * 256))
            except Exception as exc:
                print(f"      [WARN] Batch API call failed (attempt {attempt + 1}): {exc}")
                continue
            parsed = self._parse_batch_response(response, contexts)
            if parsed is not None:
                return parsed
            system += "\nRETRY: Return only the positional JSON string array; no Markdown or labels."

        # A broken JSON batch cannot be mapped safely. Retry each source independently.
        print(f"      [FALLBACK] Invalid batch response; retrying {len(contexts)} item(s) individually")
        return {
            item['block_id']: self._translate_with_glossary(item)[1]
            for item in contexts
        }

    # ── Thread-safe translation helper for concurrent loop ──────

    def _translate_with_glossary(self, context_item: dict) -> tuple:
        """Build glossary hits and translate one block. Thread-safe.

        Designed for ThreadPoolExecutor: accepts a single context dict,
        extracts fields, builds glossary matches inline, calls _translate_one,
        returns (block_id, translation_string).

        parsed_blocks writes are safe under the GIL; this method is pure
        I/O-bound (HTTP via requests) so ThreadPoolExecutor is appropriate.
        """
        block_id = context_item['block_id']
        text = context_item['text']
        context_list = context_item['context']
        char_name = context_item['character_name']
        speaker = None if char_name in ('Narrator', 'Choice') else char_name

        # Build glossary hits
        glossary_hits = {}
        if self.glossary and text:
            for key, val in self.glossary.items():
                if key.lower() in text.lower():
                    glossary_hits[key] = val

        translation = self._translate_one(
            text=text,
            context_lines=context_list if context_list else None,
            speaker=speaker,
            glossary_hits=glossary_hits if glossary_hits else None,
            is_choice=context_item['is_choice'],
        )
        return block_id, translation

    def _translate_one(
        self,
        text: str,
        context_lines: Optional[List[str]] = None,
        speaker: Optional[str] = None,
        glossary_hits: Optional[Dict[str, str]] = None,
        is_choice: bool = False,
    ) -> str:
        """Translate one source block and reject invalid outputs before save.

        All source/output contract failures receive one retry. A failed second
        attempt returns an empty value so the parsed block remains untranslated
        instead of contaminating later merge/deploy artifacts.
        """
        max_attempts = 2

        for attempt in range(1, max_attempts + 1):
            user_prompt = self._build_user_prompt(text, context_lines, speaker, glossary_hits)
            system = self.system_prompt
            if attempt > 1:
                system += (
                    "\n- RETRY: return only the translation of SOURCE; preserve all tokens; "
                    "do not emit labels, context, or extra lines."
                )
            try:
                translation = self._call_proxy(user_prompt, system)
            except Exception as e:
                print(f"      [WARN] API call failed (attempt {attempt}): {e}")
                continue

            errors = validate_translation(text, translation, is_choice=is_choice)
            if HANZI_RE.search(translation):
                errors.append("hanzi_leak")
            if errors:
                print(f"      [RETRY] Invalid output ({', '.join(errors)}) attempt {attempt}")
                continue
            return translation

        return ""


# ══════════════════════════════════════════════════════════════════
#  CLI HELPERS
# ══════════════════════════════════════════════════════════════════

def load_config(game_root: Path) -> dict:
    """Read .jarvis/config.yaml or return empty dict if missing (with warning)."""
    config_path = game_root / '.jarvis' / 'config.yaml'
    if not config_path.is_file():
        print(f"Warning: {config_path} not found — using defaults.")
        return {}
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    return config if config else {}


def load_glossary(game_root: Path) -> Optional[Dict[str, str]]:
    """Load glossary from .jarvis/: base + uncensored overlay merged."""
    glossary: Dict[str, str] = {}

    base_path = game_root / '.jarvis' / 'vi_glossary.yaml'
    uncensored_path = game_root / '.jarvis' / 'vi_glossary_uncensored.yaml'

    if base_path.exists():
        with open(base_path, 'r', encoding='utf-8-sig') as f:
            data = yaml.safe_load(f)
            if data and isinstance(data, dict):
                glossary.update(data)
        print(f"[OK] Glossary: {base_path.name}")

    if uncensored_path.exists():
        with open(uncensored_path, 'r', encoding='utf-8-sig') as f:
            data = yaml.safe_load(f)
            if data and isinstance(data, dict):
                glossary.update(data)
        print(f"[OK] Glossary (uncensored overlay): {uncensored_path.name}")

    if not glossary:
        print("[WARNING] No glossary found — continuing without glossary guidance")
        return None

    return glossary


def load_characters(game_root: Path) -> Dict:
    """Load characters from .jarvis/characters.yaml."""
    char_path = game_root / '.jarvis' / 'characters.yaml'
    if not char_path.exists():
        print(f"[WARNING] No characters.yaml found — using empty map")
        return {}
    with open(char_path, 'r', encoding='utf-8') as f:
        chars = yaml.safe_load(f)
    return chars if chars else {}


def load_system_prompt(script_dir: Path) -> str:
    """Load system prompt from translate.txt (same dir as script)."""
    prompt_path = script_dir / 'translate.txt'
    if not prompt_path.exists():
        print(f"[WARNING] translate.txt not found at {prompt_path}")
        return ""
    text = prompt_path.read_text(encoding='utf-8').strip()
    print(f"[OK] System prompt loaded ({len(text)} chars)")
    return text


def find_parsed_files(tl_dir: Path) -> List[Path]:
    """Find .parsed.yaml files recursively, excluding merge artifacts."""
    return sorted(
        f for f in tl_dir.rglob("*.parsed.yaml")
        if '.translated.' not in f.name
    )


# ══════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Phase 3+4: Translate parsed Ren'Py YAML files via llama-proxy.",
    )
    ap.add_argument(
        '--game-root', required=True,
        help='Path to the game root (containing game/ and .jarvis/).',
    )
    ap.add_argument(
        '--limit', type=int, default=0,
        help='Max files to process (0 = all).',
    )
    ap.add_argument(
        '--timeout', type=int, default=600,
        help='HTTP request timeout in seconds (default 600). Increase for slow model loading.',
    )
    args = ap.parse_args()

    game_root = Path(args.game_root).resolve()
    if not game_root.is_dir():
        print(f"Error: game root not found: {game_root}", file=sys.stderr)
        return 1

    print()
    print("=" * 70)
    print("  Ren'Py Translation — Phase 3+4 (translate.py)")
    print("=" * 70)
    print()

    # ── Load config ──
    config = load_config(game_root)
    game_cfg = config.get('game', {})
    base_url = game_cfg.get('base_url', 'http://192.168.0.140:8082')
    model = game_cfg.get('model', 'Qwen-3.6-35B-A3B-nr')
    lang = game_cfg.get('lang', 'vietnamese')
    context_before = config.get('translation', {}).get('context_before', 3)
    context_after = config.get('translation', {}).get('context_after', 1)
    concurrency = config.get('translation', {}).get('concurrency', 4)
    batch_size = config.get('translation', {}).get('batch_size', 5)
    source_lang = game_cfg.get('source_lang', 'english')

    source_lang_display = source_lang.capitalize()
    print(f"  Source:     {source_lang_display} → Vietnamese")
    print(f"  Game root:  {game_root}")
    print(f"  Language:   {lang}")
    print(f"  Source:     {source_lang}")
    print(f"  Base URL:   {base_url}")
    print(f"  Model:      {model}")
    print(f"  Concurrency:{concurrency} batch HTTP request(s)")
    print(f"  Batch size: {batch_size} source field(s)/request")
    print(f"  Max in-flight source fields: {concurrency * max(1, int(batch_size or 1))}")
    print()

    # ── Load glossary, characters, system prompt ──
    glossary = load_glossary(game_root)
    characters = load_characters(game_root)

    script_dir = Path(__file__).parent.resolve()
    system_prompt = load_system_prompt(script_dir)

    if not system_prompt:
        print("Error: system prompt is empty — translate.txt missing or blank.", file=sys.stderr)
        return 1

    # ── Init translator ──
    translator = ModularBatchTranslator(
        characters=characters,
        lang_code='vi',
        context_before=context_before,
        context_after=context_after,
        base_url=base_url,
        model=model,
        concurrency=concurrency,
        system_prompt=system_prompt,
        glossary=glossary,
        http_timeout=args.timeout,
        max_blocks=args.limit,
        source_lang=source_lang,
        batch_size=batch_size,
    )

    # ── Find .parsed.yaml files ──
    tl_dir = game_root / 'game' / 'tl' / lang
    if not tl_dir.is_dir():
        print(f"Error: translation directory not found: {tl_dir}", file=sys.stderr)
        return 1

    parsed_files = find_parsed_files(tl_dir)
    if not parsed_files:
        print(f"Error: no .parsed.yaml files found in {tl_dir}", file=sys.stderr)
        print("Run extract.py first to generate parsed files.")
        return 1

    print(f"Found {len(parsed_files)} file(s) to process")

    if args.limit > 0:
        parsed_files = parsed_files[:args.limit]
        print(f"  (limited to {len(parsed_files)} files)")

    print()

    # ── Translate each file ──
    print("=" * 70)
    print("  Translating Files")
    print("=" * 70)

    total_stats = {
        'files': 0,
        'total_blocks': 0,
        'translated_blocks': 0,
        'skipped_blocks': 0,
        'failed_blocks': 0,
    }

    overall_start = time.time()

    for file_idx, parsed_file in enumerate(parsed_files, 1):
        if len(parsed_files) > 1:
            elapsed = time.time() - overall_start
            print(f"\n[File {file_idx}/{len(parsed_files)}] ({elapsed:.0f}s elapsed)")

        base_name = parsed_file.name.removesuffix('.parsed.yaml')
        tags_file = parsed_file.parent / f"{base_name}.tags.yaml"

        if not tags_file.exists():
            print(f"  [WARNING] Skipping {parsed_file.name} — no matching .tags.yaml file")
            print(f"             Expected: {tags_file.name}")
            continue

        try:
            stats = translator.translate_file(
                parsed_yaml_path=parsed_file,
                tags_yaml_path=tags_file,
                output_yaml_path=None,
            )
        except Exception as e:
            print(f"  [ERROR] File processing failed for {parsed_file.name}: {e}")
            import traceback
            traceback.print_exc()
            continue

        total_stats['files'] += 1
        total_stats['total_blocks'] += stats['total']
        total_stats['translated_blocks'] += stats['translated']
        total_stats['skipped_blocks'] += stats['skipped']
        total_stats['failed_blocks'] += stats['failed']

    print()
    print("=" * 70)
    print("  TRANSLATION COMPLETE")
    print("=" * 70)
    print(f"  Files processed:     {total_stats['files']}")
    print(f"  Total blocks:        {total_stats['total_blocks']}")
    print(f"  Translated:          {total_stats['translated_blocks']}")
    print(f"  Already done:        {total_stats['skipped_blocks']}")
    print(f"  Failed:              {total_stats['failed_blocks']}")
    print("=" * 70)

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
