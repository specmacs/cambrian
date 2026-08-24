//! Known-answer tests: keccak, EOA derivation, EIP-55, CREATE addresses, and
//! the incremental walk the search loop depends on.

use secp256k1::{PublicKey, Scalar, Secp256k1, SecretKey};
use vanity::address::{
    address_from_uncompressed, create_address, eip55, hex_lower, keccak256, to_checksum_string,
};
use vanity::pattern::Matcher;

fn secret_from_u64(n: u64) -> SecretKey {
    let mut bytes = [0u8; 32];
    bytes[24..].copy_from_slice(&n.to_be_bytes());
    SecretKey::from_byte_array(bytes).expect("valid key")
}

fn address_of(secret: &SecretKey) -> [u8; 20] {
    let secp = Secp256k1::new();
    let pubkey = PublicKey::from_secret_key(&secp, secret);
    address_from_uncompressed(&pubkey.serialize_uncompressed())
}

fn parse_address(s: &str) -> [u8; 20] {
    let hex = s.trim_start_matches("0x");
    let mut out = [0u8; 20];
    for (i, byte) in out.iter_mut().enumerate() {
        *byte = u8::from_str_radix(&hex[2 * i..2 * i + 2], 16).expect("hex");
    }
    out
}

#[test]
fn keccak_matches_reference_vectors() {
    assert_eq!(
        hex::encode(keccak256(b"")),
        "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    );
    assert_eq!(
        hex::encode(keccak256(b"abc")),
        "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"
    );
}

#[test]
fn addresses_match_known_private_keys() {
    assert_eq!(
        to_checksum_string(&address_of(&secret_from_u64(1))),
        "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf"
    );
    assert_eq!(
        to_checksum_string(&address_of(&secret_from_u64(2))),
        "0x2B5AD5c4795c026514f8317c7a215E218DcCD6cF"
    );
    assert_eq!(
        to_checksum_string(&address_of(&secret_from_u64(3))),
        "0x6813Eb9362372EEF6200f3b1dbC3f819671cBA69"
    );
}

#[test]
fn checksums_match_eip55_vectors() {
    for expected in [
        "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed",
        "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359",
        "0xdbF03B407c01E7cD3CBea99509d93f8DDDC8C6FB",
        "0xD1220A0cf47c7B9Be7A2E6BA89F429762e7b9aDb",
    ] {
        assert_eq!(to_checksum_string(&parse_address(expected)), expected);
    }
}

#[test]
fn create_addresses_match_known_deployments() {
    // The canonical vectors for keccak256(rlp([sender, nonce]))[12..].
    let sender = parse_address("0x6ac7ea33f8831ea9dcc53393aaa88b25a785dbf0");
    for (nonce, expected) in [
        (0u64, "0xcd234a471b72ba2f1ccf0a70fcaba648a5eecd8d"),
        (1, "0x343c43a37d37dff08ae8c4a11544c718abb4fcf8"),
        (2, "0xf778b86fa74e846c4f0a1fbd1335fe81c00a0c91"),
        (3, "0xfffd933a0bc612844eaf0c6fe3e5b8e9b6c1d19c"),
    ] {
        assert_eq!(create_address(&sender, nonce), parse_address(expected), "nonce {nonce}");
    }
}

#[test]
fn create_address_handles_multi_byte_nonces() {
    // Nonce encodings the RLP branches have to get right: single byte, the
    // 0x80 boundary, and multi-byte lengths.
    let sender = parse_address("0x6ac7ea33f8831ea9dcc53393aaa88b25a785dbf0");
    let mut seen = std::collections::HashSet::new();
    for nonce in [0u64, 1, 0x7f, 0x80, 0xff, 0x100, u32::MAX as u64, u64::MAX] {
        assert!(seen.insert(create_address(&sender, nonce)), "collision at nonce {nonce}");
    }
}

#[test]
fn walking_the_curve_agrees_with_direct_derivation() {
    // The search loop adds G to the public key and reconstructs the secret as
    // `start + steps`. Both sides have to land on the same address.
    let secp = Secp256k1::new();
    let start = secret_from_u64(0x1234_5678_9abc_def0);
    let generator = PublicKey::from_secret_key(&secp, &secret_from_u64(1));
    let mut walked = PublicKey::from_secret_key(&secp, &start);

    for step in 0..64u64 {
        let mut tweak = [0u8; 32];
        tweak[24..].copy_from_slice(&step.to_be_bytes());
        let direct = start.add_tweak(&Scalar::from_be_bytes(tweak).unwrap()).unwrap();
        assert_eq!(
            address_from_uncompressed(&walked.serialize_uncompressed()),
            address_of(&direct),
            "step {step}"
        );
        walked = walked.combine(&generator).unwrap();
    }
}

#[test]
fn hex_and_checksum_forms_agree() {
    let address = parse_address("0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed");
    let lower = hex_lower(&address);
    assert_eq!(std::str::from_utf8(&lower).unwrap(), "5aaeb6053f3e94c9b9a09f33669435e7ef1beaed");
    assert_eq!(
        std::str::from_utf8(&eip55(&lower)).unwrap(),
        "5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
    );
}

#[test]
fn matcher_honours_prefix_suffix_and_contains() {
    let address = parse_address("0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed");
    let lower = hex_lower(&address);

    assert!(Matcher::new(Some("5aae"), None, None, None, false).unwrap().matches(&lower));
    assert!(Matcher::new(Some("0x5AAE"), None, None, None, false).unwrap().matches(&lower));
    assert!(!Matcher::new(Some("5aaf"), None, None, None, false).unwrap().matches(&lower));
    assert!(Matcher::new(None, Some("beaed"), None, None, false).unwrap().matches(&lower));
    assert!(!Matcher::new(None, Some("beaee"), None, None, false).unwrap().matches(&lower));
    assert!(Matcher::new(None, None, Some("94c9b9"), None, false).unwrap().matches(&lower));
    assert!(!Matcher::new(None, None, Some("94c9b8"), None, false).unwrap().matches(&lower));
    assert!(Matcher::new(Some("5aae"), Some("aed"), Some("3669"), None, false)
        .unwrap()
        .matches(&lower));
    assert!(!Matcher::new(Some("5aae"), Some("aec"), None, None, false).unwrap().matches(&lower));
}

#[test]
fn checksum_mode_is_case_sensitive() {
    // 0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed
    let lower = hex_lower(&parse_address("0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed"));

    assert!(Matcher::new(Some("5aAe"), None, None, None, true).unwrap().matches(&lower));
    assert!(!Matcher::new(Some("5aae"), None, None, None, true).unwrap().matches(&lower));
    assert!(Matcher::new(None, Some("BeAed"), None, None, true).unwrap().matches(&lower));
    assert!(!Matcher::new(None, Some("beaed"), None, None, true).unwrap().matches(&lower));
    // Case-insensitive mode still accepts a mixed-case pattern.
    assert!(Matcher::new(Some("5aAe"), None, None, None, false).unwrap().matches(&lower));
}

#[test]
fn regex_matches_against_the_bare_address() {
    let lower = hex_lower(&parse_address("0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed"));
    assert!(Matcher::new(None, None, None, Some("^5aae"), false).unwrap().matches(&lower));
    assert!(Matcher::new(None, None, None, Some("aed$"), false).unwrap().matches(&lower));
    assert!(!Matcher::new(None, None, None, Some("^dead"), false).unwrap().matches(&lower));
    // In checksum mode the regex sees the mixed-case form.
    assert!(Matcher::new(None, None, None, Some("^5aAe"), true).unwrap().matches(&lower));
    assert!(!Matcher::new(None, None, None, Some("^5aae"), true).unwrap().matches(&lower));
}

#[test]
fn difficulty_accounts_for_pattern_length_and_case() {
    let attempts = |m: Matcher| m.expected_attempts().unwrap();

    assert_eq!(attempts(Matcher::new(Some("dead"), None, None, None, false).unwrap()), 65536.0);
    assert_eq!(attempts(Matcher::new(None, Some("beef"), None, None, false).unwrap()), 65536.0);
    // Four letters, so four extra coin flips for the checksum case.
    assert_eq!(
        attempts(Matcher::new(Some("dEaD"), None, None, None, true).unwrap()),
        65536.0 * 16.0
    );
    // Digits carry no case, so checksum mode costs nothing extra.
    assert_eq!(attempts(Matcher::new(Some("1234"), None, None, None, true).unwrap()), 65536.0);
    // Constraints multiply.
    assert_eq!(
        attempts(Matcher::new(Some("00"), Some("00"), None, None, false).unwrap()),
        256.0 * 256.0
    );
    // A floating pattern is easier than an anchored one.
    assert!(
        attempts(Matcher::new(None, None, Some("dead"), None, false).unwrap())
            < attempts(Matcher::new(Some("dead"), None, None, None, false).unwrap())
    );
    assert!(Matcher::new(None, None, None, Some("^dead"), false)
        .unwrap()
        .expected_attempts()
        .is_none());
}

#[test]
fn invalid_patterns_are_rejected() {
    assert!(Matcher::new(None, None, None, None, false).is_err());
    assert!(Matcher::new(Some("zz"), None, None, None, false).is_err());
    assert!(Matcher::new(Some(""), None, None, None, false).is_err());
    assert!(Matcher::new(None, Some("0xbeef"), None, None, false).is_err());
    assert!(Matcher::new(Some(&"a".repeat(41)), None, None, None, false).is_err());
    assert!(Matcher::new(Some(&"a".repeat(30)), Some(&"b".repeat(20)), None, None, false).is_err());
    assert!(Matcher::new(None, None, None, Some("("), false).is_err());
}

#[test]
fn search_returns_keys_that_derive_the_matched_address() {
    use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
    use std::sync::mpsc;
    use std::time::Duration;

    // 1 in 256, so a hit lands in milliseconds.
    let matcher = Matcher::new(Some("ab"), None, None, None, false).unwrap();
    let counter = AtomicU64::new(0);
    let stop = AtomicBool::new(false);
    let (tx, rx) = mpsc::channel();

    std::thread::scope(|scope| {
        scope.spawn(|| vanity::search::worker(&matcher, None, &counter, &stop, &tx));
        for _ in 0..4 {
            let hit = rx.recv_timeout(Duration::from_secs(60)).expect("a match");
            let secret = SecretKey::from_byte_array(hit.secret).expect("valid key");
            assert_eq!(address_of(&secret), hit.address, "key does not derive its address");
            assert!(matcher.matches(&hex_lower(&hit.address)), "reported a non-matching address");
            assert!(hit.contract.is_none());
        }
        stop.store(true, Ordering::Relaxed);
    });
    assert!(counter.load(Ordering::Relaxed) > 0);
}

#[test]
fn contract_mode_matches_the_deployed_address() {
    use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
    use std::sync::mpsc;
    use std::time::Duration;

    let matcher = Matcher::new(Some("ab"), None, None, None, false).unwrap();
    let counter = AtomicU64::new(0);
    let stop = AtomicBool::new(false);
    let (tx, rx) = mpsc::channel();

    std::thread::scope(|scope| {
        scope.spawn(|| vanity::search::worker(&matcher, Some(7), &counter, &stop, &tx));
        let hit = rx.recv_timeout(Duration::from_secs(60)).expect("a match");
        stop.store(true, Ordering::Relaxed);

        let secret = SecretKey::from_byte_array(hit.secret).expect("valid key");
        assert_eq!(address_of(&secret), hit.address);
        let (nonce, contract) = hit.contract.expect("contract address");
        assert_eq!(nonce, 7);
        assert_eq!(contract, create_address(&hit.address, 7));
        assert!(matcher.matches(&hex_lower(&contract)));
        // The EOA itself is not what was constrained.
        assert_ne!(contract, hit.address);
    });
}
