// SPDX-License-Identifier: CC0-1.0
pragma solidity ^0.8.24;

import "forge-std/Test.sol";
import "../../contracts/TrustedForwarder.sol";

// Minimal mock WorldRegistry that records calls and enforces forwarder auth
contract MockWorldRegistry {
    // Track calls for assertion
    uint256 public recordCallCount;
    uint256 public lastTokenId;
    bytes32 public lastWorldBibleHash;
    bytes32 public lastManifestHash;

    // Optionally revert on next call (to test ForwardFailed path)
    bool public shouldRevert;

    function setRevert(bool _revert) external {
        shouldRevert = _revert;
    }

    function recordGeneration(
        uint256 tokenId,
        bytes32 worldBibleHash,
        bytes32 manifestHash,
        string calldata, // ipfsCid
        string[] calldata, // universesUsed
        uint8, // commercialConfidence
        uint256[] calldata // derivedFromTokenIds
    ) external {
        if (shouldRevert) revert("registry revert");
        recordCallCount++;
        lastTokenId = tokenId;
        lastWorldBibleHash = worldBibleHash;
        lastManifestHash = manifestHash;
    }
}

contract TrustedForwarderTest is Test {
    TrustedForwarder public forwarder;
    MockWorldRegistry public mockRegistry;

    address public contractOwner = address(0x1);
    address public stranger = address(0x2);

    // Known private key for EIP-712 signing in tests
    uint256 public signerPrivateKey = 0xA11CE;
    address public signerAddress;

    // Test data constants
    uint256 public constant TOKEN_ID = 42;
    bytes32 public constant WORLD_BIBLE_HASH = keccak256("world bible content");
    bytes32 public constant MANIFEST_HASH = keccak256("manifest content");
    string public constant IPFS_CID = "bafybeigdyrzt5sfp7udm7hu76uh7y26nf3efuylqabf3oclgtqy55fbzdi";
    uint8 public constant CONFIDENCE = 1;

    string[] universes;
    uint256[] noDerived;

    function setUp() public {
        signerAddress = vm.addr(signerPrivateKey);

        mockRegistry = new MockWorldRegistry();

        vm.prank(contractOwner);
        forwarder = new TrustedForwarder(address(mockRegistry));

        // Authorize the test signer by default
        vm.prank(contractOwner);
        forwarder.addSigner(signerAddress);

        universes.push("univ:nouns");
        // noDerived stays empty
    }

    // =========================================================
    // Helper: build valid EIP-712 digest and sign it
    // =========================================================

    function _buildDigest(TrustedForwarder.ForwardRequest memory req) internal view returns (bytes32) {
        bytes32 structHash = keccak256(abi.encode(
            forwarder.FORWARD_REQUEST_TYPEHASH(),
            req.from,
            req.to,
            req.nonce,
            req.tokenId,
            req.worldBibleHash,
            req.manifestHash,
            keccak256(bytes(req.ipfsCid)),
            req.commercialConfidence
        ));
        return keccak256(abi.encodePacked("\x19\x01", forwarder.DOMAIN_SEPARATOR(), structHash));
    }

    function _sign(TrustedForwarder.ForwardRequest memory req) internal view returns (bytes memory) {
        bytes32 digest = _buildDigest(req);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(signerPrivateKey, digest);
        return abi.encodePacked(r, s, v);
    }

    function _makeRequest() internal view returns (TrustedForwarder.ForwardRequest memory req) {
        req = TrustedForwarder.ForwardRequest({
            from: signerAddress,
            to: address(mockRegistry),
            nonce: forwarder.nonces(signerAddress),
            tokenId: TOKEN_ID,
            worldBibleHash: WORLD_BIBLE_HASH,
            manifestHash: MANIFEST_HASH,
            ipfsCid: IPFS_CID,
            universesUsed: universes,
            commercialConfidence: CONFIDENCE,
            derivedFromTokenIds: noDerived
        });
    }

    // =========================================================
    // Signer Management
    // =========================================================

    function testAddSigner_OnlyOwner() public {
        vm.prank(stranger);
        vm.expectRevert();
        forwarder.addSigner(stranger);
    }

    function testAddSigner_OwnerSucceeds() public {
        address newSigner = address(0xBEEF);
        vm.prank(contractOwner);
        forwarder.addSigner(newSigner);
        assertTrue(forwarder.authorizedSigners(newSigner));
    }

    function testAddSigner_EmitsEvent() public {
        address newSigner = address(0xBEEF);
        vm.expectEmit(true, false, false, false);
        emit TrustedForwarder.SignerAdded(newSigner);
        vm.prank(contractOwner);
        forwarder.addSigner(newSigner);
    }

    function testRemoveSigner_OnlyOwner() public {
        vm.prank(stranger);
        vm.expectRevert();
        forwarder.removeSigner(signerAddress);
    }

    function testRemoveSigner_OwnerSucceeds() public {
        vm.prank(contractOwner);
        forwarder.removeSigner(signerAddress);
        assertFalse(forwarder.authorizedSigners(signerAddress));
    }

    function testRemoveSigner_EmitsEvent() public {
        vm.expectEmit(true, false, false, false);
        emit TrustedForwarder.SignerRemoved(signerAddress);
        vm.prank(contractOwner);
        forwarder.removeSigner(signerAddress);
    }

    // =========================================================
    // forward() -- authorization checks
    // =========================================================

    function testForward_UnauthorizedSignerReverts() public {
        uint256 unknownKey = 0xDEAD;
        address unknownSigner = vm.addr(unknownKey);

        TrustedForwarder.ForwardRequest memory req = TrustedForwarder.ForwardRequest({
            from: unknownSigner,
            to: address(mockRegistry),
            nonce: 0,
            tokenId: TOKEN_ID,
            worldBibleHash: WORLD_BIBLE_HASH,
            manifestHash: MANIFEST_HASH,
            ipfsCid: IPFS_CID,
            universesUsed: universes,
            commercialConfidence: CONFIDENCE,
            derivedFromTokenIds: noDerived
        });

        bytes32 digest = _buildDigest(req);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(unknownKey, digest);
        bytes memory sig = abi.encodePacked(r, s, v);

        vm.expectRevert(TrustedForwarder.UnauthorizedSigner.selector);
        forwarder.forward(req, sig);
    }

    function testForward_WrongNonceReverts() public {
        TrustedForwarder.ForwardRequest memory req = _makeRequest();
        req.nonce = 999; // wrong nonce

        bytes32 digest = _buildDigest(req);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(signerPrivateKey, digest);
        bytes memory sig = abi.encodePacked(r, s, v);

        vm.expectRevert(TrustedForwarder.InvalidNonce.selector);
        forwarder.forward(req, sig);
    }

    function testForward_InvalidSignatureReverts() public {
        TrustedForwarder.ForwardRequest memory req = _makeRequest();

        // Sign with a different private key (wrong signer)
        uint256 wrongKey = 0xB0B;
        bytes32 digest = _buildDigest(req);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(wrongKey, digest);
        bytes memory sig = abi.encodePacked(r, s, v);

        vm.expectRevert(TrustedForwarder.InvalidSignature.selector);
        forwarder.forward(req, sig);
    }

    function testForward_ValidCallSucceeds() public {
        TrustedForwarder.ForwardRequest memory req = _makeRequest();
        bytes memory sig = _sign(req);

        forwarder.forward(req, sig);

        assertEq(mockRegistry.recordCallCount(), 1);
        assertEq(mockRegistry.lastTokenId(), TOKEN_ID);
    }

    function testForward_NonceIncrements() public {
        assertEq(forwarder.getNonce(signerAddress), 0);

        TrustedForwarder.ForwardRequest memory req = _makeRequest();
        bytes memory sig = _sign(req);
        forwarder.forward(req, sig);

        assertEq(forwarder.getNonce(signerAddress), 1);
    }

    function testForward_EmitsEvent() public {
        TrustedForwarder.ForwardRequest memory req = _makeRequest();
        bytes memory sig = _sign(req);

        vm.expectEmit(true, true, false, true);
        emit TrustedForwarder.GenerationForwarded(TOKEN_ID, signerAddress, 0);
        forwarder.forward(req, sig);
    }

    function testForward_ReplayReverts() public {
        TrustedForwarder.ForwardRequest memory req = _makeRequest();
        bytes memory sig = _sign(req);

        // First call succeeds
        forwarder.forward(req, sig);

        // Replay with same nonce/signature should revert
        vm.expectRevert(TrustedForwarder.InvalidNonce.selector);
        forwarder.forward(req, sig);
    }

    function testForward_SequentialNonces() public {
        // First request: nonce 0
        TrustedForwarder.ForwardRequest memory req1 = _makeRequest();
        bytes memory sig1 = _sign(req1);
        forwarder.forward(req1, sig1);

        // Second request: nonce 1
        TrustedForwarder.ForwardRequest memory req2 = _makeRequest(); // nonces() now returns 1
        bytes memory sig2 = _sign(req2);
        forwarder.forward(req2, sig2);

        assertEq(mockRegistry.recordCallCount(), 2);
        assertEq(forwarder.getNonce(signerAddress), 2);
    }

    function testForward_RemovedSignerReverts() public {
        // Remove signer first
        vm.prank(contractOwner);
        forwarder.removeSigner(signerAddress);

        TrustedForwarder.ForwardRequest memory req = _makeRequest();
        bytes memory sig = _sign(req);

        vm.expectRevert(TrustedForwarder.UnauthorizedSigner.selector);
        forwarder.forward(req, sig);
    }

    // =========================================================
    // batchForward()
    // =========================================================

    function testBatchForward_LengthMismatchReverts() public {
        TrustedForwarder.ForwardRequest[] memory reqs = new TrustedForwarder.ForwardRequest[](2);
        bytes[] memory sigs = new bytes[](1); // mismatch

        reqs[0] = _makeRequest();
        reqs[1] = _makeRequest();
        sigs[0] = _sign(reqs[0]);

        vm.expectRevert("length mismatch");
        forwarder.batchForward(reqs, sigs);
    }

    function testBatchForward_SingleItem() public {
        TrustedForwarder.ForwardRequest[] memory reqs = new TrustedForwarder.ForwardRequest[](1);
        bytes[] memory sigs = new bytes[](1);

        reqs[0] = _makeRequest();
        sigs[0] = _sign(reqs[0]);

        forwarder.batchForward(reqs, sigs);

        assertEq(mockRegistry.recordCallCount(), 1);
        assertEq(forwarder.getNonce(signerAddress), 1);
    }

    function testBatchForward_MultipleItems() public {
        TrustedForwarder.ForwardRequest[] memory reqs = new TrustedForwarder.ForwardRequest[](3);
        bytes[] memory sigs = new bytes[](3);

        for (uint256 i = 0; i < 3; i++) {
            reqs[i] = _makeRequest(); // each call to _makeRequest reads current nonce
            sigs[i] = _sign(reqs[i]);
            // Manually increment the nonce expectation for subsequent requests
            // _makeRequest() calls forwarder.nonces() which is on-chain -- but since we haven't
            // forwarded yet, all three will have nonce=0. We need to build them manually.
        }
        // The above won't work for batch because nonce is read before any forward happens.
        // Rebuild manually with correct sequential nonces.
        reqs[0] = TrustedForwarder.ForwardRequest({
            from: signerAddress, to: address(mockRegistry), nonce: 0,
            tokenId: 1, worldBibleHash: WORLD_BIBLE_HASH, manifestHash: MANIFEST_HASH,
            ipfsCid: IPFS_CID, universesUsed: universes, commercialConfidence: 0,
            derivedFromTokenIds: noDerived
        });
        reqs[1] = TrustedForwarder.ForwardRequest({
            from: signerAddress, to: address(mockRegistry), nonce: 1,
            tokenId: 2, worldBibleHash: WORLD_BIBLE_HASH, manifestHash: MANIFEST_HASH,
            ipfsCid: IPFS_CID, universesUsed: universes, commercialConfidence: 1,
            derivedFromTokenIds: noDerived
        });
        reqs[2] = TrustedForwarder.ForwardRequest({
            from: signerAddress, to: address(mockRegistry), nonce: 2,
            tokenId: 3, worldBibleHash: WORLD_BIBLE_HASH, manifestHash: MANIFEST_HASH,
            ipfsCid: IPFS_CID, universesUsed: universes, commercialConfidence: 2,
            derivedFromTokenIds: noDerived
        });

        sigs[0] = _sign(reqs[0]);
        sigs[1] = _sign(reqs[1]);
        sigs[2] = _sign(reqs[2]);

        forwarder.batchForward(reqs, sigs);

        assertEq(mockRegistry.recordCallCount(), 3);
        assertEq(forwarder.getNonce(signerAddress), 3);
    }

    function testBatchForward_UnauthorizedSignerReverts() public {
        uint256 unknownKey = 0xDEAD;
        address unknownSigner = vm.addr(unknownKey);

        TrustedForwarder.ForwardRequest[] memory reqs = new TrustedForwarder.ForwardRequest[](1);
        bytes[] memory sigs = new bytes[](1);

        reqs[0] = TrustedForwarder.ForwardRequest({
            from: unknownSigner, to: address(mockRegistry), nonce: 0,
            tokenId: TOKEN_ID, worldBibleHash: WORLD_BIBLE_HASH, manifestHash: MANIFEST_HASH,
            ipfsCid: IPFS_CID, universesUsed: universes, commercialConfidence: CONFIDENCE,
            derivedFromTokenIds: noDerived
        });

        bytes32 digest = _buildDigest(reqs[0]);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(unknownKey, digest);
        sigs[0] = abi.encodePacked(r, s, v);

        vm.expectRevert(TrustedForwarder.UnauthorizedSigner.selector);
        forwarder.batchForward(reqs, sigs);
    }

    // =========================================================
    // Views
    // =========================================================

    function testGetNonce_StartsAtZero() public view {
        assertEq(forwarder.getNonce(signerAddress), 0);
    }

    function testGetNonce_UnknownAddress() public view {
        assertEq(forwarder.getNonce(address(0xDEAD)), 0);
    }

    function testDomainSeparator_NotZero() public view {
        assertNotEq(forwarder.domainSeparator(), bytes32(0));
    }

    // =========================================================
    // Admin
    // =========================================================

    function testSetWorldRegistry_OnlyOwner() public {
        vm.prank(stranger);
        vm.expectRevert();
        forwarder.setWorldRegistry(address(0xBEEF));
    }

    function testSetWorldRegistry_OwnerSucceeds() public {
        address newRegistry = address(0xBEEF);
        vm.prank(contractOwner);
        forwarder.setWorldRegistry(newRegistry);
        assertEq(address(forwarder.worldRegistry()), newRegistry);
    }
}
