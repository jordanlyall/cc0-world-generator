// SPDX-License-Identifier: CC0-1.0
pragma solidity ^0.8.24;

import "@openzeppelin/contracts/access/Ownable.sol";

/// @notice Minimal interface to WorldRegistry for forwarded calls
interface IWorldRegistry {
    function recordGeneration(
        uint256 tokenId,
        bytes32 worldBibleHash,
        bytes32 manifestHash,
        string calldata ipfsCid,
        string[] calldata universesUsed,
        uint8 commercialConfidence,
        uint256[] calldata derivedFromTokenIds
    ) external;
}

/// @title TrustedForwarder
/// @notice EIP-2771 meta-transaction forwarder. worldkit.ai backend is the
///         authorized submitter after a one-time token holder approval in
///         WorldRegistry. Removes per-generation gas friction that would
///         suppress provenance accumulation.
///
///         Flow:
///         1. Token holder calls WorldRegistry.authorizeForwarder(tokenId, address(this))
///         2. worldkit.ai backend signs and submits generation records via this contract
///         3. WorldRegistry verifies msg.sender == authorizedForwarder[tokenId]
///         4. Record is written -- no gas cost to the token holder
///
/// @dev This is the standard OpenZeppelin/Biconomy EIP-2771 pattern.
///      The signer is the worldkit.ai backend EOA; the relayer pays gas.
///      For production: integrate with Biconomy Paymaster or Gelato for
///      gas abstraction so worldkit.ai covers all submission costs.
contract TrustedForwarder is Ownable {

    // =========================================================================
    // Structs
    // =========================================================================

    struct ForwardRequest {
        address from;           // worldkit.ai backend EOA (signer)
        address to;             // WorldRegistry contract address
        uint256 nonce;          // per-signer nonce (replay protection)
        uint256 tokenId;        // Manifest token ID being recorded
        bytes32 worldBibleHash;
        bytes32 manifestHash;
        string ipfsCid;
        string[] universesUsed;
        uint8 commercialConfidence;
        uint256[] derivedFromTokenIds;
    }

    // =========================================================================
    // State
    // =========================================================================

    IWorldRegistry public worldRegistry;

    /// @notice Authorized backend signers (worldkit.ai EOAs)
    mapping(address => bool) public authorizedSigners;

    /// @notice Per-signer nonce for replay protection
    mapping(address => uint256) public nonces;

    /// @notice EIP-712 domain separator
    bytes32 public immutable DOMAIN_SEPARATOR;

    bytes32 public constant FORWARD_REQUEST_TYPEHASH = keccak256(
        "ForwardRequest(address from,address to,uint256 nonce,uint256 tokenId,"
        "bytes32 worldBibleHash,bytes32 manifestHash,string ipfsCid,"
        "uint8 commercialConfidence)"
        // Note: arrays excluded from typehash for simplicity; include in production
    );

    // =========================================================================
    // Events
    // =========================================================================

    event GenerationForwarded(uint256 indexed tokenId, address indexed signer, uint256 nonce);
    event SignerAdded(address indexed signer);
    event SignerRemoved(address indexed signer);

    // =========================================================================
    // Errors
    // =========================================================================

    error UnauthorizedSigner();
    error InvalidSignature();
    error InvalidNonce();
    error ForwardFailed();

    // =========================================================================
    // Constructor
    // =========================================================================

    constructor(address _worldRegistry) Ownable(msg.sender) {
        worldRegistry = IWorldRegistry(_worldRegistry);

        DOMAIN_SEPARATOR = keccak256(abi.encode(
            keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"),
            keccak256("Manifest TrustedForwarder"),
            keccak256("1"),
            block.chainid,
            address(this)
        ));
    }

    // =========================================================================
    // Signer Management
    // =========================================================================

    function addSigner(address signer) external onlyOwner {
        authorizedSigners[signer] = true;
        emit SignerAdded(signer);
    }

    function removeSigner(address signer) external onlyOwner {
        authorizedSigners[signer] = false;
        emit SignerRemoved(signer);
    }

    // =========================================================================
    // Forward: Gasless Generation Record Submission
    // =========================================================================

    /// @notice Submit a generation record on behalf of a token holder.
    ///         Called by worldkit.ai relayer after generating a world.
    ///         The token holder must have previously called
    ///         WorldRegistry.authorizeForwarder(tokenId, address(this)).
    /// @param req The forward request struct
    /// @param signature EIP-712 signature from the authorized backend signer
    function forward(
        ForwardRequest calldata req,
        bytes calldata signature
    ) external {
        if (!authorizedSigners[req.from]) revert UnauthorizedSigner();
        if (req.nonce != nonces[req.from]) revert InvalidNonce();
        if (!_verify(req, signature)) revert InvalidSignature();

        nonces[req.from]++;

        worldRegistry.recordGeneration(
            req.tokenId,
            req.worldBibleHash,
            req.manifestHash,
            req.ipfsCid,
            req.universesUsed,
            req.commercialConfidence,
            req.derivedFromTokenIds
        );

        emit GenerationForwarded(req.tokenId, req.from, req.nonce);
    }

    /// @notice Batch forward multiple generation records in one transaction
    ///         Gas optimization for high-volume worldkit.ai backends
    function batchForward(
        ForwardRequest[] calldata requests,
        bytes[] calldata signatures
    ) external {
        require(requests.length == signatures.length, "length mismatch");
        for (uint256 i = 0; i < requests.length; i++) {
            ForwardRequest calldata req = requests[i];
            if (!authorizedSigners[req.from]) revert UnauthorizedSigner();
            if (req.nonce != nonces[req.from]) revert InvalidNonce();
            if (!_verify(req, signatures[i])) revert InvalidSignature();

            nonces[req.from]++;

            worldRegistry.recordGeneration(
                req.tokenId,
                req.worldBibleHash,
                req.manifestHash,
                req.ipfsCid,
                req.universesUsed,
                req.commercialConfidence,
                req.derivedFromTokenIds
            );

            emit GenerationForwarded(req.tokenId, req.from, req.nonce);
        }
    }

    // =========================================================================
    // EIP-712 Signature Verification
    // =========================================================================

    function _verify(ForwardRequest calldata req, bytes calldata signature)
        internal
        view
        returns (bool)
    {
        bytes32 digest = keccak256(abi.encodePacked(
            "\x19\x01",
            DOMAIN_SEPARATOR,
            keccak256(abi.encode(
                FORWARD_REQUEST_TYPEHASH,
                req.from,
                req.to,
                req.nonce,
                req.tokenId,
                req.worldBibleHash,
                req.manifestHash,
                keccak256(bytes(req.ipfsCid)),
                req.commercialConfidence
            ))
        ));

        address recovered = _recoverSigner(digest, signature);
        return recovered == req.from;
    }

    function _recoverSigner(bytes32 digest, bytes calldata signature)
        internal
        pure
        returns (address)
    {
        require(signature.length == 65, "invalid signature length");

        bytes32 r;
        bytes32 s;
        uint8 v;

        assembly {
            r := calldataload(signature.offset)
            s := calldataload(add(signature.offset, 32))
            v := byte(0, calldataload(add(signature.offset, 64)))
        }

        if (v < 27) v += 27;
        require(v == 27 || v == 28, "invalid v value");

        return ecrecover(digest, v, r, s);
    }

    // =========================================================================
    // Views
    // =========================================================================

    function getNonce(address signer) external view returns (uint256) {
        return nonces[signer];
    }

    function domainSeparator() external view returns (bytes32) {
        return DOMAIN_SEPARATOR;
    }

    // =========================================================================
    // Admin
    // =========================================================================

    function setWorldRegistry(address _worldRegistry) external onlyOwner {
        worldRegistry = IWorldRegistry(_worldRegistry);
    }
}
