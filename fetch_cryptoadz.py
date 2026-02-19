#!/usr/bin/env python3
"""
fetch_cryptoadz.py — Pull CrypToadz trait data from on-chain contract.

CrypToadz contract: 0x1CB1A5e65610AEFF2551A50f76a87a7d3fB649C4 (Ethereum mainnet)
tokenURI returns: data:application/json;base64,<base64-json>
The JSON contains: {"name":..., "description":..., "image":"data:image/svg+xml;base64,..."}

This script:
1. Calls tokenURI for a sample of token IDs via public Ethereum JSON-RPC
2. Decodes the base64 SVG from each token
3. Extracts color palettes and shape vocabulary (rect/circle fill colors)
4. Writes src/static/cryptoadz/trait-data.json for use by the portrait renderer

Usage:
    python fetch_cryptoadz.py [--count N] [--out PATH]

Defaults: --count 80, --out src/static/cryptoadz/trait-data.json

No web3.py required — uses raw JSON-RPC over urllib.
"""

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from xml.etree import ElementTree as ET

CONTRACT = "0x1CB1A5e65610AEFF2551A50f76a87a7d3fB649C4"
TOTAL_SUPPLY = 6969  # CrypToadz total supply

# ABI encoding for tokenURI(uint256)
# Function selector: keccak256("tokenURI(uint256)")[:4] = 0xc87b56dd
TOKEN_URI_SELECTOR = "c87b56dd"

# Free public Ethereum RPC endpoints (try in order)
RPC_ENDPOINTS = [
    "https://1rpc.io/eth",
    "https://api.zan.top/eth-mainnet",
    "https://eth.llamarpc.com",
    "https://cloudflare-eth.com",
]


def eth_call(rpc_url: str, to: str, data: str) -> str:
    """Make a raw eth_call JSON-RPC request. Returns hex result string."""
    payload = json.dumps({
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": to, "data": data}, "latest"],
        "id": 1,
    }).encode()
    req = urllib.request.Request(
        rpc_url,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read())
    if "error" in result:
        raise ValueError(f"RPC error: {result['error']}")
    return result["result"]


def encode_token_uri_call(token_id: int) -> str:
    """Encode tokenURI(uint256) calldata."""
    # 4-byte selector + 32-byte uint256 (left-padded)
    return "0x" + TOKEN_URI_SELECTOR + hex(token_id)[2:].zfill(64)


def decode_abi_string(hex_data: str) -> str:
    """Decode ABI-encoded string return value from eth_call hex."""
    # Strip 0x
    data = hex_data[2:] if hex_data.startswith("0x") else hex_data
    # ABI string: offset (32 bytes) + length (32 bytes) + data (padded to 32 bytes)
    # offset bytes 0-31, length bytes 32-63, string data from byte 64
    if len(data) < 128:
        raise ValueError("Response too short to be ABI-encoded string")
    str_length = int(data[64:128], 16)
    str_hex = data[128:128 + str_length * 2]
    return bytes.fromhex(str_hex).decode("utf-8")


def get_token_uri(rpc_url: str, token_id: int) -> str:
    """Fetch tokenURI for a given token_id."""
    calldata = encode_token_uri_call(token_id)
    hex_result = eth_call(rpc_url, CONTRACT, calldata)
    return decode_abi_string(hex_result)


def decode_token_uri(token_uri: str) -> dict:
    """Decode data:application/json;base64,... URI to dict."""
    prefix = "data:application/json;base64,"
    if not token_uri.startswith(prefix):
        raise ValueError(f"Unexpected tokenURI format: {token_uri[:60]}")
    json_bytes = base64.b64decode(token_uri[len(prefix):])
    return json.loads(json_bytes)


def decode_svg(image_uri: str) -> str:
    """Decode data:image/svg+xml;base64,... URI to SVG string."""
    prefix = "data:image/svg+xml;base64,"
    if image_uri.startswith(prefix):
        return base64.b64decode(image_uri[len(prefix):]).decode("utf-8")
    # Some may be plain data:image/svg+xml,<url-encoded>
    prefix2 = "data:image/svg+xml,"
    if image_uri.startswith(prefix2):
        import urllib.parse
        return urllib.parse.unquote(image_uri[len(prefix2):])
    raise ValueError(f"Unexpected image URI format: {image_uri[:60]}")


def extract_colors_from_svg(svg_string: str) -> list[str]:
    """
    Extract unique fill/stroke colors from SVG elements.
    Returns a list of hex color strings (with #).
    """
    colors = set()
    # Regex match fill="#xxxxxx" and stroke="#xxxxxx" (3 or 6 hex digits)
    for match in re.finditer(r'(?:fill|stroke)="(#[0-9a-fA-F]{3,8})"', svg_string):
        color = match.group(1).lower()
        # Skip transparent/none/black/white unless they're actually used as toad colors
        if color not in ("#000000", "#ffffff", "#000", "#fff", "#00000000"):
            colors.add(color)
    return sorted(colors)


