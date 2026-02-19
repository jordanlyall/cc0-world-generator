#!/usr/bin/env python3
"""
CC0 World Generator — Phase 2 Web Server
Usage: uvicorn src.web_server:app --reload --port 8080
       (from project root with venv active)
"""

import json
import uuid
import asyncio
import os
import urllib.request
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Optional

# Load .env if present (local dev) — force-set values from file, overriding empty env vars
_env_path = Path(__file__).parent.parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            _k, _v = _k.strip(), _v.strip()
            if _v:  # only set if .env has a non-empty value
                os.environ[_k] = _v

from fastapi import FastAPI, BackgroundTasks, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

# ── Paths ─────────────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent.parent
WORLDS_DIR = ROOT / "worlds"
TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
WORLDS_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)

# ── In-memory job store ───────────────────────────────────────────────────────
# Maps job_id -> {"status": "pending|running|done|error", "world_id": ..., "error": ...}

jobs: dict[str, dict] = {}

# ── Rate limiting ─────────────────────────────────────────────────────────────

DAILY_GENERATION_LIMIT = int(os.environ.get("DAILY_GENERATION_LIMIT", "100"))

_daily_counter: dict[str, int] = {}  # {"YYYY-MM-DD": count}

def _get_daily_count() -> int:
    today = date.today().isoformat()
    return _daily_counter.get(today, 0)

def _increment_daily_count() -> None:
    today = date.today().isoformat()
    _daily_counter[today] = _daily_counter.get(today, 0) + 1
    # Prune old days to avoid unbounded growth
    for k in list(_daily_counter.keys()):
        if k != today:
            del _daily_counter[k]

# ── App ───────────────────────────────────────────────────────────────────────

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="CC0 World Generator", version="0.2.0")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ── Models ────────────────────────────────────────────────────────────────────

class GenerateRequest(BaseModel):
    prompt: str

# ── Background generation ─────────────────────────────────────────────────────

def run_generation(job_id: str, prompt: str) -> None:
    """Runs generate.generate() synchronously in a thread pool worker."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from generate import generate as _generate, load_corpus, build_system_prompt, build_user_prompt
    from generate import validate_world, save_output, log_refusal, slugify
    import anthropic, json, re
    from datetime import datetime, timezone

    jobs[job_id]["status"] = "running"
    try:
        corpus = load_corpus()
        client = anthropic.Anthropic()

        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8192,
            system=build_system_prompt(corpus),
            messages=[{"role": "user", "content": build_user_prompt(prompt)}],
        )

        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)

        data = json.loads(raw)
        validate_world(data)  # auto-corrects confidence in-place

        if "refusal" in data:
            log_refusal(data, prompt)

        output_path = save_output(data, prompt)
        world_id = data["id"]
        jobs[job_id]["status"] = "done"
        jobs[job_id]["world_id"] = world_id
        jobs[job_id]["is_refusal"] = "refusal" in data

    except Exception as exc:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(exc)

# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    template_path = TEMPLATES_DIR / "index.html"
    return HTMLResponse(content=template_path.read_text())

@app.post("/generate")
@limiter.limit("5/hour")
async def generate(request: Request, req: GenerateRequest, background_tasks: BackgroundTasks):
    prompt = req.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
    if len(prompt) > 300:
        raise HTTPException(status_code=400, detail="prompt must be 300 characters or fewer")

    if _get_daily_count() >= DAILY_GENERATION_LIMIT:
        raise HTTPException(
            status_code=429,
            detail=f"Daily generation limit reached ({DAILY_GENERATION_LIMIT} worlds/day). Try again tomorrow.",
        )

    _increment_daily_count()
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "pending", "prompt": prompt}
    background_tasks.add_task(run_generation, job_id, prompt)
    return {"job_id": job_id}

@app.get("/status/{job_id}")
async def status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return job

@app.get("/world/{world_id:path}", response_class=HTMLResponse)
async def world_page(world_id: str):
    # world_id may be like "world:2026-02-19:noir-detective-city"
    # or the slug portion "world-2026-02-19-noir-detective-city"
    # Try to find the file by searching worlds/
    world_file = _find_world_file(world_id)
    if not world_file:
        raise HTTPException(status_code=404, detail=f"World '{world_id}' not found")

    data = json.loads(world_file.read_text())
    template_path = TEMPLATES_DIR / "world.html"
    html = template_path.read_text()
    # Embed the world JSON into the template
    world_json = json.dumps(data, indent=2)
    html = html.replace("__WORLD_JSON__", world_json)
    return HTMLResponse(content=html)

@app.get("/portrait-test", response_class=HTMLResponse)
async def portrait_test():
    template_path = TEMPLATES_DIR / "portrait-test.html"
    return HTMLResponse(content=template_path.read_text())


@app.get("/worlds")
async def list_worlds():
    worlds = []
    for f in sorted(WORLDS_DIR.glob("*.json"), reverse=True):
        try:
            data = json.loads(f.read_text())
            entry = {
                "id": data.get("id"),
                "prompt": data.get("prompt"),
                "generated_at": data.get("generated_at"),
                "is_refusal": "refusal" in data,
            }
            if not entry["is_refusal"]:
                wb = data.get("world_bible", {})
                cm = data.get("compliance_manifest", {})
                entry["title"] = wb.get("title")
                entry["logline"] = wb.get("logline")
                entry["commercial_confidence"] = cm.get("commercial_confidence")
            else:
                entry["reason"] = data.get("refusal", {}).get("reason", "")[:120]
            worlds.append(entry)
        except Exception:
            pass
    return worlds

@app.get("/api/world/{world_id:path}")
async def api_world(world_id: str):
    world_file = _find_world_file(world_id)
    if not world_file:
        raise HTTPException(status_code=404, detail="not found")
    return JSONResponse(content=json.loads(world_file.read_text()))

@app.get("/api/corpus")
async def api_corpus():
    """Returns the verified CC0 corpus — the five locked universes agents can draw from."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from generate import load_corpus
    return JSONResponse(content=load_corpus())

