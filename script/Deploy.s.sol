// SPDX-License-Identifier: CC0-1.0
pragma solidity ^0.8.24;

import {Script, console} from "forge-std/Script.sol";
import {CC0LootToken} from "../contracts/CC0LootToken.sol";
import {WorldRegistry} from "../contracts/WorldRegistry.sol";
import {TrustedForwarder} from "../contracts/TrustedForwarder.sol";

/// @title Deploy
/// @notice Forge deployment script for Base Sepolia (and mainnet).
///
/// Usage (Base Sepolia):
///   forge script script/Deploy.s.sol \
///     --rpc-url base_sepolia \
///     --broadcast \
///     --verify \
///     --verifier-url https://api-sepolia.basescan.org/api \
///     --etherscan-api-key $BASESCAN_API_KEY
///
/// Required env vars:
///   PRIVATE_KEY      -- deployer private key (hex, no 0x prefix)
///   BACKEND_SIGNER   -- worldkit.ai backend EOA to authorize as forwarder signer
///
/// Optional env vars:
///   BASESCAN_API_KEY -- for contract verification on Basescan
///
/// Canonical ERC-6551 addresses (same on Base mainnet + Sepolia):
///   Registry:   0x000000006551c19487814612e58FE06813775758
///   TBA Impl:   0x55266d75D1a14E4572138116aF39863Ed6596E7F
contract Deploy is Script {

    // =========================================================================
    // Canonical ERC-6551 Addresses (Base mainnet + Sepolia)
    // =========================================================================

    /// @dev ERC-6551 Registry -- canonical deployment, same address on all EVM chains
    address constant ERC6551_REGISTRY = 0x000000006551c19487814612e58FE06813775758;

    /// @dev ERC-6551 Reference TBA Implementation
    address constant TBA_IMPLEMENTATION = 0x55266d75D1a14E4572138116aF39863Ed6596E7F;

    // =========================================================================
    // Run
    // =========================================================================

    function run() external {
        uint256 deployerKey = vm.envUint("PRIVATE_KEY");
        address deployer = vm.addr(deployerKey);
        address backendSigner = vm.envAddress("BACKEND_SIGNER");

        console.log("=== Manifest Deployment ===");
        console.log("Deployer:       ", deployer);
        console.log("Backend signer: ", backendSigner);
        console.log("ERC-6551 Registry:", ERC6551_REGISTRY);
        console.log("TBA Implementation:", TBA_IMPLEMENTATION);
        console.log("");

        vm.startBroadcast(deployerKey);

        // ------------------------------------------------------------------
        // 1. Deploy CC0LootToken
        //    Constructor: (erc6551Registry, tbaImplementation, agentRegistry)
        //    Agent registry = address(0) on testnet -- Phase 2 agents-only gate
        //    is disabled when agentRegistry is zero (falls back to open mint).
        //    For mainnet, pass the canonical ERC-8004 Agent Registry address.
        // ------------------------------------------------------------------
        address agentRegistry = vm.envOr("AGENT_REGISTRY", address(0));

        CC0LootToken lootToken = new CC0LootToken(
            ERC6551_REGISTRY,
            TBA_IMPLEMENTATION,
            agentRegistry
        );
        console.log("CC0LootToken deployed:", address(lootToken));

        // ------------------------------------------------------------------
        // 2. Deploy WorldRegistry
        //    Constructor: (lootToken)
        // ------------------------------------------------------------------
        WorldRegistry worldRegistry = new WorldRegistry(address(lootToken));
        console.log("WorldRegistry deployed:", address(worldRegistry));

        // ------------------------------------------------------------------
        // 3. Deploy TrustedForwarder
        //    Constructor: (worldRegistry)
        // ------------------------------------------------------------------
        TrustedForwarder forwarder = new TrustedForwarder(address(worldRegistry));
        console.log("TrustedForwarder deployed:", address(forwarder));

        // ------------------------------------------------------------------
        // 4. Wire up: authorize worldkit.ai backend as forwarder signer
        // ------------------------------------------------------------------
        forwarder.addSigner(backendSigner);
        console.log("Backend signer authorized on TrustedForwarder");

        vm.stopBroadcast();

        // ------------------------------------------------------------------
        // Summary
        // ------------------------------------------------------------------
        console.log("");
        console.log("=== Deployment Complete ===");
        console.log("CC0LootToken:    ", address(lootToken));
        console.log("WorldRegistry:   ", address(worldRegistry));
        console.log("TrustedForwarder:", address(forwarder));
        console.log("");
        console.log("Next steps:");
        console.log("  1. Update worldkit.ai backend with contract addresses");
        console.log("  2. Call WorldRegistry.authorizeForwarder(tokenId, forwarder) per token");
        console.log("  3. Verify contracts on Basescan");
    }
}
