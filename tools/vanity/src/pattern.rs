//! Pattern matching against a candidate address, plus difficulty estimates.

use crate::address::eip55;
use regex::Regex;

/// What an address has to look like to count as a hit.
///
/// In checksum mode the hex patterns are matched case-sensitively against the
/// EIP-55 form; otherwise everything is compared lowercase. Checksum matching
/// always runs a lowercase pre-filter first so the second keccak is only paid
/// for candidates that already match case-insensitively.
pub struct Matcher {
    prefix: Option<Pattern>,
    suffix: Option<Pattern>,
    contains: Option<Pattern>,
    regex: Option<Regex>,
    checksum: bool,
}

struct Pattern {
    lower: Vec<u8>,
    /// Case-sensitive form, only used in checksum mode.
    exact: Vec<u8>,
}

impl Pattern {
    fn new(raw: &str) -> Self {
        Pattern { lower: raw.to_ascii_lowercase().into_bytes(), exact: raw.as_bytes().to_vec() }
    }

    fn cost(&self, checksum: bool) -> f64 {
        let mut cost = 16f64.powi(self.lower.len() as i32);
        if checksum {
            // Every letter in the pattern also has to land on the right case.
            let letters = self.exact.iter().filter(|c| c.is_ascii_alphabetic()).count();
            cost *= 2f64.powi(letters as i32);
        }
        cost
    }
}

#[derive(Debug)]
pub struct MatcherError(pub String);

impl std::fmt::Display for MatcherError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for MatcherError {}

fn validate(kind: &str, raw: &str) -> Result<(), MatcherError> {
    if raw.is_empty() {
        return Err(MatcherError(format!("--{kind} is empty")));
    }
    if raw.len() > 40 {
        return Err(MatcherError(format!("--{kind} is longer than an address (40 hex digits)")));
    }
    if let Some(bad) = raw.chars().find(|c| !c.is_ascii_hexdigit()) {
        return Err(MatcherError(format!("--{kind} contains a non-hex character: {bad:?}")));
    }
    Ok(())
}

/// Accept patterns written either as `dead` or `0xdead`.
fn strip_0x(raw: &str) -> &str {
    raw.strip_prefix("0x").or_else(|| raw.strip_prefix("0X")).unwrap_or(raw)
}

impl Matcher {
    pub fn new(
        prefix: Option<&str>,
        suffix: Option<&str>,
        contains: Option<&str>,
        regex: Option<&str>,
        checksum: bool,
    ) -> Result<Self, MatcherError> {
        let prefix = prefix.map(strip_0x);
        if let Some(p) = prefix {
            validate("prefix", p)?;
        }
        if let Some(s) = suffix {
            validate("suffix", s)?;
        }
        if let Some(c) = contains {
            validate("contains", c)?;
        }
        let total: usize = [prefix, suffix, contains].iter().flatten().map(|p| p.len()).sum();
        if total > 40 {
            return Err(MatcherError(
                "the patterns together are longer than an address (40 hex digits)".into(),
            ));
        }
        let regex = match regex {
            Some(r) => Some(Regex::new(r).map_err(|e| MatcherError(format!("--regex: {e}")))?),
            None => None,
        };
        if prefix.is_none() && suffix.is_none() && contains.is_none() && regex.is_none() {
            return Err(MatcherError(
                "no pattern given: use --prefix, --suffix, --contains or --regex".into(),
            ));
        }
        Ok(Matcher {
            prefix: prefix.map(Pattern::new),
            suffix: suffix.map(Pattern::new),
            contains: contains.map(Pattern::new),
            regex,
            checksum,
        })
    }

    /// True if `lower` (40 lowercase hex digits, no `0x`) is a hit.
    pub fn matches(&self, lower: &[u8; 40]) -> bool {
        if let Some(p) = &self.prefix {
            if !lower.starts_with(&p.lower) {
                return false;
            }
        }
        if let Some(s) = &self.suffix {
            if !lower.ends_with(&s.lower) {
                return false;
            }
        }
        if let Some(c) = &self.contains {
            if !windows_contain(lower, &c.lower) {
                return false;
            }
        }
        if !self.checksum {
            return match &self.regex {
                Some(re) => re.is_match(as_str(lower)),
                None => true,
            };
        }

        let cs = eip55(lower);
        if let Some(p) = &self.prefix {
            if !cs.starts_with(&p.exact) {
                return false;
            }
        }
        if let Some(s) = &self.suffix {
            if !cs.ends_with(&s.exact) {
                return false;
            }
        }
        if let Some(c) = &self.contains {
            if !windows_contain(&cs, &c.exact) {
                return false;
            }
        }
        match &self.regex {
            Some(re) => re.is_match(as_str(&cs)),
            None => true,
        }
    }

    /// Mean number of addresses per hit, or `None` when a regex makes it
    /// unknowable.
    pub fn expected_attempts(&self) -> Option<f64> {
        if self.regex.is_some() {
            return None;
        }
        let mut cost = 1.0;
        if let Some(p) = &self.prefix {
            cost *= p.cost(self.checksum);
        }
        if let Some(s) = &self.suffix {
            cost *= s.cost(self.checksum);
        }
        if let Some(c) = &self.contains {
            // A short pattern has (41 - len) places it could sit in the address.
            let positions = (41 - c.lower.len()) as f64;
            cost *= c.cost(self.checksum) / positions;
        }
        Some(cost.max(1.0))
    }

    pub fn describe(&self) -> String {
        let mut parts = Vec::new();
        if let Some(p) = &self.prefix {
            parts.push(format!("starts with {}", as_str(&p.exact)));
        }
        if let Some(s) = &self.suffix {
            parts.push(format!("ends with {}", as_str(&s.exact)));
        }
        if let Some(c) = &self.contains {
            parts.push(format!("contains {}", as_str(&c.exact)));
        }
        if let Some(re) = &self.regex {
            parts.push(format!("matches /{}/", re.as_str()));
        }
        if self.checksum {
            parts.push("case-sensitive (EIP-55)".to_string());
        }
        parts.join(", ")
    }
}

fn as_str(bytes: &[u8]) -> &str {
    std::str::from_utf8(bytes).expect("patterns and hex are ascii")
}

fn windows_contain(haystack: &[u8; 40], needle: &[u8]) -> bool {
    haystack.windows(needle.len()).any(|w| w == needle)
}
