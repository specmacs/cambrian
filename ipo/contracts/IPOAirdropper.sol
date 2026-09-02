// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Ownable2Step, Ownable} from "@openzeppelin/contracts/access/Ownable2Step.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";

/// @title IPOAirdropper
/// @notice Pushes the hour's trending Pons coins straight out to IPO holders.
///
/// @dev Holders do nothing. The keeper snapshots IPO balances, computes each holder's pro-rata
///      share off-chain, and sends the coins here in batches — tokens land in wallets without
///      anyone claiming, approving, or paying gas.
///
///      Batches are idempotent by `(epochId, batchIndex, token)`. A keeper that crashes mid-airdrop
///      and restarts re-sends the same batch id, which reverts rather than paying a batch twice.
///      Idempotency is deliberately at batch rather than recipient granularity: the keeper is
///      already trusted to compute the split, so the risk worth engineering against is a crash and
///      retry, not a keeper choosing to pay someone twice.
///
///      What the contract does enforce is that a token can never pay out more than the treasury
///      funded for it. A miscomputed split can misallocate one coin; it cannot overdraw one, and it
///      cannot touch another coin's balance.
contract IPOAirdropper is Ownable2Step, ReentrancyGuard, Pausable {
    using SafeERC20 for IERC20;

    /// @notice The only address permitted to fund airdrops.
    address public treasury;

    mapping(address keeper => bool) public isKeeper;

    /// @notice Total the treasury has funded, per token.
    mapping(address token => uint256) public totalFunded;
    /// @notice Total sent to holders, per token.
    mapping(address token => uint256) public totalSent;
    /// @notice Batches already delivered, keyed by (epochId, batchIndex, token).
    mapping(bytes32 batchId => bool) public batchSent;

    address[] private _tokens;
    mapping(address token => bool) public isAirdropped;

    uint256 public totalTransfers;

    event TreasurySet(address indexed treasury);
    event KeeperSet(address indexed keeper, bool allowed);
    event Funded(address indexed token, uint256 amount, uint256 totalFunded);
    event AirdropSent(
        uint256 indexed epochId,
        uint256 indexed batchIndex,
        address indexed token,
        uint256 recipients,
        uint256 amount
    );

    error NotTreasury();
    error NotKeeper();
    error ZeroAddress();
    error LengthMismatch();
    error EmptyBatch();
    error BatchAlreadySent(bytes32 batchId);
    error ExceedsFunded(address token, uint256 requested, uint256 available);

    constructor(address owner_) Ownable(owner_) {
        if (owner_ == address(0)) revert ZeroAddress();
    }

    modifier onlyTreasury() {
        if (msg.sender != treasury) revert NotTreasury();
        _;
    }

    modifier onlyKeeper() {
        if (!isKeeper[msg.sender]) revert NotKeeper();
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

    function setKeeper(address keeper, bool allowed) external onlyOwner {
        if (keeper == address(0)) revert ZeroAddress();
        isKeeper[keeper] = allowed;
        emit KeeperSet(keeper, allowed);
    }

    function pause() external onlyOwner {
        _pause();
    }

    function unpause() external onlyOwner {
        _unpause();
    }

    // ---------------------------------------------------------------------
    // Funding
    // ---------------------------------------------------------------------

    /// @notice Record coins the treasury has already transferred in.
    function fund(address token, uint256 amount) external onlyTreasury {
        if (!isAirdropped[token]) {
            isAirdropped[token] = true;
            _tokens.push(token);
        }
        totalFunded[token] += amount;
        emit Funded(token, amount, totalFunded[token]);
    }

    // ---------------------------------------------------------------------
    // Delivery
    // ---------------------------------------------------------------------

    /// @notice Send one batch of an epoch's airdrop for a single token.
    ///
    /// @dev Every coin bought is a Pons launch, and Pons tokens are plain fixed-supply ERC-20s with
    ///      no mint, freeze, blacklist, or transfer hook. A recipient therefore cannot make a
    ///      transfer revert, so one hostile address cannot brick a batch and no per-transfer
    ///      try/catch is warranted.
    ///
    ///      Zero amounts are skipped rather than rejected, so the keeper can pass a holder list
    ///      straight through without pre-filtering the tail that rounds to nothing.
    ///
    /// @param epochId The treasury epoch these coins were bought in.
    /// @param batchIndex Index of this batch within the epoch, for this token.
    /// @param token The coin being distributed.
    /// @param recipients Holders to pay.
    /// @param amounts Amount for each holder, index-aligned with `recipients`.
    function airdrop(
        uint256 epochId,
        uint256 batchIndex,
        address token,
        address[] calldata recipients,
        uint256[] calldata amounts
    ) external onlyKeeper nonReentrant whenNotPaused returns (uint256 sent, uint256 paidCount) {
        uint256 len = recipients.length;
        if (len == 0) revert EmptyBatch();
        if (amounts.length != len) revert LengthMismatch();

        bytes32 batchId = keccak256(abi.encode(epochId, batchIndex, token));
        if (batchSent[batchId]) revert BatchAlreadySent(batchId);
        batchSent[batchId] = true;

        for (uint256 i; i < len; ++i) {
            sent += amounts[i];
        }

        uint256 newTotal = totalSent[token] + sent;
        if (newTotal > totalFunded[token]) {
            revert ExceedsFunded(token, newTotal, totalFunded[token]);
        }
        totalSent[token] = newTotal;

        for (uint256 i; i < len; ++i) {
            uint256 amount = amounts[i];
            if (amount == 0) continue;
            IERC20(token).safeTransfer(recipients[i], amount);
            unchecked {
                ++paidCount;
            }
        }
        totalTransfers += paidCount;

        emit AirdropSent(epochId, batchIndex, token, paidCount, sent);
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

    /// @notice Funded but not yet delivered, per token. Rounding dust rolls into the next epoch.
    function undistributed(address token) external view returns (uint256) {
        return totalFunded[token] - totalSent[token];
    }

    /// @notice Whether a given batch has already been delivered.
    function isBatchSent(uint256 epochId, uint256 batchIndex, address token)
        external
        view
        returns (bool)
    {
        return batchSent[keccak256(abi.encode(epochId, batchIndex, token))];
    }
}
