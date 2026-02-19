#!/usr/bin/env python3
"""
fetch_svgs.py — Download real CC0 SVGs from IPFS for local portrait rendering.

Downloads a sample of actual artwork from CrypToadz, mfers, and racc00ns
using public IPFS gateways (no API key required).

Saves to:
  src/static/cryptoadz/svgs/toad-{n}.svg
  src/static/mfers/svgs/mfer-{n}.svg
  src/static/racc00ns/svgs/racc-{n}.svg

Usage:
    python fetch_svgs.py [--count N] [--project cryptoadz|mfers|racc00ns|all]

Defaults: --count 12, --project all
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

# ── Public IPFS gateways (tried in order) ──────────────────────────────────
GATEWAYS = [
    "https://ipfs.io/ipfs/{cid}",
    "https://cloudflare-ipfs.com/ipfs/{cid}",
    "https://dweb.link/ipfs/{cid}",
    "https://gateway.pinata.cloud/ipfs/{cid}",
]

# ── CrypToadz ───────────────────────────────────────────────────────────────
# Metadata stored on IPFS. CID for the metadata folder:
CRYPTOADZ_BASE_CID = "QmTTQUBXxYepwbXdBEPcFKXJo7gUYjXMLZurwwmpjmHdaQ"
# Token IDs to fetch (spread across collection, total supply 6969)
CRYPTOADZ_IDS = [1, 100, 250, 500, 750, 1000, 1500, 2000, 2500, 3000, 3500, 4000]

# ── mfers ────────────────────────────────────────────────────────────────────
# mfers metadata CID:
MFERS_BASE_CID = "QmWiQE65tmpYzcokCheQmng2DCM33DEhjXcPB6PanwpAZo"
MFERS_IDS = [1, 100, 300, 600, 900, 1200, 1500, 2000, 2500, 3000, 3500, 4000]

# ── racc00ns ─────────────────────────────────────────────────────────────────
# racc00ns metadata CID (Raccoon Club / Racc00ns by NFT Worlds):
RACC00NS_BASE_CID = "QmeSjSinHpPnmXmspMjwiXyN6zS4E9zccariGR3jxcaWtq"
RACC00NS_IDS = [1, 200, 500, 1000, 1500, 2000, 2500, 3000, 3500, 4000, 4500, 5000]

TIMEOUT = 20  # seconds per request


def ipfs_fetch(cid_path: str, timeout: int = TIMEOUT) -> bytes:
    """Fetch content from IPFS, trying each gateway in order."""
    last_err = None
    for gateway_template in GATEWAYS:
        url = gateway_template.format(cid=cid_path)
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "cc0-world-generator/1.0"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
            return data
        except Exception as e:
            last_err = e
            print(f"    Gateway failed ({url[:50]}...): {e}")
            time.sleep(0.5)
    raise RuntimeError(f"All IPFS gateways failed for {cid_path}: {last_err}")


def fetch_metadata(base_cid: str, token_id: int) -> dict:
    """Fetch JSON metadata for a token."""
    # Try common metadata path patterns
    paths = [
        f"{base_cid}/{token_id}",
        f"{base_cid}/{token_id}.json",
    ]
    for path in paths:
        try:
            raw = ipfs_fetch(path)
            return json.loads(raw)
        except Exception:
            continue
    raise RuntimeError(f"Could not fetch metadata for token {token_id}")


def extract_image_cid(meta: dict):
    """Extract IPFS CID from metadata image field."""
    image = meta.get("image", "")
    if not image:
        return None
    # Handle ipfs:// scheme
    if image.startswith("ipfs://"):
        return image[len("ipfs://"):]
    # Handle https://ipfs.io/ipfs/<cid> style
    if "/ipfs/" in image:
        return image.split("/ipfs/", 1)[1]
    return None


def fetch_svg_from_meta(meta: dict):
    """Try to fetch the actual SVG image referenced in metadata."""
    image = meta.get("image", "")
    if not image:
        return None

    # Direct IPFS image
    cid_path = extract_image_cid(meta)
    if cid_path:
        try:
            return ipfs_fetch(cid_path)
        except Exception as e:
            print(f"    Image fetch failed: {e}")
            return None

    # Some projects store data: URIs directly in metadata
    if image.startswith("data:image/svg+xml"):
        import base64
        import urllib.parse
        if "base64," in image:
            encoded = image.split("base64,", 1)[1]
            return base64.b64decode(encoded)
        elif image.startswith("data:image/svg+xml,"):
            raw = image[len("data:image/svg+xml,"):]
            return urllib.parse.unquote(raw).encode("utf-8")

    return None


def is_svg(data: bytes) -> bool:
    """Check if bytes look like an SVG."""
    try:
        text = data[:200].decode("utf-8", errors="replace").lower().strip()
        return "<svg" in text or "<?xml" in text
    except Exception:
        return False


def fetch_project(
    name: str,
    base_cid: str,
    token_ids: list[int],
    out_dir: Path,
    prefix: str,
    count: int,
) -> int:
    """Fetch up to `count` SVGs for a project. Returns number saved."""
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = 0
    attempted = 0

    for tid in token_ids:
        if saved >= count:
            break
        attempted += 1
        out_path = out_dir / f"{prefix}-{tid}.svg"

        if out_path.exists():
            print(f"  [{name}] #{tid}: already exists, skipping")
            saved += 1
            continue

        print(f"  [{name}] #{tid}: fetching metadata...")
        try:
            meta = fetch_metadata(base_cid, tid)
            print(f"    metadata ok (name={meta.get('name', '?')})")
        except Exception as e:
            print(f"    metadata FAILED: {e}")
            continue

        print(f"    fetching image...")
        try:
            img_data = fetch_svg_from_meta(meta)
        except Exception as e:
            print(f"    image FAILED: {e}")
            continue

        if img_data is None:
            print(f"    no image data found in metadata")
            # Try to at least save metadata image URL for debugging
            print(f"    image field: {meta.get('image', 'none')[:80]}")
            continue

        if not is_svg(img_data):
            # Could be PNG/other — note it but don't save as SVG
            print(f"    image is not SVG (first bytes: {img_data[:20]})")
            # Save a placeholder note
            note_path = out_dir / f"{prefix}-{tid}.note"
            note_path.write_text(
                f"Token {tid}: image is non-SVG format\n"
                f"image field: {meta.get('image', '')[:200]}\n"
            )
            continue

        out_path.write_bytes(img_data)
        size_kb = len(img_data) // 1024
        print(f"    saved {out_path.name} ({size_kb}KB)")
        saved += 1
        time.sleep(0.3)  # polite delay

    print(f"  [{name}] Done: {saved}/{attempted} saved")
    return saved


def main():
    parser = argparse.ArgumentParser(description="Fetch CC0 NFT SVGs from IPFS")
    parser.add_argument("--count", type=int, default=12,
                        help="Max SVGs per project (default: 12)")
    parser.add_argument("--project", default="all",
                        choices=["cryptoadz", "mfers", "racc00ns", "all"],
                        help="Which project to fetch (default: all)")
    parser.add_argument("--out-base", default="src/static",
                        help="Base static directory (default: src/static)")
    args = parser.parse_args()

    base = Path(args.out_base)
    total = 0

    projects = {
        "cryptoadz": {
            "base_cid": CRYPTOADZ_BASE_CID,
            "token_ids": CRYPTOADZ_IDS,
            "out_dir": base / "cryptoadz" / "svgs",
            "prefix": "toad",
        },
        "mfers": {
            "base_cid": MFERS_BASE_CID,
            "token_ids": MFERS_IDS,
            "out_dir": base / "mfers" / "svgs",
            "prefix": "mfer",
        },
        "racc00ns": {
            "base_cid": RACC00NS_BASE_CID,
            "token_ids": RACC00NS_IDS,
            "out_dir": base / "racc00ns" / "svgs",
            "prefix": "racc",
        },
    }

    to_run = projects if args.project == "all" else {args.project: projects[args.project]}

    print(f"Fetching CC0 SVGs from IPFS")
    print(f"  Projects: {list(to_run.keys())}")
    print(f"  Max per project: {args.count}")
    print(f"  Output base: {base}")
    print()

    for proj_name, cfg in to_run.items():
        print(f"── {proj_name} ──")
        n = fetch_project(
            name=proj_name,
            base_cid=cfg["base_cid"],
            token_ids=cfg["token_ids"],
            out_dir=cfg["out_dir"],
            prefix=cfg["prefix"],
            count=args.count,
        )
        total += n
        print()

    print(f"Total SVGs saved: {total}")
    if total > 0:
        print()
        print("Next step: run the portrait test page")
        print("  Start server: python src/web_server.py")
        print("  Open: http://localhost:8080/portrait-test")


if __name__ == "__main__":
    main()
