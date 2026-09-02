// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Ownable2Step, Ownable} from "@openzeppelin/contracts/access/Ownable2Step.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";

import {IPonsFactory, IPonsFeeEscrow, LaunchedToken, PonsPhase} from "./interfaces/IPons.sol";
import {IPoolManager, IUnlockCallback, PoolKey, SwapParams, V4Constants} from "./interfaces/IUniswapV4.sol";
import {IPOVault} from "./IPOVault.sol";
import {IPODistributor} from "./IPODistributor.sol";

/// @notice One trending coin to buy this epoch.
struct BuyOrder {
    address token;
    /// @dev Slippage bound, quoted off-chain against the token's graduated pool.
    uint256 minTokensOut;
}

/// @title IPOTreasury
/// @notice Collects the IPO tax and, once an hour, spends it on trending Pons coins which it hands
///         to holders.
///
/// @dev The 4% is levied by Pons, not by a transfer tax. IPO launches with `creatorTaxBps = 400`
///      and `creatorFeeRecipient = address(this)`, so the tax is charged on the quote side of every
///      buy and sell — on the bonding curve and, after graduation, by the Pons meme hook — and
///      accrues here as ETH. The treasury therefore never sells IPO to fund a purchase.
///
///      An epoch is one `runEpoch` call, rate-limited to `epochDuration` (1 hour). The whole basket
///      is bought under a single pool-manager unlock: swaps accumulate a net ETH debt that is
///      settled once, which is both cheaper and atomic — either the entire hour's basket lands or
///      none of it does.
///
///      "Trending" is not observable on-chain, so the keeper chooses which coins to buy. The
///      contract constrains *what* it will accept — a real Pons launch, graduated, ETH-paired,
///      above a graduation floor, off cooldown — and *how much* can be spent, but not which coin
///      trends. See the trust model in the README.
contract IPOTreasury is Ownable2Step, ReentrancyGuard, Pausable, IUnlockCallback {
    using SafeERC20 for IERC20;

    uint256 internal constant BPS = 10_000;

    /// @notice Native ETH, as v4 and Pons both represent it.
    address public constant NATIVE = address(0);

    IPonsFactory public immutable PONS_FACTORY;
    IPonsFeeEscrow public immutable PONS_FEE_ESCROW;
    IPoolManager public immutable POOL_MANAGER;
    /// @notice The Pons meme hook every graduated pool is keyed to.
    address public immutable PONS_MEME_HOOK;
    /// @notice The IPO token whose creator tax funds this treasury.
    address public immutable IPO;

    /// @notice Receives the share of each purchase that is handed to holders.
    IPODistributor public distributor;
    /// @notice Receives the share of each purchase that is retained as permanent backing.
    IPOVault public vault;

    // --- policy ---------------------------------------------------------

    /// @notice Minimum gap between epochs.
    uint256 public epochDuration = 1 hours;
    /// @notice Share of spendable ETH deployed each epoch.
    uint256 public epochSpendBps = BPS;
    /// @notice Hard ceiling on ETH spent in one epoch.
    uint256 public maxEpochSpend = 5 ether;
    /// @notice Most coins buyable in one epoch. Bounds the gas of a single `runEpoch`.
    uint256 public maxTokensPerEpoch = 10;
    /// @notice ETH held back and never spent.
    uint256 public minReserve;
    /// @notice Minimum gap before the same coin can be bought again.
    uint256 public tokenCooldown = 12 hours;
    /// @notice Minimum quote a launch had to raise on its curve to be worth buying.
    uint256 public minGraduationThreshold = 4.2 ether;
    /// @notice Optional recency window after graduation. Zero disables it.
    /// @dev Left at zero for a trending strategy, where a coin may trend well after it bonds.
    uint256 public maxGraduationAge;
    /// @notice Share of each purchase retained by the vault as permanent backing. Zero distributes
    ///         everything to holders.
    uint256 public vaultBps;

    uint256 public lastEpochAt;
    uint256 public epoch;

    mapping(address token => uint256) public lastBoughtAt;
    mapping(address token => uint256) public totalSpentOn;
    mapping(address keeper => bool) public isKeeper;

    uint256 public totalPurchases;
    uint256 public totalEthSpent;
    uint256 public totalTaxClaimed;

    event DistributorSet(address indexed distributor);
    event VaultSet(address indexed vault);
    event KeeperSet(address indexed keeper, bool allowed);
    event PolicySet(
        uint256 epochDuration,
        uint256 epochSpendBps,
        uint256 maxEpochSpend,
        uint256 maxTokensPerEpoch,
        uint256 minReserve,
        uint256 tokenCooldown,
        uint256 minGraduationThreshold,
        uint256 maxGraduationAge
    );
    event VaultBpsSet(uint256 vaultBps);
    event TaxClaimed(uint256 amount);
    event TaxTokenClaimed(address indexed token, uint256 amount);
    event Bought(address indexed token, uint256 ethIn, uint256 tokensOut, uint256 toHolders, uint256 toVault);
    event EpochRun(uint256 indexed epoch, uint256 tokenCount, uint256 ethSpent);

    error NotKeeper();
    error ZeroAddress();
    error DistributorNotSet();
    error VaultNotSet();
    error NotAPonsLaunch(address token);
    error NotGraduated(address token, uint8 phase);
    error NotEthPaired(address token);
    error TokenOnCooldown(address token);
    error TooOld(address token);
    error ThresholdTooLow(address token);
    error EpochNotElapsed(uint256 nextEpochAt);
    error NoOrders();
    error TooManyOrders(uint256 given, uint256 max);
    error DuplicateOrUnsorted();
    error NothingToSpend();
    error SlippageExceeded(address token, uint256 got, uint256 minOut);
    error DeadlinePassed();
    error OnlyPoolManager();
    error NothingToClaim();
    error InvalidBps();

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

    /// @dev Receives the tax from the Pons fee escrow, and ETH refunded by the pool manager.
    receive() external payable {}

    // ---------------------------------------------------------------------
    // Configuration
    // ---------------------------------------------------------------------

    function setDistributor(address distributor_) external onlyOwner {
        if (distributor_ == address(0)) revert ZeroAddress();
        distributor = IPODistributor(distributor_);
        emit DistributorSet(distributor_);
    }

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

    /// @notice Share of each purchase retained by the vault instead of handed to holders.
    function setVaultBps(uint256 vaultBps_) external onlyOwner {
        if (vaultBps_ > BPS) revert InvalidBps();
        if (vaultBps_ != 0 && address(vault) == address(0)) revert VaultNotSet();
        vaultBps = vaultBps_;
        emit VaultBpsSet(vaultBps_);
    }

    function setPolicy(
        uint256 epochDuration_,
        uint256 epochSpendBps_,
        uint256 maxEpochSpend_,
        uint256 maxTokensPerEpoch_,
        uint256 minReserve_,
        uint256 tokenCooldown_,
        uint256 minGraduationThreshold_,
        uint256 maxGraduationAge_
    ) external onlyOwner {
        if (epochSpendBps_ > BPS) revert InvalidBps();
        epochDuration = epochDuration_;
        epochSpendBps = epochSpendBps_;
        maxEpochSpend = maxEpochSpend_;
        maxTokensPerEpoch = maxTokensPerEpoch_;
        minReserve = minReserve_;
        tokenCooldown = tokenCooldown_;
        minGraduationThreshold = minGraduationThreshold_;
        maxGraduationAge = maxGraduationAge_;
        emit PolicySet(
            epochDuration_,
            epochSpendBps_,
            maxEpochSpend_,
            maxTokensPerEpoch_,
            minReserve_,
            tokenCooldown_,
            minGraduationThreshold_,
            maxGraduationAge_
        );
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
        if (PONS_FEE_ESCROW.balanceOf(address(this)) == 0) revert NothingToClaim();
        claimed = _claimTax();
    }

    function _claimTax() internal returns (uint256 claimed) {
        uint256 before = address(this).balance;
        PONS_FEE_ESCROW.claim();
        claimed = address(this).balance - before;
        totalTaxClaimed += claimed;
        emit TaxClaimed(claimed);
    }

    /// @notice Pull tax that accrued in an ERC-20 quote asset and hand it straight to holders.
    /// @dev Only reachable if IPO is ever paired against an approved ERC-20 rather than native ETH.
    ///      Such a balance cannot fund an ETH-denominated purchase, so it is distributed as-is.
    function claimTaxToken(address token) external returns (uint256 claimed) {
        if (address(distributor) == address(0)) revert DistributorNotSet();
        uint256 before = IERC20(token).balanceOf(address(this));
        if (PONS_FEE_ESCROW.balanceOfToken(address(this), token) == 0) revert NothingToClaim();
        PONS_FEE_ESCROW.claimToken(token);
        claimed = IERC20(token).balanceOf(address(this)) - before;
        if (claimed != 0) {
            IERC20(token).safeTransfer(address(distributor), claimed);
            distributor.fund(token, claimed);
        }
        emit TaxTokenClaimed(token, claimed);
    }

    // ---------------------------------------------------------------------
    // Views
    // ---------------------------------------------------------------------

    /// @notice Timestamp the next epoch may run at.
    function nextEpochAt() public view returns (uint256) {
        return lastEpochAt + epochDuration;
    }

    /// @notice ETH available to deploy right now, before the per-epoch share is applied.
    function spendable() public view returns (uint256) {
        uint256 balance = address(this).balance;
        return balance > minReserve ? balance - minReserve : 0;
    }

    /// @notice ETH that would be deployed if an epoch ran now.
    function epochBudget() public view returns (uint256 budget) {
        budget = (spendable() * epochSpendBps) / BPS;
        if (budget > maxEpochSpend) budget = maxEpochSpend;
    }

    /// @notice Whether `token` may be bought right now, and if not, why not.
    /// @dev Mirrors the per-token checks in `runEpoch` so the keeper can filter its trending
    ///      candidates off-chain without burning gas or landing a reverting transaction.
    function eligibility(address token) public view returns (bool ok, string memory reason) {
        LaunchedToken memory record = PONS_FACTORY.getLaunchedToken(token);
        if (!record.exists) return (false, "not a pons launch");
        if (record.phase != uint8(PonsPhase.PoolCreated)) return (false, "not graduated");
        if (record.pairToken != NATIVE) return (false, "not eth paired");
        if (record.graduationThreshold < minGraduationThreshold) return (false, "threshold too low");
        if (block.timestamp < lastBoughtAt[token] + tokenCooldown) return (false, "token on cooldown");
        if (maxGraduationAge != 0 && (record.sweptAt == 0 || block.timestamp > record.sweptAt + maxGraduationAge)) {
            return (false, "too old");
        }
        return (true, "");
    }

    /// @notice Whether an epoch can run right now, and if not, why not.
    function epochReady() external view returns (bool ok, string memory reason) {
        if (paused()) return (false, "paused");
        if (address(distributor) == address(0)) return (false, "distributor not set");
        if (block.timestamp < nextEpochAt()) return (false, "epoch not elapsed");
        // The keeper claims accrued tax as part of the epoch, so count it as spendable.
        uint256 accrued = PONS_FEE_ESCROW.balanceOf(address(this));
        if (spendable() + accrued == 0) return (false, "nothing to spend");
        return (true, "");
    }

    function _requireEligible(address token) internal view {
        LaunchedToken memory record = PONS_FACTORY.getLaunchedToken(token);
        if (!record.exists) revert NotAPonsLaunch(token);
        if (record.phase != uint8(PonsPhase.PoolCreated)) revert NotGraduated(token, record.phase);
        if (record.pairToken != NATIVE) revert NotEthPaired(token);
        if (record.graduationThreshold < minGraduationThreshold) revert ThresholdTooLow(token);
        if (block.timestamp < lastBoughtAt[token] + tokenCooldown) revert TokenOnCooldown(token);
        if (maxGraduationAge != 0 && (record.sweptAt == 0 || block.timestamp > record.sweptAt + maxGraduationAge)) {
            revert TooOld(token);
        }
    }

    // ---------------------------------------------------------------------
    // The hourly epoch
    // ---------------------------------------------------------------------

    /// @notice Claim the hour's tax, buy the trending coins the keeper selected, and hand them out.
    ///
    /// @dev The epoch budget is split equally across the orders. Equal weighting is deliberate: it
    ///      removes the keeper's discretion over position sizing, so the only judgement they exercise
    ///      is which coins are trending.
    ///
    ///      `orders` must be strictly ascending by token address, which rules out the duplicate
    ///      entries that would otherwise let one coin take several slices of the budget.
    ///
    /// @param orders Trending coins to buy, strictly ascending by token address.
    /// @param deadline Latest timestamp this epoch may execute.
    function runEpoch(BuyOrder[] calldata orders, uint256 deadline)
        external
        onlyKeeper
        nonReentrant
        whenNotPaused
        returns (uint256[] memory received)
    {
        if (block.timestamp > deadline) revert DeadlinePassed();
        if (address(distributor) == address(0)) revert DistributorNotSet();
        if (block.timestamp < nextEpochAt()) revert EpochNotElapsed(nextEpochAt());

        uint256 n = orders.length;
        if (n == 0) revert NoOrders();
        if (n > maxTokensPerEpoch) revert TooManyOrders(n, maxTokensPerEpoch);

        // Sweep any tax accrued since the last epoch so it is deployed this hour, not next.
        if (PONS_FEE_ESCROW.balanceOf(address(this)) != 0) _claimTax();

        address prev;
        for (uint256 i; i < n; ++i) {
            address token = orders[i].token;
            if (token <= prev) revert DuplicateOrUnsorted();
            prev = token;
            _requireEligible(token);
        }

        uint256 perToken = epochBudget() / n;
        if (perToken == 0) revert NothingToSpend();
        uint256 spent = perToken * n;

        // Effects before the swap.
        lastEpochAt = block.timestamp;
        unchecked {
            epoch += 1;
        }
        for (uint256 i; i < n; ++i) {
            lastBoughtAt[orders[i].token] = block.timestamp;
            totalSpentOn[orders[i].token] += perToken;
        }
        totalPurchases += n;
        totalEthSpent += spent;

        bytes memory result = POOL_MANAGER.unlock(abi.encode(_poolInputs(orders), perToken));
        received = abi.decode(result, (uint256[]));

        for (uint256 i; i < n; ++i) {
            if (received[i] < orders[i].minTokensOut) {
                revert SlippageExceeded(orders[i].token, received[i], orders[i].minTokensOut);
            }
            _route(orders[i].token, perToken, received[i]);
        }

        emit EpochRun(epoch, n, spent);
    }

    /// @dev Read each order's pool shape from the Pons factory so the swap cannot be pointed
    ///      anywhere the keeper chooses.
    function _poolInputs(BuyOrder[] calldata orders)
        internal
        view
        returns (bytes[] memory inputs)
    {
        inputs = new bytes[](orders.length);
        for (uint256 i; i < orders.length; ++i) {
            LaunchedToken memory record = PONS_FACTORY.getLaunchedToken(orders[i].token);
            inputs[i] = abi.encode(orders[i].token, record.poolFee, record.tickSpacing);
        }
    }

    /// @dev Split a purchase between holders and the vault, then record it with each sink.
    function _route(address token, uint256 ethIn, uint256 amount) internal {
        uint256 toVault = (amount * vaultBps) / BPS;
        uint256 toHolders = amount - toVault;

        if (toHolders != 0) {
            IERC20(token).safeTransfer(address(distributor), toHolders);
            distributor.fund(token, toHolders);
        }
        if (toVault != 0) {
            IERC20(token).safeTransfer(address(vault), toVault);
            vault.deposit(token, toVault);
        }
        emit Bought(token, ethIn, amount, toHolders, toVault);
    }

    /// @dev Pons keys every graduated pool as (native ETH, token) with the meme hook. Native ETH is
    ///      address(0) and therefore always currency0, so buying is always zeroForOne.
    ///
    ///      Swaps run under a single unlock and accumulate one net ETH debt, settled once at the
    ///      end. Any order failing here reverts the whole epoch rather than leaving a half-filled
    ///      basket with an unresolved delta.
    function unlockCallback(bytes calldata data) external returns (bytes memory) {
        if (msg.sender != address(POOL_MANAGER)) revert OnlyPoolManager();

        (bytes[] memory inputs, uint256 perToken) = abi.decode(data, (bytes[], uint256));

        uint256 n = inputs.length;
        address[] memory tokens = new address[](n);
        uint256[] memory received = new uint256[](n);
        uint256 totalOwed;

        for (uint256 i; i < n; ++i) {
            (address token, uint24 poolFee, int24 tickSpacing) =
                abi.decode(inputs[i], (address, uint24, int24));
            tokens[i] = token;

            int256 delta = POOL_MANAGER.swap(
                PoolKey({
                    currency0: NATIVE,
                    currency1: token,
                    fee: poolFee,
                    tickSpacing: tickSpacing,
                    hooks: PONS_MEME_HOOK
                }),
                SwapParams({
                    zeroForOne: true,
                    amountSpecified: -int256(perToken), // exact input
                    sqrtPriceLimitX96: V4Constants.MIN_SQRT_PRICE + 1
                }),
                ""
            );

            (int128 amount0, int128 amount1) = V4Constants.decode(delta);
            totalOwed += uint256(uint128(-amount0));
            received[i] = uint256(uint128(amount1));
        }

        POOL_MANAGER.settle{value: totalOwed}();
        for (uint256 i; i < n; ++i) {
            POOL_MANAGER.take(tokens[i], address(this), received[i]);
        }

        return abi.encode(received);
    }
}
