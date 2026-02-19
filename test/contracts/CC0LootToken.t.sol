// SPDX-License-Identifier: CC0-1.0
pragma solidity ^0.8.24;

import "forge-std/Test.sol";
import "../../contracts/CC0LootToken.sol";

// Mock ERC-6551 Registry -- just records calls, returns a deterministic address
contract MockERC6551Registry {
    mapping(uint256 => address) public accounts;

    function createAccount(
        address, // implementation
        bytes32, // salt
        uint256, // chainId
        address, // tokenContract
        uint256 tokenId
    ) external returns (address) {
        // Deterministic mock address: hash of tokenId
        address mockAccount = address(uint160(uint256(keccak256(abi.encode("tba", tokenId)))));
        accounts[tokenId] = mockAccount;
        return mockAccount;
    }

    function account(
        address, // implementation
        bytes32, // salt
        uint256, // chainId
        address, // tokenContract
        uint256 tokenId
    ) external view returns (address) {
        return address(uint160(uint256(keccak256(abi.encode("tba", tokenId)))));
    }
}

// Mock Agent Registry -- owner can register/deregister agents
contract MockAgentRegistry {
    mapping(address => bool) public registered;

    function registerAgent(address agent) external {
        registered[agent] = true;
    }

    function deregisterAgent(address agent) external {
        registered[agent] = false;
    }

    function isRegistered(address agent) external view returns (bool) {
        return registered[agent];
    }
}

