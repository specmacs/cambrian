// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {MerkleProof} from "@openzeppelin/contracts/utils/cryptography/MerkleProof.sol";
import {Ownable2Step, Ownable} from "@openzeppelin/contracts/access/Ownable2Step.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

/// @title IPODistributor
/// @notice Distributes the trending Pons coins bought each hour to IPO holders.
///
/// @dev Entitlements are **cumulative**, not per-epoch. Each published root encodes, for every
///      holder and every token, the total that holder has ever been owed. A claim pays the
///      difference against what they have already taken.
///
///      That matters at an hourly cadence. A per-epoch design would publish 24 roots a day and
///      force a holder to submit 24 proofs per token to collect a day's worth; here one proof
///      collects everything owed since their last claim, whenever they get round to it, and a
///      holder who never claims simply keeps accruing.
///
///      Publishing is trusted — no on-chain check can verify a root sums to what was deposited.
///      What the contract does enforce is that claims for a token can never exceed what the
///      treasury actually funded for it, so a bad root can misallocate a token but cannot drain
///      one that was never bought, and cannot touch another token's balance.
contract IPODistributor is Ownable2Step, ReentrancyGuard {
    using SafeERC20 for IERC20;

    /// @notice The only address permitted to fund distributions.
    address public treasury;
    /// @notice The only address permitted to publish a new root.
    address public publisher;

    bytes32 public merkleRoot;
    uint256 public epoch;
    uint256 public rootUpdatedAt;

    /// @notice Cumulative amount each account has already withdrawn, per token.
    mapping(address account => mapping(address token => uint256)) public claimed;
    /// @notice Total the treasury has funded, per token.
    mapping(address token => uint256) public totalFunded;
    /// @notice Total holders have withdrawn, per token.
    mapping(address token => uint256) public totalClaimed;

    address[] private _tokens;
    mapping(address token => bool) public isDistributed;

    event TreasurySet(address indexed treasury);
    event PublisherSet(address indexed publisher);
    event Funded(address indexed token, uint256 amount, uint256 totalFunded);
    event RootPublished(uint256 indexed epoch, bytes32 root);
    event Claimed(address indexed account, address indexed token, uint256 amount, uint256 cumulative);

    error NotTreasury();
    error NotPublisher();
    error ZeroAddress();
    error InvalidProof();
    error NothingToClaim();
    error NoRoot();
    error ExceedsFunded(address token, uint256 requested, uint256 available);
    error LengthMismatch();

    constructor(address owner_) Ownable(owner_) {
        if (owner_ == address(0)) revert ZeroAddress();
    }

    modifier onlyTreasury() {
        if (msg.sender != treasury) revert NotTreasury();
        _;
    }

    // ---------------------------------------------------------------------
    // Configuration
    // ---------------------------------------------------------------------

    function setTreasury(address treasury_) external onlyOwner {
        if (treasury_ == address(0)) revert ZeroAddress();
        treasury = treasury_;
        emit TreasurySet(treasury_);
    }

    function setPublisher(address publisher_) external onlyOwner {
        if (publisher_ == address(0)) revert ZeroAddress();
        publisher = publisher_;
        emit PublisherSet(publisher_);
    }

    // ---------------------------------------------------------------------
    // Funding and roots
    // ---------------------------------------------------------------------

    /// @notice Record tokens the treasury has already transferred in.
    function fund(address token, uint256 amount) external onlyTreasury {
        if (!isDistributed[token]) {
            isDistributed[token] = true;
            _tokens.push(token);
        }
        totalFunded[token] += amount;
        emit Funded(token, amount, totalFunded[token]);
    }

    /// @notice Publish the cumulative entitlement tree.
    /// @dev Leaves are `keccak256(bytes.concat(keccak256(abi.encode(account, token, cumulative))))`.
    ///      The inner hash is doubled so that a leaf can never be reinterpreted as an internal node.
    function publishRoot(bytes32 root) external {
        if (msg.sender != publisher && msg.sender != owner()) revert NotPublisher();
        merkleRoot = root;
        rootUpdatedAt = block.timestamp;
        unchecked {
            epoch += 1;
        }
        emit RootPublished(epoch, root);
    }

    // ---------------------------------------------------------------------
    // Claiming
    // ---------------------------------------------------------------------

    /// @notice Withdraw everything owed to `account` for `token` under the current root.
    /// @param cumulativeAmount Total ever owed to `account` for `token`, as encoded in the tree.
    function claim(address account, address token, uint256 cumulativeAmount, bytes32[] calldata proof)
        public
        nonReentrant
        returns (uint256 payout)
    {
        if (merkleRoot == bytes32(0)) revert NoRoot();

        bytes32 leaf = keccak256(bytes.concat(keccak256(abi.encode(account, token, cumulativeAmount))));
        if (!MerkleProof.verifyCalldata(proof, merkleRoot, leaf)) revert InvalidProof();

        uint256 already = claimed[account][token];
        if (cumulativeAmount <= already) revert NothingToClaim();
        unchecked {
            payout = cumulativeAmount - already;
        }

        // A published root cannot hand out more of a token than the treasury funded for it.
        uint256 newTotal = totalClaimed[token] + payout;
        if (newTotal > totalFunded[token]) {
            revert ExceedsFunded(token, newTotal, totalFunded[token]);
        }

        claimed[account][token] = cumulativeAmount;
        totalClaimed[token] = newTotal;

        IERC20(token).safeTransfer(account, payout);
        emit Claimed(account, token, payout, cumulativeAmount);
    }

    /// @notice Claim several tokens in one transaction.
    function claimMany(
        address account,
        address[] calldata tokens_,
        uint256[] calldata cumulativeAmounts,
        bytes32[][] calldata proofs
    ) external returns (uint256[] memory payouts) {
        uint256 len = tokens_.length;
        if (cumulativeAmounts.length != len || proofs.length != len) revert LengthMismatch();
        payouts = new uint256[](len);
        for (uint256 i; i < len; ++i) {
            payouts[i] = claim(account, tokens_[i], cumulativeAmounts[i], proofs[i]);
        }
    }

    // ---------------------------------------------------------------------
    // Views
    // ---------------------------------------------------------------------

    function tokens() external view returns (address[] memory) {
        return _tokens;
    }

    function tokenCount() external view returns (uint256) {
        return _tokens.length;
    }

    /// @notice What `account` would receive for `token` given a cumulative entitlement.
    function claimable(address account, address token, uint256 cumulativeAmount)
        external
        view
        returns (uint256)
    {
        uint256 already = claimed[account][token];
        return cumulativeAmount > already ? cumulativeAmount - already : 0;
    }

    /// @notice Tokens funded but not yet claimed, per token.
    function undistributed(address token) external view returns (uint256) {
        return totalFunded[token] - totalClaimed[token];
    }
}
