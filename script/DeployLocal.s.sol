// SPDX-License-Identifier: CC0-1.0
pragma solidity ^0.8.24;

import {Script, console} from "forge-std/Script.sol";
import {CC0LootToken} from "../contracts/CC0LootToken.sol";
import {WorldRegistry} from "../contracts/WorldRegistry.sol";
import {TrustedForwarder} from "../contracts/TrustedForwarder.sol";

/// @notice Mock ERC-6551 Registry for local dev -- returns deterministic addresses
contract MockERC6551Registry {
    mapping(uint256 => address) public accounts;

    function createAccount(
        address, bytes32, uint256, address, uint256 tokenId
    ) external returns (address) {
        address mockAccount = address(uint160(uint256(keccak256(abi.encode("tba", tokenId)))));
        accounts[tokenId] = mockAccount;
        return mockAccount;
    }

    function account(
        address, bytes32, uint256, address, uint256 tokenId
    ) external view returns (address) {
        return address(uint160(uint256(keccak256(abi.encode("tba", tokenId)))));
    }
}

/// @title DeployLocal
/// @notice Local Anvil deployment using mock ERC-6551 (no canonical registry needed)
contract DeployLocal is Script {
    function run() external {
        uint256 deployerKey = vm.envUint("PRIVATE_KEY");
        address deployer = vm.addr(deployerKey);
        address backendSigner = vm.envAddress("BACKEND_SIGNER");

        console.log("=== Local Dev Deployment ===");
        console.log("Deployer:       ", deployer);
        console.log("Backend signer: ", backendSigner);

        vm.startBroadcast(deployerKey);

        // 1. Deploy mock ERC-6551 Registry
        MockERC6551Registry mockRegistry = new MockERC6551Registry();
        console.log("MockERC6551Registry:", address(mockRegistry));

        // 2. Deploy CC0LootToken with mock registry, zero TBA impl, no agent gating
        CC0LootToken lootToken = new CC0LootToken(
            address(mockRegistry),
            address(0x1), // dummy TBA impl (mock doesn't use it)
            address(0)    // no agent registry -- Phase 1 open mint
        );
        console.log("CC0LootToken:   ", address(lootToken));

        // 3. Deploy WorldRegistry
        WorldRegistry worldRegistry = new WorldRegistry(address(lootToken));
        console.log("WorldRegistry:  ", address(worldRegistry));

        // 4. Deploy TrustedForwarder
        TrustedForwarder forwarder = new TrustedForwarder(address(worldRegistry));
        console.log("TrustedForwarder:", address(forwarder));

        // 5. Wire backend signer
        forwarder.addSigner(backendSigner);
        console.log("Backend signer authorized");

        vm.stopBroadcast();

        console.log("");
        console.log("=== Deployment Complete ===");
        console.log("CC0LootToken:    ", address(lootToken));
        console.log("WorldRegistry:   ", address(worldRegistry));
        console.log("TrustedForwarder:", address(forwarder));
    }
}
