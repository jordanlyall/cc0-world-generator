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
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field, ConfigDict
from mcp.server.fastmcp import FastMCP
import mcpcat

# Load .env.local from repo root (two levels up from src/)
_REPO_ROOT = Path(__file__).parent.parent
_env_path = _REPO_ROOT / ".env.local"
if _env_path.exists():
    load_dotenv(_env_path)

# ── Import generator functions ────────────────────────────────────────────────

# Add src/ to path so we can import sibling module
sys.path.insert(0, str(Path(__file__).parent))

from generate import generate, validate_world, load_corpus, WORLDS_DIR  # noqa: E402

# ── On-chain setup ────────────────────────────────────────────────────────────

# Lazily initialised — only imported/connected when a tool needs it
_w3 = None
_mint_contract = None
_registry_contract = None

# Contract ABIs (minimal — only what we need)
_WORLDKIT_TOKEN_ABI = [
    {
        "name": "mintAgent",
        "type": "function",
        "inputs": [{"name": "to", "type": "address"}],
        "outputs": [{"name": "tokenId", "type": "uint256"}],
        "stateMutability": "nonpayable",
    },
    {
        "name": "tokenTBA",
        "type": "function",
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
    },
    {
        "name": "ownerOf",
        "type": "function",
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
    },
]

