// SPDX-License-Identifier: CC0-1.0
pragma solidity ^0.8.24;

import "@openzeppelin/contracts/access/Ownable.sol";

/// @notice Interface to verify token ownership for access control
interface ICC0LootToken {
    function tokenBoundAccount(uint256 tokenId) external view returns (address);
    function ownerOf(uint256 tokenId) external view returns (address);
}

/// @title WorldRegistry
/// @notice On-chain provenance ledger for CC0 world generation events.
///         Each generation record anchors a world bible + compliance manifest
///         to a specific Manifest token. Hashes are the integrity guarantee;
///         full content lives on IPFS. Records are permanent and traversable.
/// @dev Callable by the token's TBA directly, or by an authorized TrustedForwarder
///      after one-time approval (EIP-2771 gasless pattern).
contract WorldRegistry is Ownable {

    // =========================================================================
    // Structs
    // =========================================================================

    struct Generation {
        uint256 tokenId;
        address generatorAddress;   // TBA address of the minting token
        bytes32 worldBibleHash;     // keccak256 of full world bible markdown
        bytes32 manifestHash;       // keccak256 of full compliance manifest JSON
        string ipfsCid;             // IPFS CID for off-chain content (world bible + manifest)
        string[] universesUsed;     // e.g. ["univ:nouns", "univ:cryptoadz"]
        uint8 commercialConfidence; // 0=low, 1=medium, 2=high (mirrors CC0 World Generator enum)
        uint256[] derivedFromTokenIds; // composition graph: parent token IDs
        uint256 blockHeight;
        uint256 timestamp;
    }

    // =========================================================================
    // State
    // =========================================================================

    ICC0LootToken public immutable lootToken;

    /// @notice Authorized forwarder per token (one-time approval, then worldkit.ai auto-submits)
    mapping(uint256 => address) public authorizedForwarder;

    /// @notice Full generation history per token
    mapping(uint256 => Generation[]) private _generationHistory;

    /// @notice Global index of all generation records (for discovery)
    uint256 public totalGenerations;
    mapping(uint256 => uint256) public generationIndexToTokenId; // global index => tokenId

    // =========================================================================
    // Events
    // =========================================================================

    event ForwarderAuthorized(uint256 indexed tokenId, address forwarder);
    event ForwarderRevoked(uint256 indexed tokenId);
    event GenerationRecorded(
        uint256 indexed tokenId,
        address indexed generatorAddress,
        bytes32 worldBibleHash,
        bytes32 manifestHash,
        string ipfsCid,
        uint8 commercialConfidence,
        uint256 globalIndex
    );

    // =========================================================================
    // Errors
    // =========================================================================

    error NotAuthorized();
    error InvalidToken();
    error InvalidUniverses();
    error InvalidHashes();

    // =========================================================================
    // Constructor
    // =========================================================================

    constructor(address _lootToken) Ownable(msg.sender) {
        lootToken = ICC0LootToken(_lootToken);
    }

    // =========================================================================
    // Forwarder Authorization (EIP-2771 gasless pattern)
    // =========================================================================

    /// @notice One-time approval: worldkit.ai backend can submit generation records
    ///         on behalf of this token after this call. Removes per-generation gas friction.
    /// @param tokenId The Manifest token ID
    /// @param forwarder The TrustedForwarder contract address (worldkit.ai backend)
    function authorizeForwarder(uint256 tokenId, address forwarder) external {
        _requireTokenOwnerOrTBA(tokenId);
        authorizedForwarder[tokenId] = forwarder;
        emit ForwarderAuthorized(tokenId, forwarder);
    }

    /// @notice Revoke forwarder authorization
    function revokeForwarder(uint256 tokenId) external {
        _requireTokenOwnerOrTBA(tokenId);
        delete authorizedForwarder[tokenId];
        emit ForwarderRevoked(tokenId);
    }

    // =========================================================================
    // Core: Record Generation
    // =========================================================================

    /// @notice Record a world generation event on-chain.
    ///         Callable by: (1) the token's TBA directly, (2) authorized forwarder,
    ///         (3) the token holder (owner of record).
    /// @param tokenId Manifest token ID whose TBA is the generator
    /// @param worldBibleHash keccak256 of the full world bible markdown string
    /// @param manifestHash keccak256 of the full compliance manifest JSON string
    /// @param ipfsCid IPFS CID where full content is stored
    /// @param universesUsed Array of universe IDs used (e.g. "univ:nouns")
    /// @param commercialConfidence 0=low, 1=medium, 2=high
    /// @param derivedFromTokenIds Token IDs this world derives from (empty = original)
    function recordGeneration(
        uint256 tokenId,
        bytes32 worldBibleHash,
        bytes32 manifestHash,
        string calldata ipfsCid,
        string[] calldata universesUsed,
        uint8 commercialConfidence,
        uint256[] calldata derivedFromTokenIds
    ) external {
        _requireRecordingAuthorized(tokenId);

        if (worldBibleHash == bytes32(0) || manifestHash == bytes32(0)) revert InvalidHashes();
        if (universesUsed.length == 0 || universesUsed.length > 5) revert InvalidUniverses();
        if (commercialConfidence > 2) revert InvalidUniverses(); // reusing error; confidence must be 0/1/2

        address tba = lootToken.tokenBoundAccount(tokenId);

        Generation memory gen = Generation({
            tokenId: tokenId,
            generatorAddress: tba,
            worldBibleHash: worldBibleHash,
            manifestHash: manifestHash,
            ipfsCid: ipfsCid,
            universesUsed: universesUsed,
            commercialConfidence: commercialConfidence,
            derivedFromTokenIds: derivedFromTokenIds,
            blockHeight: block.number,
            timestamp: block.timestamp
        });

        _generationHistory[tokenId].push(gen);

        uint256 globalIndex = totalGenerations;
        generationIndexToTokenId[globalIndex] = tokenId;
        totalGenerations++;

        emit GenerationRecorded(
            tokenId,
            tba,
            worldBibleHash,
            manifestHash,
            ipfsCid,
            commercialConfidence,
            globalIndex
        );
    }

    // =========================================================================
    // Views
    // =========================================================================

    /// @notice Full provenance trail for a token
    function generationHistory(uint256 tokenId) external view returns (Generation[] memory) {
        return _generationHistory[tokenId];
    }

    /// @notice Count of generation events for a token
    function generationCount(uint256 tokenId) external view returns (uint256) {
        return _generationHistory[tokenId].length;
    }

    /// @notice Single generation record by token + index
    function getGeneration(uint256 tokenId, uint256 index) external view returns (Generation memory) {
        return _generationHistory[tokenId][index];
    }

    /// @notice Most recent generation for a token (convenience)
    function latestGeneration(uint256 tokenId) external view returns (Generation memory) {
        uint256 len = _generationHistory[tokenId].length;
        require(len > 0, "no generations");
        return _generationHistory[tokenId][len - 1];
    }

    /// @notice Traverse the composition graph: returns all ancestor token IDs
    ///         for a given generation (depth-limited to prevent gas exhaustion)
    function getAncestors(uint256 tokenId, uint256 generationIndex, uint256 maxDepth)
        external
        view
        returns (uint256[] memory ancestors)
    {
        // Simple BFS -- in production would use off-chain indexing for deep traversal
        uint256[] memory queue = new uint256[](maxDepth * 5);
        uint256 head = 0;
        uint256 tail = 0;
        uint256 count = 0;

        Generation memory gen = _generationHistory[tokenId][generationIndex];
        for (uint256 i = 0; i < gen.derivedFromTokenIds.length; i++) {
            queue[tail++] = gen.derivedFromTokenIds[i];
        }

        uint256[] memory result = new uint256[](maxDepth * 5);

        while (head < tail && count < maxDepth) {
            uint256 current = queue[head++];
            result[count++] = current;

            uint256 parentLen = _generationHistory[current].length;
            if (parentLen > 0) {
                Generation memory parentGen = _generationHistory[current][parentLen - 1];
                for (uint256 i = 0; i < parentGen.derivedFromTokenIds.length && tail < queue.length; i++) {
                    queue[tail++] = parentGen.derivedFromTokenIds[i];
                }
            }
        }

        ancestors = new uint256[](count);
        for (uint256 i = 0; i < count; i++) {
            ancestors[i] = result[i];
        }
    }

    // =========================================================================
    // Internal Authorization
    // =========================================================================

    function _requireTokenOwnerOrTBA(uint256 tokenId) internal view {
        address tba = lootToken.tokenBoundAccount(tokenId);
        address owner = lootToken.ownerOf(tokenId);
        if (msg.sender != tba && msg.sender != owner) revert NotAuthorized();
    }

    function _requireRecordingAuthorized(uint256 tokenId) internal view {
        address tba = lootToken.tokenBoundAccount(tokenId);
        address owner = lootToken.ownerOf(tokenId);
        address forwarder = authorizedForwarder[tokenId];

        if (
            msg.sender != tba &&
            msg.sender != owner &&
            msg.sender != forwarder
        ) revert NotAuthorized();
    }
}
