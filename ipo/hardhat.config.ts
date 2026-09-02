import type { HardhatUserConfig } from "hardhat/config";
import "@nomicfoundation/hardhat-toolbox";

const config: HardhatUserConfig = {
  solidity: {
    version: "0.8.28",
    settings: {
      optimizer: { enabled: true, runs: 800 },
      // Robinhood Chain is an Arbitrum Orbit L2; Uniswap v4 requires transient storage.
      evmVersion: "cancun",
    },
  },
  networks: {
    hardhat: {
      // Set FORK_BLOCK to pin a block; forking is opt-in so the unit suite stays offline.
      forking: process.env.FORK_BLOCK
        ? {
            url: process.env.RPC_URL ?? "https://rpc.mainnet.chain.robinhood.com",
            blockNumber: Number(process.env.FORK_BLOCK),
          }
        : undefined,
    },
    robinhood: {
      url: process.env.RPC_URL ?? "https://rpc.mainnet.chain.robinhood.com",
      chainId: 4663,
      accounts: process.env.PRIVATE_KEY ? [process.env.PRIVATE_KEY] : [],
    },
  },
};

export default config;
