#!/usr/bin/env python3
"""
CC0 World Generator MCP Server

Exposes the CC0 World Generator as three MCP tools for use by Claude Code
and other MCP clients. Thin wrapper over generate.py — all business logic
stays there.

Tools:
  cc0_generate_world  — takes a genre/theme, returns a World Bible + Compliance Manifest
  cc0_validate_world  — validates an existing world output against the five invariants
  cc0_list_corpus     — returns the five locked CC0 universes with evidence metadata

Usage (stdio, local):
  python src/mcp_server.py

Claude Desktop config (add to claude_desktop_config.json):
  {
    "mcpServers": {
      "cc0_mcp": {
        "command": "python",
        "args": ["/path/to/cc0-world-generator/src/mcp_server.py"]
      }
    }
  }
"""

import json
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ConfigDict
from mcp.server.fastmcp import FastMCP

# ── Import generator functions ────────────────────────────────────────────────

# Add src/ to path so we can import sibling module
sys.path.insert(0, str(Path(__file__).parent))

from generate import generate, validate_world, load_corpus, WORLDS_DIR  # noqa: E402

# ── Server ────────────────────────────────────────────────────────────────────

mcp = FastMCP("cc0_mcp")

# ── Input models ──────────────────────────────────────────────────────────────


class GenerateWorldInput(BaseModel):
    """Input model for cc0_generate_world."""

    model_config = ConfigDict(
        str_strip_whitespace=True,
        validate_assignment=True,
        extra="forbid",
    )

    genre: str = Field(
        ...,
        description=(
            "Genre or theme prompt for the world (e.g., 'noir detective city', "
            "'ancient gods in a tech dystopia', 'post-apocalyptic ocean world'). "
            "Must describe a setting or tone — not a branded IP. "
            "Marvel, Disney, DC, etc. will be refused."
        ),
        min_length=3,
        max_length=200,
    )


class ValidateWorldInput(BaseModel):
    """Input model for cc0_validate_world."""

    model_config = ConfigDict(
        validate_assignment=True,
        extra="forbid",
    )

    world_json: dict[str, Any] = Field(
        ...,
        description=(
            "The full world output object to validate. Must be a dict with either "
            "a 'world_bible' + 'compliance_manifest' (world path) or a 'refusal' key "
            "(refusal path). Pass the parsed JSON object, not a string."
        ),
    )


# ── Tools ─────────────────────────────────────────────────────────────────────