@app.get("/api/status")
async def api_status():
    """Returns current generation capacity — useful for agents that want to self-throttle."""
    remaining = max(0, DAILY_GENERATION_LIMIT - _get_daily_count())
    return {
        "daily_limit": DAILY_GENERATION_LIMIT,
        "daily_used": _get_daily_count(),
        "daily_remaining": remaining,
        "per_ip_limit": "5/hour",
        "generate_endpoint": "POST /generate",
        "poll_endpoint": "GET /status/{job_id}",
        "world_endpoint": "GET /api/world/{world_id}",
    }

# ── Worldkit on-chain reader (local Anvil) ────────────────────────────────────

ANVIL_RPC = os.environ.get("ANVIL_RPC", "http://127.0.0.1:8545")
LOOT_TOKEN_ADDR = os.environ.get("LOOT_TOKEN_ADDR", "0x5FC8d32690cc91D4c39d9d3abcBD16989F875707")
WORLD_REGISTRY_ADDR = os.environ.get("WORLD_REGISTRY_ADDR", "0x0165878A594ca255338adfa4d48449f69242Eb8F")

def _eth_call(to: str, data: str) -> str:
    """Raw JSON-RPC eth_call to Anvil. Returns hex result string."""
    payload = json.dumps({
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": to, "data": data}, "latest"],
        "id": 1,
    }).encode()
    req = urllib.request.Request(
        ANVIL_RPC,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        result = json.loads(resp.read())
    if "error" in result:
        raise ValueError(result["error"])
    return result["result"]

def _decode_uint256(hex_str: str) -> int:
    return int(hex_str, 16)

def _keccak_selector(sig: str) -> str:
    """4-byte selector from function signature via cast."""
    import subprocess
    out = subprocess.check_output(["cast", "sig", sig], text=True).strip()
    return out  # e.g. "0x313ce567"

# ABI selectors — verified with: cast sig "<signature>"
SEL_TOTAL_MINTED    = "0xa2309ff8"  # totalMinted()
SEL_TOKEN_URI       = "0xc87b56dd"  # tokenURI(uint256)
SEL_GEN_COUNT       = "0x1563e5c2"  # generationCount(uint256)
SEL_GEN_HISTORY     = "0x677f05ef"  # generationHistory(uint256)
SEL_OWNER_OF        = "0x6352211e"  # ownerOf(uint256)
SEL_MINTED_AT       = "0xf1b0aa15"  # mintedAt(uint256)
SEL_CURRENT_PHASE   = "0x055ad42e"  # currentPhase()
SEL_TBA             = "0x0be76ed6"  # tokenBoundAccount(uint256)

def _pad_uint256(n: int) -> str:
    return hex(n)[2:].zfill(64)

def _fetch_token_data(token_id: int) -> dict:
    """Fetch token metadata and generation history from local Anvil."""
    result = {}

    # totalMinted
    try:
        raw = _eth_call(LOOT_TOKEN_ADDR, SEL_TOTAL_MINTED)
        result["total_minted"] = _decode_uint256(raw)
    except Exception:
        result["total_minted"] = None

    # tokenURI(tokenId) — returns ABI-encoded string
    try:
        data = SEL_TOKEN_URI + _pad_uint256(token_id)
        raw = _eth_call(LOOT_TOKEN_ADDR, data)
        # ABI decode dynamic string: offset (32 bytes) + length (32 bytes) + data
        hex_data = raw[2:]  # strip 0x
        # offset to string data (usually 0x20 = 32 bytes from start)
        # length of string
        str_len = int(hex_data[64:128], 16)
        str_hex = hex_data[128:128 + str_len * 2]
        uri = bytes.fromhex(str_hex).decode("utf-8")
        # strip data:application/json;base64, prefix and decode
        if uri.startswith("data:application/json;base64,"):
            import base64
            meta_json = base64.b64decode(uri[len("data:application/json;base64,"):]).decode("utf-8")
            result["metadata"] = json.loads(meta_json)
        else:
            result["metadata"] = {"raw_uri": uri}
    except Exception as e:
        result["metadata"] = {"error": str(e)}

    # generationCount(tokenId)
    try:
        data = SEL_GEN_COUNT + _pad_uint256(token_id)
        raw = _eth_call(WORLD_REGISTRY_ADDR, data)
        result["generation_count"] = _decode_uint256(raw)
    except Exception:
        result["generation_count"] = 0

    # generationHistory(tokenId) — use cast call for full tuple[] decode
    result["generations"] = []
    try:
        result["generations"] = _fetch_generation_history(token_id)
    except Exception as e:
        result["generations_error"] = str(e)

    # ownerOf(tokenId) — returns ABI-encoded address (padded to 32 bytes)
    try:
        data = SEL_OWNER_OF + _pad_uint256(token_id)
        raw = _eth_call(LOOT_TOKEN_ADDR, data)
        # address is right-aligned in 32 bytes; take last 20 bytes = 40 hex chars
        addr_hex = "0x" + raw[-40:]
        result["owner"] = addr_hex
    except Exception:
        result["owner"] = None

    # mintedAt(tokenId) — mapping(uint256 => uint256) public getter
    try:
        data = SEL_MINTED_AT + _pad_uint256(token_id)
        raw = _eth_call(LOOT_TOKEN_ADDR, data)
        result["minted_at"] = _decode_uint256(raw)
    except Exception:
        result["minted_at"] = None

    # currentPhase() — uint8
    try:
        raw = _eth_call(LOOT_TOKEN_ADDR, SEL_CURRENT_PHASE)
        result["current_phase"] = _decode_uint256(raw)
    except Exception:
        result["current_phase"] = None

    # tokenBoundAccount(tokenId) — returns address
    try:
        data = SEL_TBA + _pad_uint256(token_id)
        raw = _eth_call(LOOT_TOKEN_ADDR, data)
        addr_hex = "0x" + raw[-40:]
        result["tba_address"] = addr_hex
    except Exception:
        result["tba_address"] = None

    result["token_id"] = token_id
    result["loot_token_addr"] = LOOT_TOKEN_ADDR
    result["world_registry_addr"] = WORLD_REGISTRY_ADDR
    return result

_CAST = str(Path.home() / ".foundry" / "bin" / "cast")

def _fetch_generation_history(token_id: int) -> list:
    """
    Use `cast call` to fetch + decode generationHistory(uint256) in one shot.
    cast call handles ABI encoding of the call and decodes the tuple[] response.
    """
    import subprocess
    sig = "generationHistory(uint256)((uint256,address,bytes32,bytes32,string,string[],uint8,uint256[],uint256,uint256)[])"
    try:
        result = subprocess.run(
            [_CAST, "call", WORLD_REGISTRY_ADDR, sig, str(token_id),
             "--rpc-url", ANVIL_RPC],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return []
        return _parse_cast_generation_output(result.stdout)
    except Exception:
        return []

def _decode_generation_array(hex_str: str) -> list:
    """Decode ABI-encoded Generation[] -- delegates to cast call path."""
    # This path is kept for compatibility but we prefer _fetch_generation_history
    return []

def _parse_cast_generation_output(stdout: str) -> list:
    """
    Parse `cast call` tuple[] output into Generation dicts.
    cast format: [(f0, f1, f2, f3, "str", ["a", "b"], n, [], n, n), ...]
    Strategy: tokenise respecting nested brackets and quoted strings.
    """
    import re
    raw = stdout.strip()
    if not raw or raw == "[]":
        return []

    conf_map = {0: "low", 1: "medium", 2: "high"}

    def _tokenise(s: str) -> list:
        """Split a comma-separated field list respecting [], "" nesting."""
        tokens = []
        depth = 0
        cur = []
        in_str = False
        for ch in s:
            if ch == '"' and depth == 0:
                in_str = not in_str
                cur.append(ch)
            elif in_str:
                cur.append(ch)
            elif ch in "([":
                depth += 1
                cur.append(ch)
            elif ch in ")]":
                depth -= 1
                cur.append(ch)
            elif ch == "," and depth == 0:
                tokens.append("".join(cur).strip())
                cur = []
            else:
                cur.append(ch)
        if cur:
            tokens.append("".join(cur).strip())
        return tokens

    # Strip outer brackets: [(tuple), (tuple), ...] -> "(tuple), (tuple), ..."
    inner = raw
    if inner.startswith("["):
        inner = inner[1:]
    if inner.endswith("]"):
        inner = inner[:-1]

    # Split into individual tuple strings at the top level
    tuple_strings = []
    depth = 0
    cur = []
    for ch in inner:
        if ch in "([":
            depth += 1
            cur.append(ch)
        elif ch in ")]":
            depth -= 1
            cur.append(ch)
            if depth == 0:
                tuple_strings.append("".join(cur).strip())
                cur = []
        elif ch == "," and depth == 0:
            pass  # separator between top-level tuples
        else:
            if depth > 0:
                cur.append(ch)

    generations = []
    for ts in tuple_strings:
        # Strip outer parens
        if ts.startswith("(") and ts.endswith(")"):
            ts = ts[1:-1]
        fields = _tokenise(ts)
        if len(fields) < 10:
            continue

        # Field 5 is string[] e.g. ["univ:nouns", "univ:mfers"]
        universes_raw = fields[5].strip()
        if universes_raw.startswith("[") and universes_raw.endswith("]"):
            universes_raw = universes_raw[1:-1]
        universes = [u.strip().strip('"') for u in universes_raw.split(",") if u.strip()]

        try:
            conf_val = int(fields[6])
        except (ValueError, IndexError):
            conf_val = 0

        gen = {
            "token_id": fields[0].strip(),
            "generator_address": fields[1].strip(),
            "world_bible_hash": fields[2].strip(),
            "manifest_hash": fields[3].strip(),
            "ipfs_cid": fields[4].strip().strip('"'),
            "universes_used": universes,
            "commercial_confidence": conf_map.get(conf_val, str(conf_val)),
            "block_height": fields[8].strip() if len(fields) > 8 else "",
            "timestamp": fields[9].strip() if len(fields) > 9 else "",
        }
        generations.append(gen)

    return generations


# ── New API endpoints ──────────────────────────────────────────────────────────

PHASE_CONFIG = {
    0: {"label": "Phase 1 — Agents Only", "supply": 1024, "agents_only": True,
        "description": "ERC-8004 verified agents only. This is the first enforced agents-only NFT mint. Human minting opens in Phase 3."},
    1: {"label": "Phase 2 — Agents Only", "supply": 1536, "agents_only": True,
        "description": "ERC-8004 verified agents only. First enforced agents-only mint."},
    2: {"label": "Phase 3 — Public", "supply": 1024, "agents_only": False,
        "description": "Open to all. Anti-bot measures apply."},
    3: {"label": "Reserve", "supply": 512, "agents_only": False,
        "description": "Developer grants and partnerships."},
}

PHASE_START_TOKENS = {0: 0, 1: 1024, 2: 2560, 3: 3584}


@app.get("/api/tokens/recent")
async def api_tokens_recent(limit: int = 8):
    """Walk backwards from totalMinted and return the most recently minted tokens."""
    try:
        raw = _eth_call(LOOT_TOKEN_ADDR, SEL_TOTAL_MINTED)
        total_minted = _decode_uint256(raw)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Could not reach contracts: {e}")

    tokens = []
    start = max(0, total_minted - 1)
    for token_id in range(start, max(-1, start - limit), -1):
        if token_id < 0:
            break
        try:
            # owner
            d = SEL_OWNER_OF + _pad_uint256(token_id)
            owner = "0x" + _eth_call(LOOT_TOKEN_ADDR, d)[-40:]
            # tba
            d = SEL_TBA + _pad_uint256(token_id)
            tba = "0x" + _eth_call(LOOT_TOKEN_ADDR, d)[-40:]
            # generation_count
            d = SEL_GEN_COUNT + _pad_uint256(token_id)
            gen_count = _decode_uint256(_eth_call(WORLD_REGISTRY_ADDR, d))
            # minted_at
            d = SEL_MINTED_AT + _pad_uint256(token_id)
            minted_at = _decode_uint256(_eth_call(LOOT_TOKEN_ADDR, d))
            # name from metadata
            d = SEL_TOKEN_URI + _pad_uint256(token_id)
            raw_uri = _eth_call(LOOT_TOKEN_ADDR, d)
            name = f"manifest:{token_id:04d}"
            try:
                hex_data = raw_uri[2:]
                str_len = int(hex_data[64:128], 16)
                uri = bytes.fromhex(hex_data[128:128 + str_len * 2]).decode("utf-8")
                if uri.startswith("data:application/json;base64,"):
                    import base64
                    meta = json.loads(base64.b64decode(uri[len("data:application/json;base64,"):]))
                    name = meta.get("name", name)
            except Exception:
                pass

            tokens.append({
                "token_id": token_id,
                "name": name,
                "owner": owner,
                "tba_address": tba,
                "generation_count": gen_count,
                "minted_at": minted_at,
            })
        except Exception:
            continue

    return {"tokens": tokens, "total_minted": total_minted}


@app.get("/api/mint/status")
async def api_mint_status():
    """Return current mint phase status and supply info."""
    try:
        raw = _eth_call(LOOT_TOKEN_ADDR, SEL_TOTAL_MINTED)
        total_minted = _decode_uint256(raw)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Could not reach contracts: {e}")

    try:
        raw = _eth_call(LOOT_TOKEN_ADDR, SEL_CURRENT_PHASE)
        current_phase = _decode_uint256(raw)
    except Exception:
        current_phase = 0

    phase_info = PHASE_CONFIG.get(current_phase, PHASE_CONFIG[0])
    phase_start = PHASE_START_TOKENS.get(current_phase, 0)
    minted_in_phase = max(0, total_minted - phase_start)
    remaining = max(0, phase_info["supply"] - minted_in_phase)

    return {
        "total_supply": 4096,
        "total_minted": total_minted,
        "current_phase": current_phase,
        "phase_label": phase_info["label"],
        "phase_description": phase_info["description"],
        "phase_supply": phase_info["supply"],
        "minted_in_phase": minted_in_phase,
        "remaining_in_phase": remaining,
        "contract_address": LOOT_TOKEN_ADDR,
        "registry_address": WORLD_REGISTRY_ADDR,
        "chain": "Base",
        "chain_id": 8453,
        "mint_price": "Free",
        "human_eligible": not phase_info["agents_only"],
        "agent_eligible": True,
        "agents_only": phase_info["agents_only"],
    }


@app.get("/about", response_class=HTMLResponse)
async def about_page():
    template_path = TEMPLATES_DIR / "about.html"
    if not template_path.exists():
        raise HTTPException(status_code=500, detail="about.html not found")
    return HTMLResponse(content=template_path.read_text())

@app.get("/mint", response_class=HTMLResponse)
async def mint_page():
    template_path = TEMPLATES_DIR / "mint.html"
    if not template_path.exists():
        raise HTTPException(status_code=500, detail="mint.html not found")
    return HTMLResponse(content=template_path.read_text())


@app.get("/worldkit/{token_id}", response_class=HTMLResponse)
async def worldkit_page(token_id: int):
    try:
        token_data = _fetch_token_data(token_id)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Could not reach Anvil: {e}")

    template_path = TEMPLATES_DIR / "worldkit.html"
    if not template_path.exists():
        raise HTTPException(status_code=500, detail="worldkit.html template not found")

    html = template_path.read_text()
    token_json = json.dumps(token_data, indent=2)
    html = html.replace("__TOKEN_JSON__", token_json)
    return HTMLResponse(content=html)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _find_world_file(world_id: str) -> Optional[Path]:
    # Direct filename match (slug form)
    slug = world_id.replace(":", "-")
    for f in WORLDS_DIR.glob("*.json"):
        if f.stem == slug or f.stem == world_id:
            return f
        # Check id field inside
    # Fallback: read files and check id field
    for f in WORLDS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            if data.get("id") == world_id:
                return f
        except Exception:
            pass
    return None
