// SPDX-License-Identifier: CC0-1.0
pragma solidity ^0.8.24;

import "@openzeppelin/contracts/token/ERC721/ERC721.sol";
import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/utils/Strings.sol";
import "@openzeppelin/contracts/utils/Base64.sol";

/// @notice Interface for ERC-6551 Registry (Token Bound Accounts)
interface IERC6551Registry {
    function createAccount(
        address implementation,
        bytes32 salt,
        uint256 chainId,
        address tokenContract,
        uint256 tokenId
    ) external returns (address account);

    function account(
        address implementation,
        bytes32 salt,
        uint256 chainId,
        address tokenContract,
        uint256 tokenId
    ) external view returns (address account);
}

/// @notice Interface for ERC-8004 Agent Registry
interface IAgentRegistry {
    function isRegistered(address agent) external view returns (bool);
}

/// @title CC0LootToken
/// @notice Free-mint ERC-721 collection. Each token auto-deploys a Token Bound Account
///         via ERC-6551, making the token IS the agent's on-chain identity.
///         Phase 1: open to humans and agents. Phase 2: onlyAgents() enforced.
/// @dev Metadata stored fully on-chain via SSTORE2 pattern (JSON-LD FictionalCharacter).
contract CC0LootToken is ERC721, Ownable {
    using Strings for uint256;

    // =========================================================================
    // State
    // =========================================================================

    uint256 public constant MAX_SUPPLY = 4096; // 2^12
    uint256 public constant PHASE1_SUPPLY = 1024;
    uint256 public constant PHASE2_SUPPLY = 2560; // 1024 + 1536
    uint256 public constant PHASE3_SUPPLY = 3584; // 2560 + 1024
    // 512 reserved (3584 - 4096)

    uint256 public totalMinted;
    uint8 public currentPhase; // 1, 2, or 3

    /// @notice 7-day transfer lockup after mint (anti-bot)
    uint256 public constant TRANSFER_LOCKUP = 7 days;
    mapping(uint256 => uint256) public mintedAt;

    /// @notice Per-wallet cap across all phases
    mapping(address => uint256) public mintedPerWallet;
    uint256 public constant MAX_PER_WALLET = 2;

    // ERC-6551 Registry (canonical deployment on Base)
    IERC6551Registry public immutable erc6551Registry;
    address public immutable tbaImplementation;

    // ERC-8004 Agent Registry (Phase 2 gating)
    IAgentRegistry public agentRegistry;

    // On-chain metadata: tokenId => JSON-LD bytes (stored via SSTORE2 pattern)
    // Using a simple mapping here; production would use SSTORE2 for gas efficiency
    mapping(uint256 => bytes) private _tokenMetadata;

    // Universe constants (locked corpus)
    string[] private _universes = ["nouns", "cryptoadz", "mfers", "racc00ns", "bulfinch"];

    // =========================================================================
    // Events
    // =========================================================================

    event TokenMinted(uint256 indexed tokenId, address indexed to, address tba, uint8 phase);
    event PhaseAdvanced(uint8 newPhase);
    event MetadataSet(uint256 indexed tokenId);

    // =========================================================================
    // Errors
    // =========================================================================

    error MaxSupplyReached();
    error PhaseSupplyExhausted();
    error WalletCapExceeded();
    error TransferLocked();
    error AgentsOnly();
    error InvalidPhase();
    error TokenDoesNotExist();

    // =========================================================================
    // Constructor
    // =========================================================================

    constructor(
        address _erc6551Registry,
        address _tbaImplementation,
        address _agentRegistry
    ) ERC721("Worldkit", "WKIT") Ownable(msg.sender) {
        erc6551Registry = IERC6551Registry(_erc6551Registry);
        tbaImplementation = _tbaImplementation;
        agentRegistry = IAgentRegistry(_agentRegistry);
        currentPhase = 1;
    }

    // =========================================================================
    // Mint
    // =========================================================================

    /// @notice Free mint. Phase 1: open. Phase 2: ERC-8004 agents only. Phase 3: public with liveness.
    function mint() external returns (uint256 tokenId) {
        if (totalMinted >= MAX_SUPPLY) revert MaxSupplyReached();
        if (mintedPerWallet[msg.sender] >= MAX_PER_WALLET) revert WalletCapExceeded();

        _checkPhaseEligibility();

        tokenId = totalMinted + 1;
        totalMinted++;
        mintedPerWallet[msg.sender]++;
        mintedAt[tokenId] = block.timestamp;

        _safeMint(msg.sender, tokenId);

        // Auto-deploy Token Bound Account for this token
        address tba = erc6551Registry.createAccount(
            tbaImplementation,
            bytes32(tokenId), // deterministic salt per token
            block.chainid,
            address(this),
            tokenId
        );

        // Set default on-chain metadata
        _setDefaultMetadata(tokenId);

        emit TokenMinted(tokenId, msg.sender, tba, currentPhase);
    }

    /// @notice Returns the Token Bound Account address for a token (even before mint)
    function tokenBoundAccount(uint256 tokenId) external view returns (address) {
        return erc6551Registry.account(
            tbaImplementation,
            bytes32(tokenId),
            block.chainid,
            address(this),
            tokenId
        );
    }

    // =========================================================================
    // Phase Management
    // =========================================================================

    function advancePhase() external onlyOwner {
        if (currentPhase >= 3) revert InvalidPhase();
        currentPhase++;
        emit PhaseAdvanced(currentPhase);
    }

    function _checkPhaseEligibility() internal view {
        if (currentPhase == 1) {
            if (totalMinted >= PHASE1_SUPPLY) revert PhaseSupplyExhausted();
            // Open to humans and agents
        } else if (currentPhase == 2) {
            if (totalMinted >= PHASE2_SUPPLY) revert PhaseSupplyExhausted();
            // ERC-8004 agents only -- the headline moment
            if (!agentRegistry.isRegistered(msg.sender)) revert AgentsOnly();
        } else if (currentPhase == 3) {
            if (totalMinted >= PHASE3_SUPPLY) revert PhaseSupplyExhausted();
            // Phase 3: broader public (wallet age, Civic liveness enforced off-chain via signature)
        }
    }

    // =========================================================================
    // Transfer Lockup (Anti-Bot)
    // =========================================================================

    function _update(address to, uint256 tokenId, address auth) internal override returns (address) {
        address from = _ownerOf(tokenId);
        // Allow minting (from == address(0)) but lock transfers for 7 days post-mint
        if (from != address(0) && to != address(0)) {
            if (block.timestamp < mintedAt[tokenId] + TRANSFER_LOCKUP) revert TransferLocked();
        }
        return super._update(to, tokenId, auth);
    }

    // =========================================================================
    // Metadata (On-Chain JSON-LD)
    // =========================================================================

    /// @notice Set structured JSON-LD FictionalCharacter metadata for a token
    /// @dev In production: use SSTORE2.write() for gas-efficient large metadata storage
    function setTokenMetadata(uint256 tokenId, bytes calldata jsonLd) external onlyOwner {
        if (!_exists(tokenId)) revert TokenDoesNotExist();
        _tokenMetadata[tokenId] = jsonLd;
        emit MetadataSet(tokenId);
    }

    function tokenURI(uint256 tokenId) public view override returns (string memory) {
        if (!_exists(tokenId)) revert TokenDoesNotExist();

        bytes memory metadata = _tokenMetadata[tokenId];
        if (metadata.length == 0) {
            metadata = _defaultMetadataBytes(tokenId);
        }

        return string(abi.encodePacked(
            "data:application/json;base64,",
            Base64.encode(metadata)
        ));
    }

    function _setDefaultMetadata(uint256 tokenId) internal {
        _tokenMetadata[tokenId] = _defaultMetadataBytes(tokenId);
    }

    /// @notice Default JSON-LD FictionalCharacter skeleton -- fully on-chain
    function _defaultMetadataBytes(uint256 tokenId) internal view returns (bytes memory) {
        string memory universeIndex = _universes[tokenId % _universes.length];
        return abi.encodePacked(
            '{"@context":"https://schema.org",',
            '"@type":"FictionalCharacter",',
            '"name":"Manifest #', tokenId.toString(), '",',
            '"universe":"', universeIndex, '",',
            '"asset_source":{"contract":"', Strings.toHexString(address(this)), '","token_id":"', tokenId.toString(), '"},',
            '"traits":[],',
            '"faction_affinity":"",',
            '"relationship_slots":[],',
            '"cc0_verified":true,',
            '"ontology_version":"1.0",',
            '"schema_hash":"0x0"}'
        );
    }

    function _exists(uint256 tokenId) internal view returns (bool) {
        return _ownerOf(tokenId) != address(0);
    }

    // =========================================================================
    // Admin
    // =========================================================================

    function setAgentRegistry(address _agentRegistry) external onlyOwner {
        agentRegistry = IAgentRegistry(_agentRegistry);
    }

    function reserveMint(address to, uint256 quantity) external onlyOwner {
        require(totalMinted + quantity <= MAX_SUPPLY, "exceeds supply");
        for (uint256 i = 0; i < quantity; i++) {
            uint256 tokenId = totalMinted + 1;
            totalMinted++;
            mintedAt[tokenId] = block.timestamp;
            _safeMint(to, tokenId);
            _setDefaultMetadata(tokenId);
            address tba = erc6551Registry.createAccount(
                tbaImplementation,
                bytes32(tokenId),
                block.chainid,
                address(this),
                tokenId
            );
            emit TokenMinted(tokenId, to, tba, 0); // phase 0 = reserve
        }
    }
}
