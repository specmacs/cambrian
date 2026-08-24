//! Address derivation: keccak-256, EOA addresses, EIP-55 checksums, CREATE addresses.

use tiny_keccak::{Hasher, Keccak};

const HEX: &[u8; 16] = b"0123456789abcdef";

pub fn keccak256(data: &[u8]) -> [u8; 32] {
    let mut out = [0u8; 32];
    let mut hasher = Keccak::v256();
    hasher.update(data);
    hasher.finalize(&mut out);
    out
}

/// EOA address for a 65-byte uncompressed SEC1 public key (`0x04 || X || Y`):
/// the low 20 bytes of `keccak256(X || Y)`.
pub fn address_from_uncompressed(pubkey: &[u8; 65]) -> [u8; 20] {
    debug_assert_eq!(pubkey[0], 0x04);
    let hash = keccak256(&pubkey[1..]);
    let mut addr = [0u8; 20];
    addr.copy_from_slice(&hash[12..]);
    addr
}

/// Lowercase hex of an address, without the `0x` prefix.
pub fn hex_lower(addr: &[u8; 20]) -> [u8; 40] {
    let mut out = [0u8; 40];
    for (i, byte) in addr.iter().enumerate() {
        out[2 * i] = HEX[(byte >> 4) as usize];
        out[2 * i + 1] = HEX[(byte & 0x0f) as usize];
    }
    out
}

/// EIP-55 mixed-case checksum form, computed from the lowercase hex digits.
pub fn eip55(lower: &[u8; 40]) -> [u8; 40] {
    let hash = keccak256(lower);
    let mut out = *lower;
    for (i, c) in out.iter_mut().enumerate() {
        let nibble = if i % 2 == 0 { hash[i / 2] >> 4 } else { hash[i / 2] & 0x0f };
        if nibble >= 8 {
            c.make_ascii_uppercase();
        }
    }
    out
}

/// Address of the contract `sender` deploys with CREATE at `nonce`:
/// the low 20 bytes of `keccak256(rlp([sender, nonce]))`.
pub fn create_address(sender: &[u8; 20], nonce: u64) -> [u8; 20] {
    let hash = keccak256(&rlp_sender_nonce(sender, nonce));
    let mut addr = [0u8; 20];
    addr.copy_from_slice(&hash[12..]);
    addr
}

fn rlp_sender_nonce(sender: &[u8; 20], nonce: u64) -> Vec<u8> {
    let mut payload = Vec::with_capacity(30);
    payload.push(0x80 + 20);
    payload.extend_from_slice(sender);
    match nonce {
        0 => payload.push(0x80),
        n if n <= 0x7f => payload.push(n as u8),
        n => {
            let be = n.to_be_bytes();
            let first = be.iter().position(|&b| b != 0).expect("nonce != 0");
            payload.push(0x80 + (be.len() - first) as u8);
            payload.extend_from_slice(&be[first..]);
        }
    }
    // The payload is at most 30 bytes, so the list header is always a single byte.
    let mut out = Vec::with_capacity(payload.len() + 1);
    out.push(0xc0 + payload.len() as u8);
    out.extend_from_slice(&payload);
    out
}

/// `0x`-prefixed EIP-55 string, the form to paste anywhere.
pub fn to_checksum_string(addr: &[u8; 20]) -> String {
    let cs = eip55(&hex_lower(addr));
    let mut s = String::with_capacity(42);
    s.push_str("0x");
    s.push_str(std::str::from_utf8(&cs).expect("hex is ascii"));
    s
}

pub fn to_hex_string(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(2 + bytes.len() * 2);
    s.push_str("0x");
    for b in bytes {
        s.push(HEX[(b >> 4) as usize] as char);
        s.push(HEX[(b & 0x0f) as usize] as char);
    }
    s
}