def extract_background_color(svg_string: str) -> str | None:
    """Extract the background rect fill color (first rect with full-width fill)."""
    match = re.search(r'<rect[^>]*width="100%"[^>]*fill="([^"]+)"', svg_string)
    if match:
        return match.group(1).lower()
    # Try first rect element
    match = re.search(r'<rect[^>]*fill="(#[0-9a-fA-F]{3,6})"', svg_string)
    if match:
        return match.group(1).lower()
    return None


def extract_shape_primitives(svg_string: str) -> dict:
    """
    Extract shape vocabulary: rect counts, circle/ellipse counts, polygon counts.
    Gives a feel for the geometric character of toad art.
    """
    return {
        "rects": len(re.findall(r"<rect\b", svg_string)),
        "circles": len(re.findall(r"<circle\b", svg_string)),
        "ellipses": len(re.findall(r"<ellipse\b", svg_string)),
        "polygons": len(re.findall(r"<polygon\b", svg_string)),
        "paths": len(re.findall(r"<path\b", svg_string)),
    }


def find_working_rpc() -> str:
    """Try each RPC endpoint until one responds."""
    for url in RPC_ENDPOINTS:
        try:
            # Simple call: eth_blockNumber
            payload = json.dumps({
                "jsonrpc": "2.0",
                "method": "eth_blockNumber",
                "params": [],
                "id": 1,
            }).encode()
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                result = json.loads(resp.read())
            if "result" in result:
                print(f"  Using RPC: {url}")
                return url
        except Exception as e:
            print(f"  RPC {url} failed: {e}")
    raise RuntimeError("All RPC endpoints failed. Check your internet connection.")


def build_trait_vocabulary(token_data: list[dict]) -> dict:
    """
    Aggregate extracted token data into a trait vocabulary for the renderer.

    Returns a JSON structure with:
    - palette: list of unique colors seen across tokens (most frequent first)
    - backgrounds: list of unique background colors
    - body_colors: list of green/toad-body colors
    - accent_colors: list of bright eye/feature colors
    - shape_profile: avg primitive counts (characterizes the pixel art style)
    - sample_svgs: list of 5 inline SVG strings for direct embedding (smallest ones)
    - token_ids_sampled: list of token IDs that were successfully fetched
    """
    all_colors = []
    backgrounds = []
    sample_svgs = []
    shape_stats = []
    token_ids = []

    for td in token_data:
        all_colors.extend(td.get("colors", []))
        bg = td.get("background")
        if bg:
            backgrounds.append(bg)
        shape_stats.append(td.get("shapes", {}))
        token_ids.append(td.get("token_id"))

    # Count color frequency
    color_freq = {}
    for c in all_colors:
        color_freq[c] = color_freq.get(c, 0) + 1
    palette = [c for c, _ in sorted(color_freq.items(), key=lambda x: -x[1])]

    # Unique backgrounds
    bg_freq = {}
    for bg in backgrounds:
        bg_freq[bg] = bg_freq.get(bg, 0) + 1
    unique_backgrounds = [bg for bg, _ in sorted(bg_freq.items(), key=lambda x: -x[1])]

    # Characterize toad body colors: typically greens (#3-7 char hex with 4-7 as first char)
    body_colors = [c for c in palette if _is_green_ish(c)]
    accent_colors = [c for c in palette if _is_bright(c)]
    neutral_darks = [c for c in palette if _is_dark(c)]

    # Average shape profile
    def avg_stat(key):
        vals = [s.get(key, 0) for s in shape_stats if s]
        return round(sum(vals) / len(vals), 1) if vals else 0

    shape_profile = {
        "avg_rects": avg_stat("rects"),
        "avg_circles": avg_stat("circles"),
        "avg_ellipses": avg_stat("ellipses"),
        "avg_polygons": avg_stat("polygons"),
        "avg_paths": avg_stat("paths"),
    }

    # Collect small SVGs for direct use (prefer ones with path data = more interesting)
    svg_candidates = sorted(
        [td for td in token_data if td.get("svg") and len(td["svg"]) < 8000],
        key=lambda x: len(x["svg"])
    )
    sample_svgs = [td["svg"] for td in svg_candidates[:8]]

    return {
        "source": "CrypToadz by GREMPLIN",
        "contract": CONTRACT,
        "license": "CC0-1.0",
        "evidence_id": "evid:cc0:cryptoadz:2026-02-18",
        "generated_at": _utc_now(),
        "tokens_sampled": len(token_data),
        "token_ids_sampled": [tid for tid in token_ids if tid is not None],
        "palette": palette[:80],  # top 80 colors by frequency
        "backgrounds": unique_backgrounds[:20],
        "body_colors": body_colors[:20],
        "accent_colors": accent_colors[:20],
        "neutral_darks": neutral_darks[:20],
        "shape_profile": shape_profile,
        "sample_svgs": sample_svgs,
    }


