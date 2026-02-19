#!/usr/bin/env python3
"""
CC0 World Generator — Phase 1 CLI
Usage: python generate.py "noir detective city"
Output: worlds/world-YYYY-MM-DD-{slug}.json
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic

# ── Paths ────────────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent.parent
CORPUS_PATH = ROOT / "corpus.json"
WORLDS_DIR = ROOT / "worlds"
REFUSAL_LOG = ROOT / "evidence" / "refusal-log.jsonl"

WORLDS_DIR.mkdir(exist_ok=True)

# ── Load corpus ───────────────────────────────────────────────────────────────

def load_corpus() -> dict:
    if not CORPUS_PATH.exists():
        print(f"ERROR: corpus.json not found at {CORPUS_PATH}", file=sys.stderr)
        sys.exit(1)
    with open(CORPUS_PATH) as f:
        return json.load(f)

# ── Prompt assembly ───────────────────────────────────────────────────────────

SYSTEM_PROMPT_TEMPLATE = """\
You are a CC0 World Generator. Your job is to take a genre or theme prompt and \
produce a World Bible — a structured creative brief for agents, writers, and game \
tools — using only the verified CC0 universes in the corpus below.

You must output a single valid JSON object with no markdown fencing, no explanation, \
no commentary — raw JSON only.

Output either:
1. A world object with keys: id, prompt, generated_at, world_bible, compliance_manifest
2. A refusal object with keys: id, prompt, generated_at, refusal

Hard rules:
- Every character, faction, and visual element must trace to a universe in the corpus
- Every corpus reference must carry its evidence_id
- commercial_confidence is computed from risk flags — never set manually
- Risk flags are never suppressed — only documented
- If the prompt cannot be served safely from the corpus, output a refusal object

commercial_confidence logic:
- "high": All assets CC0 or public domain, primary evidence captured, no trademark \
flags above low, no jurisdiction ambiguity
- "medium": CC0 confirmed but trademark or meme derivative flag is medium, OR minor \
jurisdiction assumption required
- "low": Any unresolved risk flag medium-high, incomplete evidence, or refusal-adjacent