contract CC0LootTokenTest is Test {
    CC0LootToken public token;
    MockERC6551Registry public mockRegistry;
    MockAgentRegistry public mockAgentRegistry;

    address public owner = address(0x1);
    address public user1 = address(0x2);
    address public user2 = address(0x3);
    address public agentUser = address(0x4);
    address public tbaImpl = address(0x5);

    function setUp() public {
        mockRegistry = new MockERC6551Registry();
        mockAgentRegistry = new MockAgentRegistry();

        vm.prank(owner);
        token = new CC0LootToken(
            address(mockRegistry),
            tbaImpl,
            address(mockAgentRegistry)
        );
    }

    // =========================================================
    // Phase 1: Free mint
    // =========================================================

    function testMintPhase1() public {
        vm.prank(user1);
        uint256 tokenId = token.mint();

        assertEq(tokenId, 1);
        assertEq(token.ownerOf(1), user1);
        assertEq(token.totalMinted(), 1);
    }

    function testMintPhase1TwoTokens() public {
        vm.prank(user1);
        token.mint();

        vm.prank(user1);
        token.mint();

        assertEq(token.mintedPerWallet(user1), 2);
        assertEq(token.totalMinted(), 2);
    }

    function testWalletCapEnforced() public {
        vm.prank(user1);
        token.mint();
        vm.prank(user1);
        token.mint();

        // Third mint should revert
        vm.prank(user1);
        vm.expectRevert(CC0LootToken.WalletCapExceeded.selector);
        token.mint();
    }

    function testPhase1SupplyExhausted() public {
        // Mint all 1024 Phase 1 tokens across different wallets
        for (uint256 i = 0; i < 512; i++) {
            address walletA = address(uint160(0x1000 + i * 2));
            address walletB = address(uint160(0x1001 + i * 2));
            vm.prank(walletA);
            token.mint();
            vm.prank(walletB);
            token.mint();
        }

        assertEq(token.totalMinted(), 1024);

        // Next mint should revert -- phase supply exhausted
        address overflow = address(0xDEAD);
        vm.prank(overflow);
        vm.expectRevert(CC0LootToken.PhaseSupplyExhausted.selector);
        token.mint();
    }

    // =========================================================
    // TBA deployment
    // =========================================================

    function testTBADeployedOnMint() public {
        vm.prank(user1);
        uint256 tokenId = token.mint();

        address tba = token.tokenBoundAccount(tokenId);
        assertNotEq(tba, address(0));
    }

    function testTBADifferentPerToken() public {
        vm.prank(user1);
        uint256 tokenId1 = token.mint();

        vm.prank(user2);
        uint256 tokenId2 = token.mint();

        address tba1 = token.tokenBoundAccount(tokenId1);
        address tba2 = token.tokenBoundAccount(tokenId2);

        assertNotEq(tba1, tba2);
    }

    // =========================================================
    // Phase 2: Agents-only gate
    // =========================================================

    function testPhase2AgentsOnly_NonAgentReverts() public {
        // Advance to Phase 2
        vm.prank(owner);
        token.advancePhase();

        vm.prank(user1);
        vm.expectRevert(CC0LootToken.AgentsOnly.selector);
        token.mint();
    }

    function testPhase2AgentsOnly_AgentSucceeds() public {
        // Register agentUser in mock registry
        mockAgentRegistry.registerAgent(agentUser);

        // Advance to Phase 2
        vm.prank(owner);
        token.advancePhase();

        vm.prank(agentUser);
        uint256 tokenId = token.mint();

        assertEq(token.ownerOf(tokenId), agentUser);
    }

    function testPhase2AgentDeregisteredReverts() public {
        // Register, advance phase, deregister, try to mint
        mockAgentRegistry.registerAgent(agentUser);

        vm.prank(owner);
        token.advancePhase();

        mockAgentRegistry.deregisterAgent(agentUser);

        vm.prank(agentUser);
        vm.expectRevert(CC0LootToken.AgentsOnly.selector);
        token.mint();
    }

    // =========================================================
    // Phase 3: Public mint
    // =========================================================

    function testPhase3AnyoneCanMint() public {
        vm.prank(owner);
        token.advancePhase(); // -> Phase 2
        vm.prank(owner);
        token.advancePhase(); // -> Phase 3

        vm.prank(user1);
        uint256 tokenId = token.mint();
        assertEq(token.ownerOf(tokenId), user1);
    }

    // =========================================================
    // Phase management
    // =========================================================

    function testAdvancePhaseOnlyOwner() public {
        vm.prank(user1);
        vm.expectRevert();
        token.advancePhase();
    }

    function testCannotAdvancePastPhase3() public {
        vm.startPrank(owner);
        token.advancePhase(); // -> 2
        token.advancePhase(); // -> 3
        vm.expectRevert(CC0LootToken.InvalidPhase.selector);
        token.advancePhase(); // -> should fail
        vm.stopPrank();
    }

    // =========================================================
    // Transfer lockup
    // =========================================================

    function testTransferLockedWithin7Days() public {
        vm.prank(user1);
        uint256 tokenId = token.mint();

        // Attempt transfer immediately -- should revert
        vm.prank(user1);
        vm.expectRevert(CC0LootToken.TransferLocked.selector);
        token.transferFrom(user1, user2, tokenId);
    }

    function testTransferLockedAt6Days() public {
        vm.prank(user1);
        uint256 tokenId = token.mint();

        // Warp to 6 days 23 hours
        vm.warp(block.timestamp + 6 days + 23 hours);

        vm.prank(user1);
        vm.expectRevert(CC0LootToken.TransferLocked.selector);
        token.transferFrom(user1, user2, tokenId);
    }

    function testTransferSucceedsAfter7Days() public {
        vm.prank(user1);
        uint256 tokenId = token.mint();

        // Warp past lockup
        vm.warp(block.timestamp + 7 days + 1);

        vm.prank(user1);
        token.transferFrom(user1, user2, tokenId);

        assertEq(token.ownerOf(tokenId), user2);
    }

    function testMintIsNotATransfer() public {
        // Minting (from = address(0)) should never trigger the lockup check
        vm.prank(user1);
        uint256 tokenId = token.mint();
        assertEq(token.ownerOf(tokenId), user1);
    }

    // =========================================================
    // Token URI
    // =========================================================

    function testTokenURIExistsAfterMint() public {
        vm.prank(user1);
        uint256 tokenId = token.mint();

        string memory uri = token.tokenURI(tokenId);
        // Should be a non-empty data URI
        assertTrue(bytes(uri).length > 0);
    }

    function testTokenURIStartsWithDataURI() public {
        vm.prank(user1);
        uint256 tokenId = token.mint();

        string memory uri = token.tokenURI(tokenId);
        // Check prefix "data:application/json;base64,"
        bytes memory uriBytes = bytes(uri);
        bytes memory prefix = bytes("data:application/json;base64,");
        assertTrue(uriBytes.length >= prefix.length);
        for (uint256 i = 0; i < prefix.length; i++) {
            assertEq(uriBytes[i], prefix[i]);
        }
    }

    function testTokenURIRevertsForNonexistent() public {
        vm.expectRevert();
        token.tokenURI(999);
    }

    // =========================================================
    // Custom metadata
    // =========================================================

    function testSetTokenMetadataOnlyOwner() public {
        vm.prank(user1);
        uint256 tokenId = token.mint();

        vm.prank(user1);
        vm.expectRevert();
        token.setTokenMetadata(tokenId, bytes('{"@type":"FictionalCharacter"}'));
    }

    function testSetTokenMetadataOwnerSucceeds() public {
        vm.prank(user1);
        uint256 tokenId = token.mint();

        bytes memory customMeta = bytes('{"@context":"https://schema.org","@type":"FictionalCharacter","name":"Custom"}');

        vm.prank(owner);
        token.setTokenMetadata(tokenId, customMeta);

        // URI should now reflect the custom metadata
        string memory uri = token.tokenURI(tokenId);
        assertTrue(bytes(uri).length > 0);
    }

    // =========================================================
    // Reserve mint
    // =========================================================

    function testReserveMintOnlyOwner() public {
        vm.prank(user1);
        vm.expectRevert();
        token.reserveMint(user1, 1);
    }

    function testReserveMintOwnerSucceeds() public {
        vm.prank(owner);
        token.reserveMint(user2, 3);

        assertEq(token.totalMinted(), 3);
        assertEq(token.ownerOf(1), user2);
        assertEq(token.ownerOf(2), user2);
        assertEq(token.ownerOf(3), user2);
    }

    function testReserveMintExceedsLimit() public {
        vm.prank(owner);
        vm.expectRevert("exceeds supply");
        token.reserveMint(user2, 4097); // More than MAX_SUPPLY
    }

    // =========================================================
    // Max supply
    // =========================================================

    function testMaxSupplyEnforced() public {
        // Use reserveMint to fill supply efficiently (avoids per-wallet cap and gas explosion)
        vm.startPrank(owner);

        // Fill all 4096 tokens via reserve mint
        token.reserveMint(owner, 4096);

        assertEq(token.totalMinted(), 4096);

        vm.stopPrank();

        // Any further mint should revert -- phase doesn't matter once MAX_SUPPLY is hit
        address overflow = address(0xBEEF);
        vm.prank(overflow);
        vm.expectRevert(CC0LootToken.MaxSupplyReached.selector);
        token.mint();
    }

    // =========================================================
    // Supports interface
    // =========================================================

    function testSupportsERC721Interface() public view {
        // ERC-721 interfaceId = 0x80ac58cd
        assertTrue(token.supportsInterface(0x80ac58cd));
    }

    function testSupportsERC165Interface() public view {
        // ERC-165 interfaceId = 0x01ffc9a7
        assertTrue(token.supportsInterface(0x01ffc9a7));
    }
}
