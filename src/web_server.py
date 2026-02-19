#!/usr/bin/env python3
"""
CC0 World Generator — Phase 2 Web Server
Usage: uvicorn src.web_server:app --reload --port 8080
       (from project root with venv active)
"""

import json
import uuid
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ── Paths ─────────────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent.parent
WORLDS_DIR = ROOT / "worlds"
TEMPLATES_DIR = Path(__file__).parent / "templates"
WORLDS_DIR.mkdir(exist_ok=True)

# ── In-memory job store ───────────────────────────────────────────────────────
# Maps job_id -> {"status": "pending|running|done|error", "world_id": ..., "error": ...}

jobs: dict[str, dict] = {}

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="CC0 World Generator", version="0.2.0")

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
async def generate(req: GenerateRequest, background_tasks: BackgroundTasks):
    prompt = req.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
    if len(prompt) > 300:
        raise HTTPException(status_code=400, detail="prompt must be 300 characters or fewer")

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
