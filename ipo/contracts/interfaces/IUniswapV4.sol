// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

/// @notice Uniswap v4 pool identity. Currencies are sorted ascending; native ETH is address(0).
/// @dev v4 core declares currencies as the `Currency` user-defined value type and hooks as
///      `IHooks`. Both are ABI-identical to `address`, so plain addresses encode the same
///      `PoolId` and keep this integration free of v4-core's dependency tree.
struct PoolKey {
    address currency0;
    address currency1;
    uint24 fee;
    int24 tickSpacing;
    address hooks;
}

struct SwapParams {
    bool zeroForOne;
    /// @dev Negative is exact-input, positive is exact-output.
    int256 amountSpecified;
    uint160 sqrtPriceLimitX96;
}

interface IPoolManager {
    /// @notice Acquire the lock. Calls `unlockCallback` back on the caller.
    function unlock(bytes calldata data) external returns (bytes memory);

    /// @notice Swap against a pool. Only callable while unlocked.
    /// @return delta Packed BalanceDelta: int128 amount0 in the high 128 bits, int128 amount1 in the low.
    function swap(PoolKey memory key, SwapParams memory params, bytes calldata hookData)
        external
        returns (int256 delta);

    /// @notice Snapshot an ERC-20 balance before transferring it in, so `settle` can measure the delta.
    function sync(address currency) external;

    /// @notice Pay what is owed. Send native value directly; ERC-20s must be transferred in after `sync`.
    function settle() external payable returns (uint256 paid);

    /// @notice Withdraw what is owed to the caller.
    function take(address currency, address to, uint256 amount) external;
}

interface IUnlockCallback {
    function unlockCallback(bytes calldata data) external returns (bytes memory);
}

library V4Constants {
    /// @dev Lowest sqrt price the pool will accept; `MIN + 1` is the unbounded zeroForOne limit.
    uint160 internal constant MIN_SQRT_PRICE = 4295128739;
    /// @dev Highest sqrt price the pool will accept; `MAX - 1` is the unbounded oneForZero limit.
    uint160 internal constant MAX_SQRT_PRICE = 1461446703485210103287273052203988822378723970342;

    /// @notice Split a packed BalanceDelta into its two signed halves.
    function decode(int256 delta) internal pure returns (int128 amount0, int128 amount1) {
        assembly ("memory-safe") {
            amount0 := sar(128, delta)
            amount1 := signextend(15, delta)
        }
    }
}
