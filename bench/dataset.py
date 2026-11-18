"""
bench/dataset.py — Tier 4 item #12: loads the golden/redteam .jsonl files.

One row per line, compact keys so a reviewer can scan a whole file at a
glance (same convention SARA's own bench/dataset.py uses):
    t  text                          (required)
    i  expected intent/skill, or "chat" meaning "intent_router should NOT
       match anything - defer to the LLM" (required for golden rows)
    l  language: "en" | "hi" | "hinglish"
    s  style: "typed" | "spoken" | "terse" | ... (free text, diagnostics only)
    a  expected arguments dict, e.g. {"app_name": "spotify"} - optional,
       only set on rows where argument-extraction correctness matters
    n  a short note on WHY this row is labelled the way it is - optional,
       but strongly encouraged on anything non-obvious (see the golden
       files themselves: a Devanagari row expecting "chat" always explains
       why in `n`, since that's the kind of line someone might otherwise
       "fix" by mistake)
A line of just {"_defaults": {...}} at the top of a file sets fallback
values (typically just "l") merged under whatever each real row provides -
skipped when loading, not returned as a row.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

BENCH_DIR = Path(__file__).resolve().parent


@dataclass
class Row:
    text: str
    intent: str
    lang: str = "en"
    style: str = ""
    expected_args: Optional[Dict[str, Any]] = None
    note: str = ""
    source_file: str = ""
    line_no: int = 0


def load_jsonl(path: Path) -> List[Row]:
    rows: List[Row] = []
    defaults: Dict[str, Any] = {}
    with open(path, "r", encoding="utf-8") as f:
        for line_no, raw_line in enumerate(f, start=1):
            line = raw_line.strip()
            if not line or line.startswith("//"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}:{line_no}: invalid JSON ({e})") from e
            if "_defaults" in obj:
                defaults.update(obj["_defaults"])
                continue
            merged = {**defaults, **obj}
            if "t" not in merged or "i" not in merged:
                raise ValueError(f"{path}:{line_no}: row missing required 't' or 'i' key")
            rows.append(Row(
                text=merged["t"], intent=merged["i"], lang=merged.get("l", "en"),
                style=merged.get("s", ""), expected_args=merged.get("a"),
                note=merged.get("n", ""), source_file=path.name, line_no=line_no,
            ))
    return rows


def load_many(*names: str, subdir: str = "golden") -> List[Row]:
    rows: List[Row] = []
    for name in names:
        rows.extend(load_jsonl(BENCH_DIR / subdir / name))
    return rows


GOLDEN_FILES = ("intents_en.jsonl", "intents_hi.jsonl", "intents_hinglish.jsonl",
                "negatives.jsonl", "adversarial.jsonl")


def load_golden(files: Optional[List[str]] = None) -> List[Row]:
    return load_many(*(files or GOLDEN_FILES), subdir="golden")
