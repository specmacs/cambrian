// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

/// @notice Graduation phases reported by the Pons factory for a launch.
enum PonsPhase {
    NotGraduated, // 0 - still trading on the bonding curve
    Swept,        // 1 - curve drained, pool creation pending
    PoolCreated,  // 2 - trading on a locked Uniswap v4 pool
    Rescued       // 3 - recovery path was used, do NOT buy
}

/// @notice Per-launch record held by the Pons factory.
/// @dev Field order mirrors the v2 docs' `LaunchedToken` struct.
struct LaunchedToken {
    address token;
    address curve;
    address deployer;
    address creatorFeeRecipient;
    address pairToken;
    uint256 graduationThreshold;
    uint24 poolFee;
    int24 tickSpacing;
    uint16 creatorTaxBps;
    bool buybackEnabled;
    uint8 phase;
    /// @dev The three `swept*` fields are present in the ABI but left at zero by the live
    ///      factory, including on launches that have fully graduated. Nothing on-chain may
    ///      depend on them; graduation recency has to come from the `PoolGraduated` block.
    uint256 sweptQuote;
    uint256 sweptTokens;
    uint256 sweptAt;
    bool exists;
}

/// @notice Launch parameters accepted by the factory at deploy time.
struct TokenParams {
    string name;
    string symbol;
    string logo;
    string description;
    string twitter;
    string telegram;
    string discord;
    string website;
    string farcaster;
    address creatorFeeRecipient;
    uint16 creatorTaxBps;
    bool buybackEnabled;
    bytes32 expectedEconomics;
    bytes32 salt;
}

interface IPonsFactory {
    event TokenLaunched(
        address indexed token,
        address indexed curve,
        address indexed deployer,
        address pairToken,
        uint256 launchConfigId,
        uint256 graduationThreshold
    );

    /// @notice Emitted once a launch's liquidity has been migrated into a locked v4 pool.
    /// @dev Verified against the live factory: topic0
    ///      0x0a44ef75df69c534f43cd6c1aa3ef8983065fe5fe79ef9e79f6494e6f258c259, one indexed
    ///      argument and three words of data. Every token seen carrying it reports phase 2.
    /// @param positionId The locked full-range v4 position minted at graduation.
    /// @param tokenAmount Reserved supply deposited as liquidity (~20.4% of supply).
    /// @param quoteAmount Quote asset deposited alongside it (4.2 ETH for ETH-paired launches).
    event PoolGraduated(
        address indexed token, uint256 positionId, uint256 tokenAmount, uint256 quoteAmount
    );

    function launchToken(TokenParams calldata params, uint256 launchConfigId, address pairToken)
        external
        payable
        returns (address token, address curve);

    function getLaunchedToken(address token) external view returns (LaunchedToken memory);

    /// @notice Protocol-enforced ceiling on `creatorTaxBps`, in basis points.
    function maxCreatorTaxBps() external view returns (uint16);

    function previewLaunchEconomics(uint256 configId, address pairToken) external view returns (bytes32);

    /// @notice Permissionless retry when automatic graduation fails.
    function createGraduatedPool(address token) external;
}

/// @notice Pons fee escrow. The IPO creator tax accrues here and is pulled by the treasury.
interface IPonsFeeEscrow {
    /// @notice Native (ETH) balance credited to `recipient`.
    function balanceOf(address recipient) external view returns (uint256);

    /// @notice ERC-20 balance credited to `recipient` for `token`.
    function balanceOfToken(address recipient, address token) external view returns (uint256);

    /// @notice Withdraw the caller's full native balance.
    function claim() external;

    /// @notice Withdraw the caller's full balance of `token`.
    function claimToken(address token) external;
}
