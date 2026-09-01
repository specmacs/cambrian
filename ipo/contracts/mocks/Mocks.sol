// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IPonsFactory, IPonsFeeEscrow, LaunchedToken, TokenParams} from "../interfaces/IPons.sol";
import {IPoolManager, IUnlockCallback, PoolKey, SwapParams} from "../interfaces/IUniswapV4.sol";

contract MockERC20 is ERC20 {
    constructor(string memory n, string memory s, uint256 supply) ERC20(n, s) {
        _mint(msg.sender, supply);
    }

    function mint(address to, uint256 amount) external {
        _mint(to, amount);
    }
}

contract MockPonsFactory is IPonsFactory {
    mapping(address => LaunchedToken) internal records;
    uint16 public maxTax = 1000;

    function setRecord(address token, LaunchedToken calldata record) external {
        records[token] = record;
    }

    function setPhase(address token, uint8 phase) external {
        records[token].phase = phase;
    }

    function getLaunchedToken(address token) external view returns (LaunchedToken memory) {
        return records[token];
    }

    function maxCreatorTaxBps() external view returns (uint16) {
        return maxTax;
    }

    function setMaxCreatorTaxBps(uint16 v) external {
        maxTax = v;
    }

    function launchToken(TokenParams calldata, uint256, address) external payable returns (address, address) {
        return (address(0), address(0));
    }

    function previewLaunchEconomics(uint256, address) external pure returns (bytes32) {
        return bytes32(0);
    }

    function createGraduatedPool(address) external {}
}

contract MockPonsFeeEscrow is IPonsFeeEscrow {
    mapping(address => uint256) public credited;
    mapping(address => mapping(address => uint256)) public creditedToken;

    /// @notice Credit `recipient` with the ETH sent, as the Pons hook would.
    function credit(address recipient) external payable {
        credited[recipient] += msg.value;
    }

    function creditToken(address recipient, address token, uint256 amount) external {
        IERC20(token).transferFrom(msg.sender, address(this), amount);
        creditedToken[recipient][token] += amount;
    }

    function balanceOf(address recipient) external view returns (uint256) {
        return credited[recipient];
    }

    function balanceOfToken(address recipient, address token) external view returns (uint256) {
        return creditedToken[recipient][token];
    }

    function claim() external {
        uint256 amount = credited[msg.sender];
        credited[msg.sender] = 0;
        (bool ok,) = msg.sender.call{value: amount}("");
        require(ok, "claim failed");
    }

    function claimToken(address token) external {
        uint256 amount = creditedToken[msg.sender][token];
        creditedToken[msg.sender][token] = 0;
        IERC20(token).transfer(msg.sender, amount);
    }
}

/// @notice Minimal v4 pool manager: honours the unlock/swap/settle/take dance at a fixed rate.
contract MockPoolManager is IPoolManager {
    /// @notice Tokens minted per wei of ETH in.
    uint256 public rate = 1000;
    bool public unlocked;

    function setRate(uint256 r) external {
        rate = r;
    }

    function unlock(bytes calldata data) external returns (bytes memory result) {
        unlocked = true;
        result = IUnlockCallback(msg.sender).unlockCallback(data);
        unlocked = false;
    }

    function swap(PoolKey memory key, SwapParams memory params, bytes calldata)
        external
        view
        returns (int256 delta)
    {
        require(unlocked, "locked");
        require(params.amountSpecified < 0, "exact input only");
        uint256 ethIn = uint256(-params.amountSpecified);
        uint256 out = ethIn * rate;
        require(IERC20(key.currency1).balanceOf(address(this)) >= out, "insufficient pool tokens");
        return _toBalanceDelta(-int128(int256(ethIn)), int128(int256(out)));
    }

    function sync(address) external {}

    function settle() external payable returns (uint256) {
        return msg.value;
    }

    function take(address currency, address to, uint256 amount) external {
        require(unlocked, "locked");
        IERC20(currency).transfer(to, amount);
    }

    function _toBalanceDelta(int128 amount0, int128 amount1) internal pure returns (int256 balanceDelta) {
        assembly ("memory-safe") {
            balanceDelta := or(shl(128, amount0), and(sub(shl(128, 1), 1), amount1))
        }
    }

    receive() external payable {}
}
