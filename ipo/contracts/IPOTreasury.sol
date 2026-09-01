// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Ownable2Step, Ownable} from "@openzeppelin/contracts/access/Ownable2Step.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";

import {IPonsFactory, IPonsFeeEscrow, LaunchedToken, PonsPhase} from "./interfaces/IPons.sol";
import {IPoolManager, IUnlockCallback, PoolKey, SwapParams, V4Constants} from "./interfaces/IUniswapV4.sol";
import {IPOVault} from "./IPOVault.sol";

/// @title IPOTreasury
/// @notice Collects the IPO creator tax from Pons and spends it on newly bonded Pons projects,
///         which it hands to the vault for holders to redeem.
///
/// @dev The 4% is levied by Pons itself. IPO is launched with `creatorTaxBps = 400` and
///      `creatorFeeRecipient = address(this)`, so the tax is charged on the quote side of every
///      buy and sell — on the bonding curve and, after graduation, by the Pons meme hook — and
///      accrues to this contract as ETH in the Pons fee escrow. Because it arrives as ETH, the
///      treasury never has to sell IPO to fund a purchase.
///
///      Keepers cannot choose where the money goes. They name a token; this contract reads that
///      token's launch record from the Pons factory, rebuilds the pool key itself, and swaps
///      through the pool manager directly. There is no arbitrary-calldata path, and no owner
///      withdrawal: ETH that lands here can only leave as a purchase destined for the vault.
contract IPOTreasury is Ownable2Step, ReentrancyGuard, Pausable, IUnlockCallback {
    using SafeERC20 for IERC20;

    /// @notice Native ETH, as v4 and Pons both represent it.
    address public constant NATIVE = address(0);

    IPonsFactory public immutable PONS_FACTORY;
    IPonsFeeEscrow public immutable PONS_FEE_ESCROW;
    IPoolManager public immutable POOL_MANAGER;
    /// @notice The Pons meme hook that every graduated pool is keyed to.
    address public immutable PONS_MEME_HOOK;
    /// @notice The IPO token whose creator tax funds this treasury.
    address public immutable IPO;

    IPOVault public vault;

    // --- policy ---------------------------------------------------------

    /// @notice ETH spent per purchase.
    uint256 public buySize = 0.05 ether;
    /// @notice ETH held back and never spent.
    uint256 public minReserve;
    /// @notice Minimum gap between purchases.
    uint256 public buyCooldown = 5 minutes;
    /// @notice How long after graduation a project stays eligible. "Newly bonded" is the thesis.
    uint256 public maxGraduationAge = 24 hours;
    /// @notice Minimum quote a launch had to raise on its curve to be worth buying.
    uint256 public minGraduationThreshold = 4.2 ether;

    uint256 public lastBuyAt;

    mapping(address token => bool) public purchased;
    mapping(address keeper => bool) public isKeeper;
    uint256 public totalPurchases;
    uint256 public totalEthSpent;
    uint256 public totalTaxClaimed;

    event VaultSet(address indexed vault);
    event KeeperSet(address indexed keeper, bool allowed);
    event PolicySet(
        uint256 buySize,
        uint256 minReserve,
        uint256 buyCooldown,
        uint256 maxGraduationAge,
        uint256 minGraduationThreshold
    );
    event TaxClaimed(uint256 amount);
    event TaxTokenClaimed(address indexed token, uint256 amount);
    event Purchased(address indexed token, uint256 ethIn, uint256 tokensOut);

    error NotKeeper();
    error ZeroAddress();
    error VaultNotSet();
    error NotAPonsLaunch(address token);
    error NotGraduated(address token, uint8 phase);
    error NotEthPaired(address token);
    error AlreadyPurchased(address token);
    error TooOld(address token);
    error ThresholdTooLow(address token);
    error CooldownActive();
    error InsufficientFunds(uint256 available, uint256 needed);
    error SlippageExceeded(uint256 got, uint256 minOut);
    error DeadlinePassed();
    error OnlyPoolManager();
    error NothingToClaim();

    modifier onlyKeeper() {
        if (!isKeeper[msg.sender]) revert NotKeeper();
        _;
    }

    constructor(
        address ponsFactory,
        address ponsFeeEscrow,
        address poolManager,
        address ponsMemeHook,
        address ipo,
        address owner_
    ) Ownable(owner_) {
        if (
            ponsFactory == address(0) || ponsFeeEscrow == address(0) || poolManager == address(0)
                || ponsMemeHook == address(0) || ipo == address(0) || owner_ == address(0)
        ) revert ZeroAddress();
        PONS_FACTORY = IPonsFactory(ponsFactory);
        PONS_FEE_ESCROW = IPonsFeeEscrow(ponsFeeEscrow);
        POOL_MANAGER = IPoolManager(poolManager);
        PONS_MEME_HOOK = ponsMemeHook;
        IPO = ipo;
    }

    /// @dev Receives the tax from the Pons fee escrow, and ETH taken back from the pool manager.
    receive() external payable {}

    // ---------------------------------------------------------------------
    // Configuration
    // ---------------------------------------------------------------------

    function setVault(address vault_) external onlyOwner {
        if (vault_ == address(0)) revert ZeroAddress();
        vault = IPOVault(vault_);
        emit VaultSet(vault_);
    }

    function setKeeper(address keeper, bool allowed) external onlyOwner {
        if (keeper == address(0)) revert ZeroAddress();
        isKeeper[keeper] = allowed;
        emit KeeperSet(keeper, allowed);
    }

    function setPolicy(
        uint256 buySize_,
        uint256 minReserve_,
        uint256 buyCooldown_,
        uint256 maxGraduationAge_,
        uint256 minGraduationThreshold_
    ) external onlyOwner {
        buySize = buySize_;
        minReserve = minReserve_;
        buyCooldown = buyCooldown_;
        maxGraduationAge = maxGraduationAge_;
        minGraduationThreshold = minGraduationThreshold_;
        emit PolicySet(buySize_, minReserve_, buyCooldown_, maxGraduationAge_, minGraduationThreshold_);
    }

    function pause() external onlyOwner {
        _pause();
    }

    function unpause() external onlyOwner {
        _unpause();
    }

    // ---------------------------------------------------------------------
    // Tax collection
    // ---------------------------------------------------------------------

    /// @notice Pull the accrued IPO creator tax out of the Pons fee escrow. Permissionless.
    function claimTax() external returns (uint256 claimed) {
        uint256 before = address(this).balance;
        if (PONS_FEE_ESCROW.balanceOf(address(this)) == 0) revert NothingToClaim();
        PONS_FEE_ESCROW.claim();
        claimed = address(this).balance - before;
        totalTaxClaimed += claimed;
        emit TaxClaimed(claimed);
    }

    /// @notice Pull tax that accrued in an ERC-20 quote asset and hand it straight to the vault.
    /// @dev Only reachable if IPO is ever paired against an approved ERC-20 rather than native ETH.
    ///      Such a balance cannot fund an ETH-denominated purchase, so it is distributed as-is.
    function claimTaxToken(address token) external returns (uint256 claimed) {
        if (address(vault) == address(0)) revert VaultNotSet();
        uint256 before = IERC20(token).balanceOf(address(this));
        if (PONS_FEE_ESCROW.balanceOfToken(address(this), token) == 0) revert NothingToClaim();
        PONS_FEE_ESCROW.claimToken(token);
        claimed = IERC20(token).balanceOf(address(this)) - before;
        if (claimed != 0) {
            IERC20(token).safeTransfer(address(vault), claimed);
            vault.deposit(token, claimed);
        }
        emit TaxTokenClaimed(token, claimed);
    }

    // ---------------------------------------------------------------------
    // Eligibility
    // ---------------------------------------------------------------------

    /// @notice Whether `token` may be bought right now, and if not, why not.
    /// @dev Mirrors the checks in `buy` so keepers can filter off-chain without burning gas.
    function eligibility(address token) public view returns (bool ok, string memory reason) {
        LaunchedToken memory record = PONS_FACTORY.getLaunchedToken(token);
        if (!record.exists) return (false, "not a pons launch");
        if (record.phase != uint8(PonsPhase.PoolCreated)) return (false, "not graduated");
        if (record.pairToken != NATIVE) return (false, "not eth paired");
        if (purchased[token]) return (false, "already purchased");
        if (record.graduationThreshold < minGraduationThreshold) return (false, "threshold too low");
        if (record.sweptAt == 0 || block.timestamp > record.sweptAt + maxGraduationAge) {
            return (false, "too old");
        }
        if (block.timestamp < lastBuyAt + buyCooldown) return (false, "cooldown active");
        if (address(this).balance < buySize + minReserve) return (false, "insufficient funds");
        if (paused()) return (false, "paused");
        if (address(vault) == address(0)) return (false, "vault not set");
        return (true, "");
    }

    function _requireEligible(address token) internal view returns (LaunchedToken memory record) {
        record = PONS_FACTORY.getLaunchedToken(token);
        if (!record.exists) revert NotAPonsLaunch(token);
        if (record.phase != uint8(PonsPhase.PoolCreated)) revert NotGraduated(token, record.phase);
        if (record.pairToken != NATIVE) revert NotEthPaired(token);
        if (purchased[token]) revert AlreadyPurchased(token);
        if (record.graduationThreshold < minGraduationThreshold) revert ThresholdTooLow(token);
        if (record.sweptAt == 0 || block.timestamp > record.sweptAt + maxGraduationAge) revert TooOld(token);
        if (block.timestamp < lastBuyAt + buyCooldown) revert CooldownActive();
    }

    // ---------------------------------------------------------------------
    // Buying
    // ---------------------------------------------------------------------

    /// @notice Buy `buySize` worth of a newly bonded Pons project and deposit it in the vault.
    /// @param token The graduated Pons launch to buy.
    /// @param minTokensOut Slippage bound, quoted off-chain by the keeper.
    /// @param deadline Latest timestamp this purchase may execute.
    function buy(address token, uint256 minTokensOut, uint256 deadline)
        external
        onlyKeeper
        nonReentrant
        whenNotPaused
        returns (uint256 tokensOut)
    {
        if (block.timestamp > deadline) revert DeadlinePassed();
        if (address(vault) == address(0)) revert VaultNotSet();

        LaunchedToken memory record = _requireEligible(token);

        uint256 ethIn = buySize;
        uint256 balance = address(this).balance;
        if (balance < ethIn + minReserve) revert InsufficientFunds(balance, ethIn + minReserve);

        // Effects before the external swap.
        purchased[token] = true;
        lastBuyAt = block.timestamp;
        totalPurchases += 1;
        totalEthSpent += ethIn;

        bytes memory result = POOL_MANAGER.unlock(abi.encode(token, record.poolFee, record.tickSpacing, ethIn));
        tokensOut = abi.decode(result, (uint256));
        if (tokensOut < minTokensOut) revert SlippageExceeded(tokensOut, minTokensOut);

        IERC20(token).safeTransfer(address(vault), tokensOut);
        vault.deposit(token, tokensOut);

        emit Purchased(token, ethIn, tokensOut);
    }

    /// @dev Pons keys every graduated pool as (native ETH, token) with the meme hook. Native ETH is
    ///      address(0) and therefore always currency0, so buying is always zeroForOne.
    function unlockCallback(bytes calldata data) external returns (bytes memory) {
        if (msg.sender != address(POOL_MANAGER)) revert OnlyPoolManager();

        (address token, uint24 poolFee, int24 tickSpacing, uint256 ethIn) =
            abi.decode(data, (address, uint24, int24, uint256));

        PoolKey memory key = PoolKey({
            currency0: NATIVE,
            currency1: token,
            fee: poolFee,
            tickSpacing: tickSpacing,
            hooks: PONS_MEME_HOOK
        });

        int256 delta = POOL_MANAGER.swap(
            key,
            SwapParams({
                zeroForOne: true,
                amountSpecified: -int256(ethIn), // exact input
                sqrtPriceLimitX96: V4Constants.MIN_SQRT_PRICE + 1
            }),
            ""
        );

        (int128 amount0, int128 amount1) = V4Constants.decode(delta);

        // amount0 is what we owe in ETH (negative), amount1 what the pool owes us in token.
        uint256 owed = uint256(uint128(-amount0));
        POOL_MANAGER.settle{value: owed}();

        uint256 received = uint256(uint128(amount1));
        POOL_MANAGER.take(token, address(this), received);

        return abi.encode(received);
    }
}
