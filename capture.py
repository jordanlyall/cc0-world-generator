#!/usr/bin/env python3
"""
CC0 Evidence Capture Script
Captures a license declaration URL: hashes the HTML, requests an archive.org
snapshot, and outputs a pre-filled evidence JSON template.

Usage:
    python3 capture.py <universe_id> <license_url> [--contract <address>]

Examples:
    python3 capture.py nouns https://nouns.wtf/license
    python3 capture.py cryptoadz https://cryptoadz.io --contract 0x1CB1A5e65610AEFF2551A50f76a87a7d3fB649C6
"""

import argparse
import hashlib
import json
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Optional


def fetch_url(url: str) -> tuple[str, str]:
    """Fetch URL content and return (html, final_url). Follows redirects."""
    import http.client
    import urllib.parse

    # Build opener that follows redirects (including 308)
    opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; CC0-Evidence-Capture/1.0)",
            "Accept": "text/html,application/xhtml+xml,*/*"
        }
    )
    with opener.open(req, timeout=20) as resp:
        html = resp.read().decode("utf-8", errors="replace")
        return html, resp.url


def sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def request_archive_snapshot(url: str) -> str:
    """Request archive.org to save a snapshot. Returns the expected snapshot URL."""
    save_url = f"https://web.archive.org/save/{url}"
    try:
        req = urllib.request.Request(
            save_url,
            headers={"User-Agent": "CC0-Evidence-Capture/1.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            # Archive.org returns the snapshot URL in the Content-Location header
            location = resp.headers.get("Content-Location", "")
            if location:
                return f"https://web.archive.org{location}"
    except Exception as e:
        print(f"  [archive.org] Snapshot request failed: {e}", file=sys.stderr)

    # Fall back to a predictable URL pattern with today's date
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"https://web.archive.org/web/{today}000000*/{url}"


def build_evidence(universe_id: str, license_url: str, html: str,
                   final_url: str, archive_url: str,
                   contract: Optional[str], timestamp: str) -> dict:
    """Build the evidence JSON object."""
    content_hash = sha256(html)
    date_str = timestamp[:10]  # YYYY-MM-DD

    evidence = {
        "id": f"evid:cc0:{universe_id}:{date_str}",
        "type": "license_declaration",
        "claim": f"Universe univ:{universe_id} is licensed under CC0-1.0",
        "source": {
            "uri": final_url,
            "retrieved_at": timestamp,
            "method": "automated_fetch",
            "content_hash_sha256": content_hash,
            "mime_type": "text/html"
        },
        "attestation": {
            "collector": "Jordan Lyall",
            "confidence": 0.0,  # TODO: set manually (0.0-1.0)
            "notes": "TODO: describe where the CC0 declaration appears and any ambiguity"
        },
        "snapshots": [
            {
                "type": "web_archive",
                "uri": archive_url,
                "retrieved_at": timestamp,
                "content_hash_sha256": "TODO: hash after verifying archive page loads"
            }
        ]
    }

    if contract:
        evidence["contract_address"] = contract

    return evidence


def build_universe(universe_id: str, evidence_id: str,
                   contract: Optional[str]) -> dict:
    """Build a stub Universe object."""
    universe = {
        "id": f"univ:{universe_id}",
        "name": universe_id.title(),
        "kind": "nft_collection",  # TODO: adjust if needed
        "canonical_refs": [
            {"type": "url", "value": "TODO: canonical project URL"}
        ],
        "license": {
            "type": "CC0-1.0",
            "evidence_id": evidence_id
        },
        "risk_flags": [],  # TODO: add from the project file risk table
        "cmi_present": False,
        "ai_policy_signals": []
    }

    if contract:
        universe["canonical_refs"].append({
            "type": "contract",
            "value": contract
        })

    return universe


def main():
    parser = argparse.ArgumentParser(description="CC0 Evidence Capture")
    parser.add_argument("universe_id", help="Short ID for the universe (e.g. nouns)")
    parser.add_argument("license_url", help="URL of the license declaration page")
    parser.add_argument("--contract", help="Contract address (for NFT projects)", default=None)
    args = parser.parse_args()

    universe_id = args.universe_id.lower().replace(" ", "-")
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    date_str = timestamp[:10]
    out_filename = f"univ-{universe_id}-cc0-{date_str}.json"

    print(f"\nCapturing evidence for: {universe_id}")
    print(f"  License URL: {args.license_url}")
    print(f"  Timestamp:   {timestamp}\n")

    # Fetch license page
    print("  [1/3] Fetching license page...")
    try:
        html, final_url = fetch_url(args.license_url)
        content_hash = sha256(html)
        print(f"        Hash: {content_hash}")
        print(f"        Size: {len(html)} chars")
    except Exception as e:
        print(f"  ERROR: Could not fetch URL: {e}", file=sys.stderr)
        sys.exit(1)

    # Request archive snapshot
    print("  [2/3] Requesting archive.org snapshot...")
    archive_url = request_archive_snapshot(args.license_url)
    print(f"        Snapshot: {archive_url}")

    # Build output
    print("  [3/3] Building evidence JSON...")
    evidence = build_evidence(
        universe_id, args.license_url, html, final_url,
        archive_url, args.contract, timestamp
    )
    universe = build_universe(universe_id, evidence["id"], args.contract)

    output = {
        "_meta": {
            "captured_at": timestamp,
            "script_version": "1.0",
            "status": "needs_manual_review",
            "todo": [
                "Set attestation.confidence (0.0-1.0)",
                "Fill attestation.notes with where the CC0 declaration appears",
                "Add risk_flags from project file",
                "Verify archive snapshot loaded and update its hash",
                "Take and save a local screenshot",
                "Review universe.kind and adjust if needed"
            ]
        },
        "universe": universe,
        "evidence": evidence
    }

    print(f"\n  Output file: evidence/{out_filename}")
    print("\n" + "="*60)
    print(json.dumps(output, indent=2))
    print("="*60)

    # Write to evidence directory (relative to script location)
    import os
    script_dir = os.path.dirname(os.path.abspath(__file__))
    out_path = os.path.join(script_dir, "evidence", out_filename)
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nSaved to: {out_path}")
    print("\nNext: open the file, fill in the TODOs, take a screenshot, done.")


if __name__ == "__main__":
    main()
