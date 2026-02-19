// SPDX-License-Identifier: CC0-1.0
pragma solidity ^0.8.24;

import "forge-std/Test.sol";
import "../../contracts/WorldRegistry.sol";

/// @notice Mock CC0LootToken for isolated WorldRegistry testing
contract MockCC0LootToken {
    mapping(uint256 => address) private _owners;
    mapping(uint256 => address) private _tbas;

    function setOwner(uint256 tokenId, address owner) external {
        _owners[tokenId] = owner;
    }

    function setTBA(uint256 tokenId, address tba) external {
        _tbas[tokenId] = tba;
    }

    function ownerOf(uint256 tokenId) external view returns (address) {
        return _owners[tokenId];
    }

    function tokenBoundAccount(uint256 tokenId) external view returns (address) {
        return _tbas[tokenId];
    }
}

contract WorldRegistryTest is Test {
    WorldRegistry public registry;
    MockCC0LootToken public mockToken;

    address public registryOwner = address(0x1);
    address public tokenOwner = address(0x2);
    address public tba = address(0x3);
    address public forwarder = address(0x4);
    address public stranger = address(0x5);

    uint256 public constant TOKEN_ID = 1;

    bytes32 public constant WORLD_BIBLE_HASH = keccak256("world bible content");
    bytes32 public constant MANIFEST_HASH = keccak256("manifest content");
    string public constant IPFS_CID = "bafybeigdyrzt5sfp7udm7hu76uh7y26nf3efuylqabf3oclgtqy55fbzdi";

    string[] public universes;
    uint256[] public noDerived;

    function setUp() public {
        mockToken = new MockCC0LootToken();

        vm.prank(registryOwner);
        registry = new WorldRegistry(address(mockToken));

        // Wire up token 1: owner = tokenOwner, TBA = tba
        mockToken.setOwner(TOKEN_ID, tokenOwner);
        mockToken.setTBA(TOKEN_ID, tba);

        universes.push("univ:nouns");
        universes.push("univ:cryptoadz");
    }

    // =========================================================
    // Forwarder Authorization
    // =========================================================

    function testAuthorizeForwarder_ByOwner() public {
        vm.prank(tokenOwner);
        registry.authorizeForwarder(TOKEN_ID, forwarder);

        assertEq(registry.authorizedForwarder(TOKEN_ID), forwarder);
    }

    function testAuthorizeForwarder_ByTBA() public {
        vm.prank(tba);
        registry.authorizeForwarder(TOKEN_ID, forwarder);

        assertEq(registry.authorizedForwarder(TOKEN_ID), forwarder);
    }

    function testAuthorizeForwarder_StrangerReverts() public {
        vm.prank(stranger);
        vm.expectRevert(WorldRegistry.NotAuthorized.selector);
        registry.authorizeForwarder(TOKEN_ID, forwarder);
    }

    function testAuthorizeForwarder_EmitsEvent() public {
        vm.prank(tokenOwner);
        vm.expectEmit(true, false, false, true);
        emit WorldRegistry.ForwarderAuthorized(TOKEN_ID, forwarder);
        registry.authorizeForwarder(TOKEN_ID, forwarder);
    }

    // =========================================================
    // Forwarder Revocation
    // =========================================================

    function testRevokeForwarder_ByOwner() public {
        vm.prank(tokenOwner);
        registry.authorizeForwarder(TOKEN_ID, forwarder);

        vm.prank(tokenOwner);
        registry.revokeForwarder(TOKEN_ID);

        assertEq(registry.authorizedForwarder(TOKEN_ID), address(0));
    }

    function testRevokeForwarder_ByTBA() public {
        vm.prank(tba);
        registry.authorizeForwarder(TOKEN_ID, forwarder);

        vm.prank(tba);
        registry.revokeForwarder(TOKEN_ID);

        assertEq(registry.authorizedForwarder(TOKEN_ID), address(0));
    }

    function testRevokeForwarder_StrangerReverts() public {
        vm.prank(tokenOwner);
        registry.authorizeForwarder(TOKEN_ID, forwarder);

        vm.prank(stranger);
        vm.expectRevert(WorldRegistry.NotAuthorized.selector);
        registry.revokeForwarder(TOKEN_ID);
    }

    function testRevokeForwarder_EmitsEvent() public {
        vm.prank(tokenOwner);
        registry.authorizeForwarder(TOKEN_ID, forwarder);

        vm.prank(tokenOwner);
        vm.expectEmit(true, false, false, false);
        emit WorldRegistry.ForwarderRevoked(TOKEN_ID);
        registry.revokeForwarder(TOKEN_ID);
    }

    // =========================================================
    // recordGeneration -- Authorization
    // =========================================================

    function testRecordGeneration_ByOwner() public {
        vm.prank(tokenOwner);
        registry.recordGeneration(
            TOKEN_ID,
            WORLD_BIBLE_HASH,
            MANIFEST_HASH,
            IPFS_CID,
            universes,
            1,
            noDerived
        );

        assertEq(registry.generationCount(TOKEN_ID), 1);
    }

    function testRecordGeneration_ByTBA() public {
        vm.prank(tba);
        registry.recordGeneration(
            TOKEN_ID,
            WORLD_BIBLE_HASH,
            MANIFEST_HASH,
            IPFS_CID,
            universes,
            0,
            noDerived
        );

        assertEq(registry.generationCount(TOKEN_ID), 1);
    }

    function testRecordGeneration_ByAuthorizedForwarder() public {
        vm.prank(tokenOwner);
        registry.authorizeForwarder(TOKEN_ID, forwarder);

        vm.prank(forwarder);
        registry.recordGeneration(
            TOKEN_ID,
            WORLD_BIBLE_HASH,
            MANIFEST_HASH,
            IPFS_CID,
            universes,
            2,
            noDerived
        );

        assertEq(registry.generationCount(TOKEN_ID), 1);
    }

    function testRecordGeneration_StrangerReverts() public {
        vm.prank(stranger);
        vm.expectRevert(WorldRegistry.NotAuthorized.selector);
        registry.recordGeneration(
            TOKEN_ID,
            WORLD_BIBLE_HASH,
            MANIFEST_HASH,
            IPFS_CID,
            universes,
            1,
            noDerived
        );
    }

    function testRecordGeneration_UnauthorizedForwarderReverts() public {
        // forwarder not yet authorized
        vm.prank(forwarder);
        vm.expectRevert(WorldRegistry.NotAuthorized.selector);
        registry.recordGeneration(
            TOKEN_ID,
            WORLD_BIBLE_HASH,
            MANIFEST_HASH,
            IPFS_CID,
            universes,
            1,
            noDerived
        );
    }

    function testRecordGeneration_RevokedForwarderReverts() public {
        vm.prank(tokenOwner);
        registry.authorizeForwarder(TOKEN_ID, forwarder);

        vm.prank(tokenOwner);
        registry.revokeForwarder(TOKEN_ID);

        vm.prank(forwarder);
        vm.expectRevert(WorldRegistry.NotAuthorized.selector);
        registry.recordGeneration(
            TOKEN_ID,
            WORLD_BIBLE_HASH,
            MANIFEST_HASH,
            IPFS_CID,
            universes,
            1,
            noDerived
        );
    }

    // =========================================================
    // recordGeneration -- Input Validation
    // =========================================================

    function testRecordGeneration_ZeroWorldBibleHashReverts() public {
        vm.prank(tokenOwner);
        vm.expectRevert(WorldRegistry.InvalidHashes.selector);
        registry.recordGeneration(
            TOKEN_ID,
            bytes32(0),
            MANIFEST_HASH,
            IPFS_CID,
            universes,
            1,
            noDerived
        );
    }

    function testRecordGeneration_ZeroManifestHashReverts() public {
        vm.prank(tokenOwner);
        vm.expectRevert(WorldRegistry.InvalidHashes.selector);
        registry.recordGeneration(
            TOKEN_ID,
            WORLD_BIBLE_HASH,
            bytes32(0),
            IPFS_CID,
            universes,
            1,
            noDerived
        );
    }

    function testRecordGeneration_EmptyUniversesReverts() public {
        string[] memory empty;
        vm.prank(tokenOwner);
        vm.expectRevert(WorldRegistry.InvalidUniverses.selector);
        registry.recordGeneration(
            TOKEN_ID,
            WORLD_BIBLE_HASH,
            MANIFEST_HASH,
            IPFS_CID,
            empty,
            1,
            noDerived
        );
    }

    function testRecordGeneration_TooManyUniversesReverts() public {
        string[] memory tooMany = new string[](6);
        for (uint256 i = 0; i < 6; i++) {
            tooMany[i] = "univ:x";
        }
        vm.prank(tokenOwner);
        vm.expectRevert(WorldRegistry.InvalidUniverses.selector);
        registry.recordGeneration(
            TOKEN_ID,
            WORLD_BIBLE_HASH,
            MANIFEST_HASH,
            IPFS_CID,
            tooMany,
            1,
            noDerived
        );
    }

    function testRecordGeneration_MaxFiveUniversesSucceeds() public {
        string[] memory five = new string[](5);
        for (uint256 i = 0; i < 5; i++) {
            five[i] = "univ:x";
        }
        vm.prank(tokenOwner);
        registry.recordGeneration(
            TOKEN_ID,
            WORLD_BIBLE_HASH,
            MANIFEST_HASH,
            IPFS_CID,
            five,
            1,
            noDerived
        );
        assertEq(registry.generationCount(TOKEN_ID), 1);
    }

    function testRecordGeneration_CommercialConfidenceAbove2Reverts() public {
        vm.prank(tokenOwner);
        vm.expectRevert(WorldRegistry.InvalidUniverses.selector);
        registry.recordGeneration(
            TOKEN_ID,
            WORLD_BIBLE_HASH,
            MANIFEST_HASH,
            IPFS_CID,
            universes,
            3,
            noDerived
        );
    }

    function testRecordGeneration_AllConfidenceLevelsSucceed() public {
        for (uint8 conf = 0; conf <= 2; conf++) {
            uint256 tokenId = conf + 10; // use different token IDs to avoid count confusion
            mockToken.setOwner(tokenId, tokenOwner);
            mockToken.setTBA(tokenId, tba);

            vm.prank(tokenOwner);
            registry.recordGeneration(
                tokenId,
                WORLD_BIBLE_HASH,
                MANIFEST_HASH,
                IPFS_CID,
                universes,
                conf,
                noDerived
            );
            assertEq(registry.generationCount(tokenId), 1);
        }
    }

    // =========================================================
    // recordGeneration -- Data Integrity
    // =========================================================

    function testRecordGeneration_DataStoredCorrectly() public {
        vm.prank(tokenOwner);
        registry.recordGeneration(
            TOKEN_ID,
            WORLD_BIBLE_HASH,
            MANIFEST_HASH,
            IPFS_CID,
            universes,
            1,
            noDerived
        );

        WorldRegistry.Generation memory gen = registry.getGeneration(TOKEN_ID, 0);

        assertEq(gen.tokenId, TOKEN_ID);
        assertEq(gen.generatorAddress, tba);
        assertEq(gen.worldBibleHash, WORLD_BIBLE_HASH);
        assertEq(gen.manifestHash, MANIFEST_HASH);
        assertEq(gen.ipfsCid, IPFS_CID);
        assertEq(gen.commercialConfidence, 1);
        assertEq(gen.universesUsed.length, 2);
        assertEq(gen.derivedFromTokenIds.length, 0);
        assertEq(gen.blockHeight, block.number);
        assertEq(gen.timestamp, block.timestamp);
    }

    function testRecordGeneration_EmitsEvent() public {
        vm.prank(tokenOwner);
        vm.expectEmit(true, true, false, true);
        emit WorldRegistry.GenerationRecorded(
            TOKEN_ID,
            tba,
            WORLD_BIBLE_HASH,
            MANIFEST_HASH,
            IPFS_CID,
            1,
            0
        );
        registry.recordGeneration(
            TOKEN_ID,
            WORLD_BIBLE_HASH,
            MANIFEST_HASH,
            IPFS_CID,
            universes,
            1,
            noDerived
        );
    }

    // =========================================================
    // Global Index
    // =========================================================

    function testTotalGenerationsIncrements() public {
        assertEq(registry.totalGenerations(), 0);

        vm.prank(tokenOwner);
        registry.recordGeneration(TOKEN_ID, WORLD_BIBLE_HASH, MANIFEST_HASH, IPFS_CID, universes, 1, noDerived);
        assertEq(registry.totalGenerations(), 1);

        // Record a second generation for the same token
        vm.prank(tokenOwner);
        registry.recordGeneration(TOKEN_ID, WORLD_BIBLE_HASH, MANIFEST_HASH, IPFS_CID, universes, 0, noDerived);
        assertEq(registry.totalGenerations(), 2);
    }

    function testGenerationIndexToTokenId() public {
        uint256 tokenId2 = 2;
        mockToken.setOwner(tokenId2, tokenOwner);
        mockToken.setTBA(tokenId2, address(0x99));

        vm.prank(tokenOwner);
        registry.recordGeneration(TOKEN_ID, WORLD_BIBLE_HASH, MANIFEST_HASH, IPFS_CID, universes, 1, noDerived);

        vm.prank(tokenOwner);
        registry.recordGeneration(tokenId2, WORLD_BIBLE_HASH, MANIFEST_HASH, IPFS_CID, universes, 0, noDerived);

        assertEq(registry.generationIndexToTokenId(0), TOKEN_ID);
        assertEq(registry.generationIndexToTokenId(1), tokenId2);
    }

    // =========================================================
    // View Functions
    // =========================================================

    function testGenerationCount_StartsAtZero() public view {
        assertEq(registry.generationCount(TOKEN_ID), 0);
    }

    function testGenerationHistory_ReturnsAll() public {
        vm.prank(tokenOwner);
        registry.recordGeneration(TOKEN_ID, WORLD_BIBLE_HASH, MANIFEST_HASH, IPFS_CID, universes, 0, noDerived);

        vm.prank(tokenOwner);
        registry.recordGeneration(TOKEN_ID, keccak256("v2"), keccak256("m2"), IPFS_CID, universes, 1, noDerived);

        WorldRegistry.Generation[] memory history = registry.generationHistory(TOKEN_ID);
        assertEq(history.length, 2);
        assertEq(history[0].commercialConfidence, 0);
        assertEq(history[1].commercialConfidence, 1);
    }

    function testLatestGeneration_ReturnsLast() public {
        vm.prank(tokenOwner);
        registry.recordGeneration(TOKEN_ID, WORLD_BIBLE_HASH, MANIFEST_HASH, IPFS_CID, universes, 0, noDerived);

        vm.prank(tokenOwner);
        registry.recordGeneration(TOKEN_ID, keccak256("v2"), keccak256("m2"), IPFS_CID, universes, 2, noDerived);

        WorldRegistry.Generation memory latest = registry.latestGeneration(TOKEN_ID);
        assertEq(latest.commercialConfidence, 2);
    }

    function testLatestGeneration_RevertsOnEmpty() public {
        vm.expectRevert("no generations");
        registry.latestGeneration(TOKEN_ID);
    }

    // =========================================================
    // Composition Graph (getAncestors)
    // =========================================================

    function testGetAncestors_NoParents() public {
        vm.prank(tokenOwner);
        registry.recordGeneration(TOKEN_ID, WORLD_BIBLE_HASH, MANIFEST_HASH, IPFS_CID, universes, 1, noDerived);

        uint256[] memory ancestors = registry.getAncestors(TOKEN_ID, 0, 10);
        assertEq(ancestors.length, 0);
    }

    function testGetAncestors_SingleParent() public {
        uint256 parentId = 2;
        mockToken.setOwner(parentId, tokenOwner);
        mockToken.setTBA(parentId, address(0x20));

        // Record parent generation first
        vm.prank(tokenOwner);
        registry.recordGeneration(parentId, WORLD_BIBLE_HASH, MANIFEST_HASH, IPFS_CID, universes, 1, noDerived);

        // Record child generation derived from parent
        uint256[] memory parents = new uint256[](1);
        parents[0] = parentId;

        vm.prank(tokenOwner);
        registry.recordGeneration(TOKEN_ID, WORLD_BIBLE_HASH, MANIFEST_HASH, IPFS_CID, universes, 1, parents);

        uint256[] memory ancestors = registry.getAncestors(TOKEN_ID, 0, 10);
        assertEq(ancestors.length, 1);
        assertEq(ancestors[0], parentId);
    }

    function testGetAncestors_TwoLevels() public {
        uint256 grandparentId = 3;
        uint256 parentId = 2;

        mockToken.setOwner(grandparentId, tokenOwner);
        mockToken.setTBA(grandparentId, address(0x30));
        mockToken.setOwner(parentId, tokenOwner);
        mockToken.setTBA(parentId, address(0x20));

        // Grandparent generation (no parents)
        vm.prank(tokenOwner);
        registry.recordGeneration(grandparentId, WORLD_BIBLE_HASH, MANIFEST_HASH, IPFS_CID, universes, 1, noDerived);

        // Parent derived from grandparent
        uint256[] memory grandparents = new uint256[](1);
        grandparents[0] = grandparentId;
        vm.prank(tokenOwner);
        registry.recordGeneration(parentId, WORLD_BIBLE_HASH, MANIFEST_HASH, IPFS_CID, universes, 1, grandparents);

        // Child derived from parent
        uint256[] memory parents = new uint256[](1);
        parents[0] = parentId;
        vm.prank(tokenOwner);
        registry.recordGeneration(TOKEN_ID, WORLD_BIBLE_HASH, MANIFEST_HASH, IPFS_CID, universes, 1, parents);

        uint256[] memory ancestors = registry.getAncestors(TOKEN_ID, 0, 10);
        assertEq(ancestors.length, 2);
        assertEq(ancestors[0], parentId);
        assertEq(ancestors[1], grandparentId);
    }

    function testGetAncestors_DepthLimitRespected() public {
        uint256 grandparentId = 3;
        uint256 parentId = 2;

        mockToken.setOwner(grandparentId, tokenOwner);
        mockToken.setTBA(grandparentId, address(0x30));
        mockToken.setOwner(parentId, tokenOwner);
        mockToken.setTBA(parentId, address(0x20));

        vm.prank(tokenOwner);
        registry.recordGeneration(grandparentId, WORLD_BIBLE_HASH, MANIFEST_HASH, IPFS_CID, universes, 1, noDerived);

        uint256[] memory grandparents = new uint256[](1);
        grandparents[0] = grandparentId;
        vm.prank(tokenOwner);
        registry.recordGeneration(parentId, WORLD_BIBLE_HASH, MANIFEST_HASH, IPFS_CID, universes, 1, grandparents);

        uint256[] memory parents = new uint256[](1);
        parents[0] = parentId;
        vm.prank(tokenOwner);
        registry.recordGeneration(TOKEN_ID, WORLD_BIBLE_HASH, MANIFEST_HASH, IPFS_CID, universes, 1, parents);

        // maxDepth=1 should only return the immediate parent
        uint256[] memory ancestors = registry.getAncestors(TOKEN_ID, 0, 1);
        assertEq(ancestors.length, 1);
        assertEq(ancestors[0], parentId);
    }

    // =========================================================
    // Multiple generations per token
    // =========================================================

    function testMultipleGenerationsPerToken() public {
        for (uint256 i = 0; i < 3; i++) {
            vm.prank(tokenOwner);
            registry.recordGeneration(TOKEN_ID, WORLD_BIBLE_HASH, MANIFEST_HASH, IPFS_CID, universes, uint8(i % 3), noDerived);
        }

        assertEq(registry.generationCount(TOKEN_ID), 3);
        assertEq(registry.getGeneration(TOKEN_ID, 0).commercialConfidence, 0);
        assertEq(registry.getGeneration(TOKEN_ID, 1).commercialConfidence, 1);
        assertEq(registry.getGeneration(TOKEN_ID, 2).commercialConfidence, 2);
    }
}