@mcp.tool(
    name="cc0_generate_world",
    annotations={
        "title": "Generate CC0 World Bible",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def cc0_generate_world(params: GenerateWorldInput) -> str:
    """Generate a World Bible and Compliance Manifest from a genre/theme prompt.

    Uses the five locked CC0 universes (Nouns, CrypToadz, Mfers,
    Bulfinch's Mythology 1855, racc00ns) to produce a copyright-safe
    creative brief with a structured compliance manifest.

    Every character, faction, and visual element is traced to a verified
    CC0 universe with an evidence_id. commercial_confidence is computed
    deterministically from risk flags — never set manually.

    If the prompt cannot be served safely from the corpus (e.g., requests
    branded IP like Marvel, Disney, DC), returns a refusal object explaining
    the reason and logging the gap for roadmap tracking.

    Args:
        params (GenerateWorldInput): Input containing:
            - genre (str): Genre/theme prompt (3-200 chars), e.g., "noir detective city"

    Returns:
        str: JSON-formatted string. One of two shapes:

        World output (success):
        {
            "id": "world:YYYY-MM-DD:slug",
            "prompt": str,
            "generated_at": "ISO8601",
            "world_bible": {
                "title": str,
                "logline": str,
                "setting": {...},
                "tone": str,
                "characters": [{"id": str, "name": str, "evidence_id": str, ...}],
                "factions": [{"id": str, "name": str, "evidence_id": str, ...}],
                "visual_language": str
            },
            "compliance_manifest": {
                "universes_used": [str],
                "assets_used": [{"universe": str, "evidence_id": str, "risk_flags": [...]}],
                "evidence_used": [str],
                "risk_flags": [str],
                "commercial_confidence": "high" | "medium" | "low",
                "rationale": str
            },
            "_validation_warnings": [str]   // present only if validator found issues
        }

        Refusal output:
        {
            "id": "refusal:YYYY-MM-DD:slug",
            "prompt": str,
            "generated_at": "ISO8601",
            "refusal": {
                "reason": str,
                "closest_possible": str,
                "corpus_gap": str
            }
        }

    Examples:
        - Use when: "Build a world for my noir detective story" -> genre="noir detective city"
        - Use when: "Create a CC0 world with ancient mythology and tech" -> genre="ancient gods in a tech dystopia"
        - Don't use when: You want to validate an existing world (use cc0_validate_world)
        - Don't use when: You need to inspect corpus licenses (use cc0_list_corpus)

    Error Handling:
        - Branded IP prompts return a refusal (not an error) — this is correct behavior
        - corpus.json not found: returns error string
        - ANTHROPIC_API_KEY not set: returns error string
        - Model JSON parse failure: returns error string
    """
    try:
        # generate() calls the Anthropic API, validates, saves to disk, and prints.
        # We capture its output by running the core logic directly.
        # Since generate() prints to stdout and returns None, we replicate its
        # core steps here to get the dict back for MCP response.
        import re
        from datetime import datetime, timezone
        import anthropic
        from generate import (
            load_corpus as _load_corpus,
            build_system_prompt,
            build_user_prompt,
            validate_world as _validate_world,
            save_output,
            log_refusal,
            slugify,
        )

        corpus = _load_corpus()
        client = anthropic.Anthropic()

        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8192,
            system=build_system_prompt(corpus),
            messages=[{"role": "user", "content": build_user_prompt(params.genre)}],
        )

        raw = message.content[0].text.strip()

        # Strip accidental markdown fencing
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            return f"Error: Model output is not valid JSON: {e}\nRaw (first 500 chars): {raw[:500]}"

        # Validate and auto-correct
        errors = _validate_world(data)

        # Handle refusal path
        if "refusal" in data:
            log_refusal(data, params.genre)
            output_path = save_output(data, params.genre)
            data["_saved_to"] = str(output_path)
            return json.dumps(data, indent=2)

        # Handle world path
        if errors:
            data["_validation_warnings"] = errors

        output_path = save_output(data, params.genre)
        data["_saved_to"] = str(output_path)
        return json.dumps(data, indent=2)

    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"


@mcp.tool(
    name="cc0_validate_world",
    annotations={
        "title": "Validate CC0 World Output",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def cc0_validate_world(params: ValidateWorldInput) -> str:
    """Validate a world output object against the five CC0 enforcement invariants.

    Checks:
      1. Every character and faction has an evidence_id
      2. All evidence_ids in world_bible appear in the compliance_manifest
      3. commercial_confidence matches computed value from risk flags
         (auto-corrects in the returned object if mismatched)

    Also validates the refusal path: refusal objects must not contain
    world_bible or compliance_manifest keys.

    Args:
        params (ValidateWorldInput): Input containing:
            - world_json (dict): Full world output object to validate

    Returns:
        str: JSON-formatted validation result:

        {
            "valid": bool,
            "warnings": [str],    // invariant violations found
            "world": dict         // the (possibly auto-corrected) world object
        }

        If commercial_confidence was auto-corrected, world.compliance_manifest
        will contain "_confidence_corrected": true.

    Examples:
        - Use when: "Check if this world output is valid" -> pass the world dict
        - Use when: Verifying a world before using it in downstream creative work
        - Don't use when: You need to generate a new world (use cc0_generate_world)
    """
    try:
        import copy
        world = copy.deepcopy(params.world_json)
        errors = validate_world(world)
        return json.dumps(
            {
                "valid": len(errors) == 0,
                "warnings": errors,
                "world": world,
            },
            indent=2,
        )
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"


@mcp.tool(
    name="cc0_list_corpus",
    annotations={
        "title": "List CC0 Corpus Universes",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def cc0_list_corpus() -> str:
    """List the five locked CC0 universes available for world generation.

    Returns the full corpus with license evidence, risk flags, and
    confidence ratings for each universe. Use this to understand what
    source material is available before generating a world, or to inspect
    the evidence backing a particular universe's CC0 claim.

    The five v0 universes are locked — corpus cap is sacred. No universe
    is added without primary-source evidence capture.

    Returns:
        str: JSON-formatted corpus object:

        {
            "universes": [
                {
                    "id": str,              // e.g., "univ:nouns"
                    "name": str,            // e.g., "Nouns"
                    "kind": str,            // "nft_collection" | "public_domain_corpus" | "synthetic_pack"
                    "license": {
                        "type": "CC0-1.0",
                        "evidence_id": str
                    },
                    "commercial_confidence": str,   // "high" | "medium-high" | "medium"
                    "risk_flags": [str],
                    "canonical_refs": [...],
                    "evidence": {...}               // full evidence record
                }
            ],
            "count": int,
            "corpus_cap": 5
        }

    Examples:
        - Use when: "What CC0 universes are available?" -> call with no params
        - Use when: Inspecting evidence for a specific universe before creative work
        - Don't use when: You need to generate a world (use cc0_generate_world)
    """
    try:
        corpus = load_corpus()
        universes = corpus.get("universes", [])
        return json.dumps(
            {
                "universes": universes,
                "count": len(universes),
                "corpus_cap": 5,
            },
            indent=2,
        )
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