Corpus:
{corpus_json}
"""

def build_system_prompt(corpus: dict) -> str:
    return SYSTEM_PROMPT_TEMPLATE.format(
        corpus_json=json.dumps(corpus, indent=2)
    )

def build_user_prompt(genre: str) -> str:
    return (
        f"Genre/theme: {genre}\n\n"
        "Generate a World Bible and Compliance Manifest using only the corpus above."
    )

# ── Validation ────────────────────────────────────────────────────────────────

def validate_world(data: dict) -> list[str]:
    """
    Returns a list of violation strings. Empty list = valid.
    Enforces the five invariants from the project spec.
    """
    errors = []

    if "refusal" in data:
        # Refusal path: must not contain world_bible or compliance_manifest
        for forbidden in ("world_bible", "compliance_manifest"):
            if forbidden in data:
                errors.append(f"Refusal output must not contain '{forbidden}'")
        return errors

    # World path
    wb = data.get("world_bible", {})
    cm = data.get("compliance_manifest", {})

    if not wb:
        errors.append("Missing world_bible")
        return errors
    if not cm:
        errors.append("Missing compliance_manifest")
        return errors

    # Invariant 1: every asset reference has an evidence_id
    for char in wb.get("characters", []):
        if not char.get("evidence_id"):
            label = char.get("id") or char.get("name", "?")
            errors.append(f"Character '{label}' missing evidence_id")
    for faction in wb.get("factions", []):
        if not faction.get("evidence_id"):
            label = faction.get("id") or faction.get("name", "?")
            errors.append(f"Faction '{label}' missing evidence_id")

    # Invariant 2: collect declared evidence_ids from any of the known field names
    # Model may emit: evidence_used (flat list), assets_used, or asset_clearances
    raw_declared = cm.get("evidence_used", [])
    for field in ("asset_clearances", "assets_used"):
        assets = cm.get(field, [])
        if assets and not raw_declared:
            raw_declared = [a.get("evidence_id") for a in assets if a.get("evidence_id")]
            break
    declared = set(raw_declared)
    for char in wb.get("characters", []):
        eid = char.get("evidence_id")
        if eid and eid not in declared:
            label = char.get("id") or char.get("name", "?")
            errors.append(
                f"Character '{label}' evidence_id '{eid}' "
                "not in compliance_manifest evidence"
            )
    for faction in wb.get("factions", []):
        eid = faction.get("evidence_id")
        if eid and eid not in declared:
            label = faction.get("id") or faction.get("name", "?")
            errors.append(
                f"Faction '{label}' evidence_id '{eid}' "
                "not in compliance_manifest evidence"
            )

    # Invariant 3: recompute commercial_confidence from risk flags
    # Model emits unsuppressed_flags at manifest level + per-clearance flags in asset_clearances
    flags = list(cm.get("unsuppressed_flags", cm.get("risk_flags", [])))
    for asset in cm.get("asset_clearances", cm.get("assets_used", [])):
        for f in asset.get("risk_flags", []):
            if f not in flags:
                flags.append(f)
    high_flags = [f for f in flags if f.endswith(":high")]
    medium_flags = [f for f in flags if f.endswith(":medium")]

    # declared may be empty if model used asset_clearances without evidence_ids — not a low signal
    has_clearances = bool(cm.get("asset_clearances") or cm.get("assets_used") or declared)
    if high_flags or not has_clearances:
        expected_confidence = "low"
    elif medium_flags:
        expected_confidence = "medium"
    else:
        expected_confidence = "high"

    stated_confidence = cm.get("commercial_confidence")
    if stated_confidence != expected_confidence:
        errors.append(
            f"commercial_confidence mismatch: model said '{stated_confidence}', "
            f"computed '{expected_confidence}' from risk flags {flags}"
        )
        # Auto-correct rather than fail hard
        cm["commercial_confidence"] = expected_confidence
        cm["_confidence_corrected"] = True

    return errors

# ── Output ────────────────────────────────────────────────────────────────────

def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text[:40].strip("-")

def save_output(data: dict, genre: str) -> Path:
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    slug = slugify(genre)
    prefix = "refusal" if "refusal" in data else "world"
    filename = f"{prefix}-{date_str}-{slug}.json"
    output_path = WORLDS_DIR / filename
    # Always stamp generated_at and id from CLI — never trust model-emitted values
    data["generated_at"] = now.isoformat()
    data["id"] = f"{prefix}:{date_str}:{slug}"
    with open(output_path, "w") as f:
        json.dump(data, f, indent=2)
    return output_path

def log_refusal(data: dict, genre: str) -> None:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "prompt": genre,
        "reason": data.get("refusal", {}).get("reason", ""),
        "corpus_gap": data.get("refusal", {}).get("corpus_gap", ""),
    }
    with open(REFUSAL_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")

# ── Main ──────────────────────────────────────────────────────────────────────

def generate(genre: str) -> None:
    corpus = load_corpus()
    client = anthropic.Anthropic()

    print(f"Generating world for: '{genre}'")

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        system=build_system_prompt(corpus),
        messages=[{"role": "user", "content": build_user_prompt(genre)}],
    )

    raw = message.content[0].text.strip()

    # Strip accidental markdown fencing if present
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"ERROR: Model output is not valid JSON: {e}", file=sys.stderr)
        print("Raw output:", raw[:500], file=sys.stderr)
        sys.exit(1)

    # Validate
    errors = validate_world(data)
    if errors:
        print("VALIDATION WARNINGS:")
        for err in errors:
            print(f"  ⚠ {err}")

    # Handle refusal
    if "refusal" in data:
        log_refusal(data, genre)
        output_path = save_output(data, genre)
        print(f"\nREFUSED")
        print(f"Reason: {data['refusal'].get('reason', '')[:120]}...")
        print(f"Closest possible: {data['refusal'].get('closest_possible', '')[:120]}...")
        print(f"Logged to: {REFUSAL_LOG}")
        print(f"Saved to: {output_path}")
        return

    # Handle world
    cm = data.get("compliance_manifest", {})
    output_path = save_output(data, genre)

    print(f"\n✓ World generated: {data.get('world_bible', {}).get('title', 'Untitled')}")
    print(f"  Logline: {data.get('world_bible', {}).get('logline', '')}")
    print(f"  Universes: {', '.join(cm.get('universes_used', []))}")
    print(f"  Risk flags: {len(cm.get('risk_flags', []))}")
    print(f"  commercial_confidence: {cm.get('commercial_confidence')}")
    if cm.get("_confidence_corrected"):
        print("  (confidence was auto-corrected from model output)")
    print(f"  Saved to: {output_path}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python generate.py \"your genre or theme prompt\"")
        sys.exit(1)
    generate(" ".join(sys.argv[1:]))