def _is_green_ish(hex_color: str) -> bool:
    """True if color is in the green spectrum (toad body range)."""
    c = hex_color.lstrip("#")
    if len(c) == 3:
        c = c[0]*2 + c[1]*2 + c[2]*2
    if len(c) != 6:
        return False
    try:
        r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
        return g > r and g > b and g > 60
    except Exception:
        return False


def _is_bright(hex_color: str) -> bool:
    """True if color is high-saturation (eye/accent colors)."""
    c = hex_color.lstrip("#")
    if len(c) == 3:
        c = c[0]*2 + c[1]*2 + c[2]*2
    if len(c) != 6:
        return False
    try:
        r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
        max_c = max(r, g, b)
        min_c = min(r, g, b)
        return max_c > 150 and (max_c - min_c) > 100
    except Exception:
        return False


def _is_dark(hex_color: str) -> bool:
    """True if color is dark (shadow/outline colors)."""
    c = hex_color.lstrip("#")
    if len(c) == 3:
        c = c[0]*2 + c[1]*2 + c[2]*2
    if len(c) != 6:
        return False
    try:
        r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
        return (r + g + b) < 200
    except Exception:
        return False


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    parser = argparse.ArgumentParser(description="Fetch CrypToadz trait data from on-chain contract")
    parser.add_argument("--count", type=int, default=80,
                        help="Number of tokens to sample (default: 80)")
    parser.add_argument("--out", default="src/static/cryptoadz/trait-data.json",
                        help="Output JSON path (default: src/static/cryptoadz/trait-data.json)")
    parser.add_argument("--skip-rpc-check", action="store_true",
                        help="Skip RPC connectivity check (use first endpoint)")
    args = parser.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"CrypToadz trait fetcher")
    print(f"  Contract: {CONTRACT}")
    print(f"  Tokens to sample: {args.count}")
    print(f"  Output: {out_path}")
    print()

    # Find a working RPC
    print("Finding working RPC endpoint...")
    if args.skip_rpc_check:
        rpc_url = RPC_ENDPOINTS[0]
        print(f"  Skipping check, using: {rpc_url}")
    else:
        rpc_url = find_working_rpc()
    print()

    # Select token IDs to sample — spread across the collection
    # Use every Nth token to get diversity, plus a few early tokens (lower IDs often simpler)
    step = max(1, TOTAL_SUPPLY // args.count)
    token_ids = list(range(1, TOTAL_SUPPLY + 1, step))[:args.count]
    print(f"Sampling token IDs: {token_ids[:5]}...{token_ids[-3:]} ({len(token_ids)} total)")
    print()

    token_data = []
    errors = 0
    for i, tid in enumerate(token_ids):
        try:
            uri = get_token_uri(rpc_url, tid)
            meta = decode_token_uri(uri)
            svg = decode_svg(meta["image"])
            colors = extract_colors_from_svg(svg)
            background = extract_background_color(svg)
            shapes = extract_shape_primitives(svg)
            token_data.append({
                "token_id": tid,
                "name": meta.get("name", f"CrypToadz #{tid}"),
                "colors": colors,
                "background": background,
                "shapes": shapes,
                "svg": svg,
            })
            print(f"  [{i+1}/{len(token_ids)}] Token #{tid}: {len(colors)} colors, bg={background}")
        except Exception as e:
            errors += 1
            print(f"  [{i+1}/{len(token_ids)}] Token #{tid}: ERROR — {e}")
            if errors > 10:
                print("  Too many errors, stopping early.")
                break
        # Small delay to be polite to public RPCs
        time.sleep(0.15)

    if not token_data:
        print("\nNo tokens fetched successfully. Check RPC connectivity.")
        sys.exit(1)

    print(f"\nFetched {len(token_data)} tokens ({errors} errors).")
    print("Building trait vocabulary...")
    vocab = build_trait_vocabulary(token_data)

    with open(out_path, "w") as f:
        json.dump(vocab, f, indent=2)

    print(f"\nWrote {out_path}")
    print(f"  Palette: {len(vocab['palette'])} colors")
    print(f"  Backgrounds: {len(vocab['backgrounds'])} unique")
    print(f"  Body (green) colors: {len(vocab['body_colors'])}")
    print(f"  Accent colors: {len(vocab['accent_colors'])}")
    print(f"  Sample SVGs: {len(vocab['sample_svgs'])} (for direct embedding)")
    print(f"  Shape profile: {vocab['shape_profile']}")
    print()
    print("Next step: update svgCryptoadz() in src/templates/world.html")
    print("  The renderer can now use /static/cryptoadz/trait-data.json")
    print("  Sample SVGs can be embedded directly for authentic CC0 art.")


if __name__ == "__main__":
    main()
