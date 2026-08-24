//! The search loop.
//!
//! Deriving a public key from scratch means a full scalar multiplication. Since
//! consecutive secret keys differ by one, a worker instead picks one random
//! starting key and then walks the curve by repeatedly adding the generator to
//! the public key — a single point addition per candidate. The matching secret
//! key is only reconstructed (`start + i`) once an address hits.

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::Sender;

use secp256k1::{PublicKey, Scalar, Secp256k1, SecretKey};

use crate::address::{address_from_uncompressed, create_address, hex_lower};
use crate::pattern::Matcher;

/// How often a worker publishes its progress and checks whether to stop.
const FLUSH_INTERVAL: u64 = 4096;

/// Keys walked from one random start before re-seeding. Bounded so a run that
/// yields several hits does not hand out a long chain of related keys.
const WALK_LEN: u64 = 1 << 24;

#[derive(Clone, Copy)]
pub struct Hit {
    pub secret: [u8; 32],
    pub address: [u8; 20],
    /// The CREATE address that was matched, with its nonce, when searching for
    /// a contract address rather than the EOA itself.
    pub contract: Option<(u64, [u8; 20])>,
}

/// Runs until `stop` is set, sending every match to `hits`.
pub fn worker(
    matcher: &Matcher,
    contract_nonce: Option<u64>,
    counter: &AtomicU64,
    stop: &AtomicBool,
    hits: &Sender<Hit>,
) {
    let secp = Secp256k1::new();
    let mut rng = rand::rng();
    let generator = PublicKey::from_secret_key(&secp, &one());
    let mut local: u64 = 0;

    'walk: loop {
        if stop.load(Ordering::Relaxed) {
            break;
        }
        let (start_secret, mut pubkey) = secp.generate_keypair(&mut rng);

        for step in 0..WALK_LEN {
            let address = address_from_uncompressed(&pubkey.serialize_uncompressed());
            let candidate = match contract_nonce {
                Some(nonce) => create_address(&address, nonce),
                None => address,
            };

            if matcher.matches(&hex_lower(&candidate)) {
                let secret = match offset_secret(&start_secret, step) {
                    Some(secret) => secret,
                    // Wrapped past the curve order: astronomically unlikely, and
                    // re-seeding costs nothing.
                    None => continue 'walk,
                };
                let hit = Hit {
                    secret: secret.secret_bytes(),
                    address,
                    contract: contract_nonce.map(|nonce| (nonce, candidate)),
                };
                counter.fetch_add(local + 1, Ordering::Relaxed);
                local = 0;
                if hits.send(hit).is_err() || stop.load(Ordering::Relaxed) {
                    return;
                }
                // Keys from one walk are offsets of a shared start, so start
                // over rather than emit a second key related to the first.
                continue 'walk;
            }

            local += 1;
            if local >= FLUSH_INTERVAL {
                counter.fetch_add(local, Ordering::Relaxed);
                local = 0;
                if stop.load(Ordering::Relaxed) {
                    return;
                }
            }

            pubkey = match pubkey.combine(&generator) {
                Ok(next) => next,
                Err(_) => continue 'walk,
            };
        }
    }
    counter.fetch_add(local, Ordering::Relaxed);
}

/// The secret key `start + steps`, matching `steps` additions of the generator.
fn offset_secret(start: &SecretKey, steps: u64) -> Option<SecretKey> {
    if steps == 0 {
        return Some(*start);
    }
    let mut tweak = [0u8; 32];
    tweak[24..].copy_from_slice(&steps.to_be_bytes());
    let tweak = Scalar::from_be_bytes(tweak).ok()?;
    start.add_tweak(&tweak).ok()
}

fn one() -> SecretKey {
    let mut bytes = [0u8; 32];
    bytes[31] = 1;
    SecretKey::from_byte_array(bytes).expect("1 is a valid secret key")
}

/// Addresses per second this machine sustains, measured over `duration`.
pub fn benchmark(duration: std::time::Duration, contract_nonce: Option<u64>) -> f64 {
    let secp = Secp256k1::new();
    let mut rng = rand::rng();
    let generator = PublicKey::from_secret_key(&secp, &one());
    let (_, mut pubkey) = secp.generate_keypair(&mut rng);

    let start = std::time::Instant::now();
    let mut count: u64 = 0;
    loop {
        for _ in 0..FLUSH_INTERVAL {
            let address = address_from_uncompressed(&pubkey.serialize_uncompressed());
            let candidate = match contract_nonce {
                Some(nonce) => create_address(&address, nonce),
                None => address,
            };
            std::hint::black_box(hex_lower(&candidate));
            pubkey = pubkey.combine(&generator).expect("generator addition");
        }
        count += FLUSH_INTERVAL;
        if start.elapsed() >= duration {
            break;
        }
    }
    count as f64 / start.elapsed().as_secs_f64()
}
