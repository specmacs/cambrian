# vanity

Multi-threaded EVM vanity address generator. Give it a pattern, it hands back
matching addresses and their private keys.

```
cargo build --release
./target/release/vanity --prefix c0ffee
```

```
looking for an address that starts with c0ffee
1 in 16.8M addresses qualifies (50% odds after 11.6M tries)
searching on 4 threads
keys are printed in the clear — anyone who sees this output owns the address

match 1/1  ·  4.93M tried in 5s
  address      0xC0FfeE397c050C25aeBB1a20F84674883aD68772
  private key  0xad4aa5d8adb15354e4a6cf9c0d052edfdff576a11a3ad07a4bd03dd25c6616a9
```

## Patterns

| flag | meaning |
| --- | --- |
| `--prefix <hex>` | address starts with these digits (`dead` or `0xdead`) |
| `--suffix <hex>` | address ends with these digits |
| `--contains <hex>` | digits appear anywhere in the address |
| `--regex <re>` | regex over the 40 digits, no `0x` (no backreferences) |
| `--checksum` | match the patterns case-sensitively against the EIP-55 form |

Combine them freely — `--prefix dead --suffix beef` requires both. Without
`--checksum`, patterns are case-insensitive and only the digits matter; with it,
`--prefix dEaD` demands that exact capitalisation in the checksummed address,
which costs an extra factor of 2 per letter.

## Other flags

| flag | meaning |
| --- | --- |
| `-n, --count <N>` | stop after N matches (default 1) |
| `-j, --threads <N>` | worker threads (default: all cores) |
| `--contract-nonce <N>` | match the contract this address would deploy at nonce N, not the address itself |
| `-o, --out <FILE>` | append each match to a file as JSON, created mode 0600 |
| `--json` | print one JSON object per match on stdout |
| `--estimate` | measure the search rate, print the ETA, and exit |
| `-q, --quiet` | no progress meter |

## How long it takes

Each extra hex digit is 16× the work. On a 4-core container this build does
~935k addresses/s; run `--estimate` for your own machine's number.

| prefix | 1 in | at ~1M addr/s |
| --- | --- | --- |
| 4 digits | 65.5k | instant |
| 5 digits | 1.05M | ~1s |
| 6 digits | 16.8M | ~18s |
| 7 digits | 268M | ~5 min |
| 8 digits | 4.29G | ~1.3 hours |
| 9 digits | 68.7G | ~20 hours |
| 10 digits | 1.10T | ~2 weeks |

These are means for a memoryless process: there is no progress toward the next
hit, so the 50% mark comes at 0.69× the expected tries and a run can take much
longer than the table says.

## Handling the keys

- **Generate on the machine that will hold the key.** A key generated over SSH,
  in CI, or in a cloud sandbox has been through someone else's memory and
  terminal scrollback. Treat any such key as public.
- `-o keys.json` writes mode 0600, but the key is still plaintext on disk. Import
  it into a wallet and delete the file, or accept that the file *is* the wallet.
- Verify before funding — the address and key are independent facts:
  `cast wallet address --private-key 0x…` should print the address you were
  given.
- Nothing here is a brain wallet: every search starts from a fresh
  `getrandom`-seeded key, so found keys are as random as the OS entropy behind
  them. Within one run keys are found by walking from a random start, so a run
  that returns several keys re-seeds after each hit rather than handing out a
  chain of related keys.

## How it works

`address = keccak256(pubkey.x ‖ pubkey.y)[12..]`, so a candidate costs one
public key and one keccak. Deriving each public key from scratch would mean a
full scalar multiplication; instead each thread picks one random secret key and
then repeatedly adds the generator to the *public* key, which is a single point
addition per candidate — consecutive public keys correspond to consecutive
secret keys. The secret is only reconstructed, as `start + steps`, once an
address matches (`src/search.rs`).

Checksum matching runs the cheap lowercase comparison first and only pays for
the second keccak on candidates that already match case-insensitively.

`--contract-nonce N` matches `keccak256(rlp([address, N]))[12..]` instead, for
mining an EOA whose *deployment* has the vanity address.

## Tests

```
cargo test
```

Known-answer tests cover keccak-256, address derivation from known private keys,
the EIP-55 vectors, the canonical CREATE-address vectors, and the equivalence of
the incremental curve walk with direct derivation.