_WORLDKIT_REGISTRY_ABI = [
    {
        "name": "recordGeneration",
        "type": "function",
        "inputs": [
            {"name": "tokenId", "type": "uint256"},
            {"name": "worldHash", "type": "bytes32"},
            {"name": "ipfsCid", "type": "string"},
        ],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
]

_ERC20_TRANSFER_ABI = [
    {
        "name": "Transfer",
        "type": "event",
        "anonymous": False,
        "inputs": [
            {"name": "from", "indexed": True, "type": "address"},
            {"name": "to", "indexed": True, "type": "address"},
            {"name": "value", "indexed": False, "type": "uint256"},
        ],
    },
]

# Replay protection — persists for the lifetime of the server process
_used_payment_txs: set[str] = set()

MINT_PRICE_USDC = 50_000_000  # $50 USDC (6 decimals)


def _get_web3():
    """Return (and cache) a Web3 instance connected to RPC_URL."""
    global _w3
    if _w3 is None:
        try:
            from web3 import Web3
        except ImportError as e:
            raise RuntimeError("web3.py is required for on-chain operations. Run: pip install web3") from e

        rpc_url = os.getenv("RPC_URL", "http://localhost:8545")
        _w3 = Web3(Web3.HTTPProvider(rpc_url))
        if not _w3.is_connected():
            raise RuntimeError(f"Cannot connect to RPC at {rpc_url}")
    return _w3


def _get_contracts():
    """Return (and cache) WorldkitToken + WorldkitRegistry contract objects."""
    global _mint_contract, _registry_contract
    if _mint_contract is None:
        from web3 import Web3
        w3 = _get_web3()
        token_addr = os.getenv("WORLDKIT_TOKEN_ADDRESS") or os.getenv("MINT_CONTRACT_ADDRESS")
        registry_addr = os.getenv("WORLDKIT_REGISTRY_ADDRESS") or os.getenv("WORLD_REGISTRY_ADDRESS")
        if not token_addr or not registry_addr:
            raise RuntimeError(
                "Missing env vars. Set WORLDKIT_TOKEN_ADDRESS and WORLDKIT_REGISTRY_ADDRESS "
                "(or load .env.local)."
            )
        _mint_contract = w3.eth.contract(
            address=Web3.to_checksum_address(token_addr),
            abi=_WORLDKIT_TOKEN_ABI,
        )
        _registry_contract = w3.eth.contract(
            address=Web3.to_checksum_address(registry_addr),
            abi=_WORLDKIT_REGISTRY_ABI,
        )
    return _mint_contract, _registry_contract


# ── Server ────────────────────────────────────────────────────────────────────

mcp = FastMCP("cc0_mcp")
mcpcat.init()

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
    token_id: Optional[int] = Field(
        None,
        description=(
            "Optional Worldkit token ID to attribute this generation to. "
            "When provided and generation succeeds (non-refusal), records the "
            "world hash on-chain via WorldkitRegistry.recordGeneration(). "
            "The response will include 'recorded_on_chain: true' and 'registry_tx'."
        ),
        ge=1,
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


class WorldkitMintInput(BaseModel):
    """Input model for worldkit_mint."""

    model_config = ConfigDict(
        str_strip_whitespace=True,
        validate_assignment=True,
        extra="forbid",
    )

    wallet_address: str = Field(
        ...,
        description=(
            "Ethereum wallet address to receive the Worldkit token. "
            "Must be a valid checksummed or lowercased hex address (0x...)."
        ),
        min_length=42,
        max_length=42,
    )
    payment_tx: str = Field(
        ...,
        description=(
            "Transaction hash of the $50 USDC payment to the treasury address. "
            "The server will verify the on-chain Transfer event before minting."
        ),
        min_length=66,
        max_length=66,
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

        # On-chain attribution (only on success, only when token_id provided)
        if params.token_id is not None:
            try:
                from web3 import Web3
                w3 = _get_web3()
                _, registry = _get_contracts()
                world_text = json.dumps(data.get("world_bible", data))
                world_hash = Web3.keccak(text=world_text)
                minter_key = os.getenv("PRIVATE_KEY", "")
                minter_account = w3.eth.account.from_key(minter_key)
                tx = registry.functions.recordGeneration(
                    params.token_id, world_hash, ""
                ).build_transaction({
                    "from": minter_account.address,
                    "nonce": w3.eth.get_transaction_count(minter_account.address),
                    "gas": 200_000,
                    "gasPrice": w3.eth.gas_price,
                    "chainId": w3.eth.chain_id,
                })
                signed = w3.eth.account.sign_transaction(tx, minter_key)
                tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
                data["recorded_on_chain"] = True
                data["registry_tx"] = tx_hash.hex()
            except Exception as chain_err:
                data["recorded_on_chain"] = False
                data["registry_tx_error"] = str(chain_err)

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


@mcp.tool(
    name="worldkit_mint",
    annotations={
        "title": "Mint Worldkit Token",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def worldkit_mint(params: WorldkitMintInput) -> str:
    """Mint a Worldkit ERC-721 token after verifying a $50 USDC payment on-chain.

    Phase 1 (agents-only mint): Verifies the provided payment transaction hash,
    confirms a USDC Transfer of at least $50 to the treasury address, then calls
    WorldkitToken.mintAgent() to mint token to the caller's wallet.

    Payment verification steps (all must pass):
      1. tx hash not already used (replay protection)
      2. eth_getTransactionReceipt succeeds and status == 1
      3. USDC Transfer event present in logs: to == TREASURY_ADDRESS, value >= $50

    Args:
        params (WorldkitMintInput): Input containing:
            - wallet_address (str): Recipient Ethereum address (0x..., 42 chars)
            - payment_tx (str): Transaction hash of the $50 USDC payment (0x..., 66 chars)

    Returns:
        str: JSON-formatted mint result:

        Success:
        {
            "token_id": int,
            "tba_address": "0x...",
            "owner_address": "0x...",
            "minted_at": "ISO8601",
            "manifests_url": "https://worldkit.ai/manifests/{token_id}",
            "manifests_json_url": "https://worldkit.ai/manifests/{token_id}.json"
        }

        Error (any failure mode returns an error string with a clear reason):
        "Error: Payment tx already used — replay attack prevented."
        "Error: Payment tx not found on-chain or transaction failed."
        "Error: No qualifying USDC transfer found in tx {hash}. ..."
        "Error: Wallet already holds a Worldkit token (one per wallet)."

    Examples:
        - Use when: An agent has paid $50 USDC and wants to mint their Worldkit token
        - Don't use when: Payment hasn't been submitted on-chain yet
        - Don't use when: You want to generate a world (use cc0_generate_world)
    """
    try:
        from web3 import Web3

        w3 = _get_web3()

        # ── 1. Validate wallet address ─────────────────────────────────────────
        if not Web3.is_address(params.wallet_address):
            return f"Error: Invalid wallet address: {params.wallet_address!r}. Must be a valid 0x hex address."
        wallet = Web3.to_checksum_address(params.wallet_address)

        # ── 2. Normalize tx hash + replay check ───────────────────────────────
        tx_hash_norm = params.payment_tx.lower()
        if not tx_hash_norm.startswith("0x") or len(tx_hash_norm) != 66:
            return f"Error: Invalid payment_tx format: {params.payment_tx!r}. Expected 0x-prefixed 32-byte hash (66 chars)."
        if tx_hash_norm in _used_payment_txs:
            return "Error: Payment tx already used — replay attack prevented."

        # ── 3. Fetch receipt ───────────────────────────────────────────────────
        try:
            receipt = w3.eth.get_transaction_receipt(tx_hash_norm)
        except Exception as e:
            return f"Error: Could not fetch receipt for tx {params.payment_tx}: {e}"

        if receipt is None:
            return f"Error: Payment tx not found on-chain or still pending: {params.payment_tx}"
        if receipt["status"] != 1:
            return f"Error: Payment transaction reverted (status=0): {params.payment_tx}"

        # ── 4. Verify USDC Transfer event ─────────────────────────────────────
        usdc_addr = os.getenv("MOCK_USDC_ADDRESS") or os.getenv("USDC_ADDRESS")
        treasury_addr = os.getenv("DEPLOYER_ADDRESS") or os.getenv("TREASURY_ADDRESS")
        if not usdc_addr or not treasury_addr:
            return "Error: Missing env vars. Set MOCK_USDC_ADDRESS and DEPLOYER_ADDRESS (or load .env.local)."

        usdc_contract = w3.eth.contract(
            address=Web3.to_checksum_address(usdc_addr),
            abi=_ERC20_TRANSFER_ABI,
        )
        treasury_checksummed = Web3.to_checksum_address(treasury_addr)

        transfer_verified = False
        for log in receipt["logs"]:
            # Only inspect logs from the USDC contract
            if log["address"].lower() != usdc_addr.lower():
                continue
            try:
                decoded = usdc_contract.events.Transfer().process_log(log)
                if (
                    decoded["args"]["to"].lower() == treasury_checksummed.lower()
                    and decoded["args"]["value"] >= MINT_PRICE_USDC
                ):
                    transfer_verified = True
                    break
            except Exception:
                continue  # log doesn't match Transfer ABI — skip

        if not transfer_verified:
            return (
                f"Error: No qualifying USDC transfer found in tx {params.payment_tx}. "
                f"Expected Transfer(to={treasury_checksummed}, value>={MINT_PRICE_USDC}) "
                f"from USDC contract {usdc_addr}."
            )

        # ── 5. Mark tx as used (replay protection — do this before mint) ──────
        _used_payment_txs.add(tx_hash_norm)

        # ── 6. Call mintAgent as minter wallet ────────────────────────────────
        minter_key = os.getenv("BACKEND_SIGNER_KEY")
        if not minter_key:
            raise RuntimeError(
                "BACKEND_SIGNER_KEY environment variable is not set. "
                "Set it in Railway environment variables before deploying."
            )
        minter_account = w3.eth.account.from_key(minter_key)
        token_contract, _ = _get_contracts()

        try:
            mint_tx = token_contract.functions.mintAgent(wallet).build_transaction({
                "from": minter_account.address,
                "nonce": w3.eth.get_transaction_count(minter_account.address),
                "gas": 300_000,
                "gasPrice": w3.eth.gas_price,
                "chainId": w3.eth.chain_id,
            })
            signed_mint = w3.eth.account.sign_transaction(mint_tx, minter_key)
            mint_tx_hash = w3.eth.send_raw_transaction(signed_mint.raw_transaction)
            mint_receipt = w3.eth.wait_for_transaction_receipt(mint_tx_hash, timeout=60)
        except Exception as e:
            # Roll back replay guard so the caller can retry after fixing the issue
            _used_payment_txs.discard(tx_hash_norm)
            err_str = str(e)
            if "OnePerWallet" in err_str:
                return f"Error: Wallet {wallet} already holds a Worldkit token (one per wallet)."
            if "NotMinter" in err_str:
                return "Error: Backend signer is not authorized as minter on WorldkitToken."
            if "MaxSupplyReached" in err_str:
                return "Error: Max supply of 4096 tokens reached."
            return f"Error: mintAgent failed: {e}"

        if mint_receipt["status"] != 1:
            _used_payment_txs.discard(tx_hash_norm)
            return f"Error: mintAgent transaction reverted: {mint_tx_hash.hex()}"

        # ── 7. Read token ID from contract state ──────────────────────────────
        # totalMinted() == last minted token id (tokens are sequential from 1)
        try:
            # Add totalMinted to ABI on the fly — cheaper than a new contract object
            total_minted_abi = [{
                "name": "totalMinted",
                "type": "function",
                "inputs": [],
                "outputs": [{"name": "", "type": "uint256"}],
                "stateMutability": "view",
            }]
            total_contract = w3.eth.contract(
                address=token_contract.address,
                abi=total_minted_abi,
            )
            token_id = total_contract.functions.totalMinted().call()
        except Exception:
            # Fallback: count from event logs — look for Transfer(from=0x0, to=wallet)
            token_id = None
            for log in mint_receipt["logs"]:
                try:
                    erc721_transfer_abi = [{
                        "name": "Transfer",
                        "type": "event",
                        "anonymous": False,
                        "inputs": [
                            {"name": "from", "indexed": True, "type": "address"},
                            {"name": "to", "indexed": True, "type": "address"},
                            {"name": "tokenId", "indexed": True, "type": "uint256"},
                        ],
                    }]
                    nft_contract_tmp = w3.eth.contract(
                        address=token_contract.address,
                        abi=erc721_transfer_abi,
                    )
                    decoded_log = nft_contract_tmp.events.Transfer().process_log(log)
                    if (
                        decoded_log["args"]["from"] == "0x0000000000000000000000000000000000000000"
                        and decoded_log["args"]["to"].lower() == wallet.lower()
                    ):
                        token_id = decoded_log["args"]["tokenId"]
                        break
                except Exception:
                    continue

            if token_id is None:
                return (
                    f"Error: Mint succeeded (tx={mint_tx_hash.hex()}) but could not "
                    "determine token ID. Check the transaction on-chain."
                )

        # ── 8. Fetch TBA address ───────────────────────────────────────────────
        try:
            tba_address = token_contract.functions.tokenTBA(token_id).call()
        except Exception as e:
            tba_address = f"unknown (tokenTBA call failed: {e})"

        # ── 9. Verify owner ────────────────────────────────────────────────────
        try:
            owner_address = token_contract.functions.ownerOf(token_id).call()
        except Exception:
            owner_address = wallet  # best-effort fallback

        return json.dumps(
            {
                "token_id": token_id,
                "tba_address": tba_address,
                "owner_address": owner_address,
                "minted_at": datetime.now(timezone.utc).isoformat(),
                "manifests_url": f"https://worldkit.ai/manifests/{token_id}",
                "manifests_json_url": f"https://worldkit.ai/manifests/{token_id}.json",
            },
            indent=2,
        )

    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
