// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Ownable2Step, Ownable} from "@openzeppelin/contracts/access/Ownable2Step.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

/// @title IPOVault
/// @notice Custodies the basket of newly bonded Pons projects bought by the treasury and lets
///         IPO holders redeem a pro-rata slice of it.
///
/// @dev Distribution is pull-based and continuous rather than a periodic push. Every purchase the
///      treasury makes raises the basket's per-token backing immediately, for every holder, at zero
///      gas cost to the protocol. A holder converts that backing into assets whenever they choose by
///      redeeming.
///
///      IPO sent in during a redemption is never released again. `IPO` is a Pons token, so it has no
///      `burn` and cannot be sent to address(0); permanently locking it here is the equivalent, and
///      is why the vault's own balance is excluded from `redeemableSupply`.
///
///      The vault has no withdrawal path for the owner. Assets can enter the basket and leave only
///      through `redeem`. This is deliberate: the no-rug guarantee is worth more than the ability to
///      recover a stray token.
contract IPOVault is Ownable2Step, ReentrancyGuard {
    using SafeERC20 for IERC20;

    /// @notice The IPO token, as deployed by the Pons factory.
    IERC20 public immutable IPO;

    /// @notice The only address permitted to add assets to the basket.
    address public treasury;

    address[] private _assets;
    mapping(address asset => bool) public isAsset;

    /// @notice Holders whose balances do not back a claim on the basket (curve, treasury, etc).
    mapping(address account => bool) public isExcluded;
    address[] private _excluded;

    event TreasurySet(address indexed treasury);
    event AssetRegistered(address indexed asset);
    event Deposited(address indexed asset, uint256 amount);
    event Redeemed(address indexed holder, address indexed to, uint256 ipoIn, uint256 assetCount);
    event ExclusionSet(address indexed account, bool excluded);

    error NotTreasury();
    error ZeroAddress();
    error ZeroAmount();
    error AssetsNotSorted();
    error NotRegistered(address asset);
    error NothingRedeemable();
    error CannotExcludeZeroAddress();

    modifier onlyTreasury() {
        if (msg.sender != treasury) revert NotTreasury();
        _;
    }

    constructor(address ipo, address owner_) Ownable(owner_) {
        if (ipo == address(0) || owner_ == address(0)) revert ZeroAddress();
        IPO = IERC20(ipo);
        // The vault's own locked IPO never backs a claim on the basket.
        isExcluded[address(this)] = true;
        _excluded.push(address(this));
        emit ExclusionSet(address(this), true);
    }

    // ---------------------------------------------------------------------
    // Configuration
    // ---------------------------------------------------------------------

    function setTreasury(address treasury_) external onlyOwner {
        if (treasury_ == address(0)) revert ZeroAddress();
        treasury = treasury_;
        emit TreasurySet(treasury_);
    }

    /// @notice Exclude an address's balance from `redeemableSupply`.
    /// @dev Intended for the Pons bonding curve (which holds unsold supply before graduation) and
    ///      for protocol-owned addresses. Excluding an account raises every other holder's claim.
    function setExcluded(address account, bool excluded) external onlyOwner {
        if (account == address(0)) revert CannotExcludeZeroAddress();
        if (isExcluded[account] == excluded) return;
        isExcluded[account] = excluded;
        if (excluded) {
            _excluded.push(account);
        } else {
            uint256 n = _excluded.length;
            for (uint256 i; i < n; ++i) {
                if (_excluded[i] == account) {
                    _excluded[i] = _excluded[n - 1];
                    _excluded.pop();
                    break;
                }
            }
        }
        emit ExclusionSet(account, excluded);
    }

    /// @notice Add a token to the redeemable basket.
    /// @dev Also the recovery path for tokens sent here by mistake: registering one can only ever
    ///      hand it to holders, never to the owner.
    function registerAsset(address asset) public onlyOwner {
        _registerAsset(asset);
    }

    function _registerAsset(address asset) internal {
        if (asset == address(0)) revert ZeroAddress();
        if (asset == address(IPO)) revert NotRegistered(asset); // IPO is the claim, not the collateral
        if (isAsset[asset]) return;
        isAsset[asset] = true;
        _assets.push(asset);
        emit AssetRegistered(asset);
    }

    // ---------------------------------------------------------------------
    // Deposits
    // ---------------------------------------------------------------------

    /// @notice Record a purchase already transferred in by the treasury, registering it if new.
    /// @dev The treasury transfers first and calls this after, so the vault never needs an allowance.
    function deposit(address asset, uint256 amount) external onlyTreasury {
        _registerAsset(asset);
        emit Deposited(asset, amount);
    }

    // ---------------------------------------------------------------------
    // Views
    // ---------------------------------------------------------------------

    function assets() external view returns (address[] memory) {
        return _assets;
    }

    function assetCount() external view returns (uint256) {
        return _assets.length;
    }

    function excludedAccounts() external view returns (address[] memory) {
        return _excluded;
    }

    /// @notice IPO supply that currently backs a claim on the basket.
    function redeemableSupply() public view returns (uint256 supply) {
        supply = IPO.totalSupply();
        uint256 n = _excluded.length;
        for (uint256 i; i < n; ++i) {
            uint256 bal = IPO.balanceOf(_excluded[i]);
            // Defensive: an exclusion list larger than supply should floor at zero, not revert.
            supply = bal >= supply ? 0 : supply - bal;
        }
    }

    /// @notice Assets `ipoAmount` would return today for each of `requestedAssets`.
    function previewRedeem(uint256 ipoAmount, address[] calldata requestedAssets)
        external
        view
        returns (uint256[] memory amounts)
    {
        uint256 supply = redeemableSupply();
        amounts = new uint256[](requestedAssets.length);
        if (supply == 0) return amounts;
        for (uint256 i; i < requestedAssets.length; ++i) {
            amounts[i] = (IERC20(requestedAssets[i]).balanceOf(address(this)) * ipoAmount) / supply;
        }
    }

    // ---------------------------------------------------------------------
    // Redemption
    // ---------------------------------------------------------------------

    /// @notice Burn `ipoAmount` of IPO for a pro-rata slice of each asset in `requestedAssets`.
    ///
    /// @dev Callers choose which assets to take because the basket grows without bound and
    ///      redeeming all of it in one transaction would eventually exceed the block gas limit.
    ///      Taking a subset is strictly favourable to the holders who remain: the redeemer forfeits
    ///      their claim on everything they skipped, which raises per-token backing for everyone else.
    ///
    ///      `requestedAssets` must be strictly ascending, which cheaply rules out the duplicate
    ///      entries that would otherwise pay a redeemer twice for the same asset.
    ///
    /// @param ipoAmount IPO to lock, pulled from the caller.
    /// @param requestedAssets Registered basket assets to claim, strictly ascending by address.
    /// @param to Recipient of the redeemed assets.
    function redeem(uint256 ipoAmount, address[] calldata requestedAssets, address to)
        external
        nonReentrant
        returns (uint256[] memory amounts)
    {
        if (ipoAmount == 0) revert ZeroAmount();
        if (to == address(0)) revert ZeroAddress();

        // Read supply before pulling IPO in: the pull raises the vault's own balance, which is
        // excluded, and would otherwise shrink the denominator the redeemer is measured against.
        uint256 supply = redeemableSupply();
        if (supply == 0) revert NothingRedeemable();

        uint256 len = requestedAssets.length;
        amounts = new uint256[](len);

        // Snapshot balances before any transfer so every asset is priced against the same instant.
        address prev;
        for (uint256 i; i < len; ++i) {
            address asset = requestedAssets[i];
            if (asset <= prev) revert AssetsNotSorted();
            if (!isAsset[asset]) revert NotRegistered(asset);
            prev = asset;
            amounts[i] = (IERC20(asset).balanceOf(address(this)) * ipoAmount) / supply;
        }

        IPO.safeTransferFrom(msg.sender, address(this), ipoAmount);

        for (uint256 i; i < len; ++i) {
            uint256 amount = amounts[i];
            if (amount != 0) IERC20(requestedAssets[i]).safeTransfer(to, amount);
        }

        emit Redeemed(msg.sender, to, ipoAmount, len);
    }
}
