use std::io::{IsTerminal, Write};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc;
use std::time::{Duration, Instant};

use clap::Parser;

use vanity::address::{to_checksum_string, to_hex_string};
use vanity::pattern::Matcher;
use vanity::search::{self, Hit};

/// Generate EVM vanity addresses.
#[derive(Parser)]
#[command(name = "vanity", version, about, long_about = None)]
struct Args {
    /// Hex digits the address must start with, e.g. `dead` or `0xdead`
    #[arg(short, long)]
    prefix: Option<String>,

    /// Hex digits the address must end with
    #[arg(short, long)]
    suffix: Option<String>,

    /// Hex digits the address must contain anywhere
    #[arg(short, long)]
    contains: Option<String>,

    /// Regex the 40-digit address (no `0x`) must match
    #[arg(long)]
    regex: Option<String>,

    /// Match patterns case-sensitively against the EIP-55 checksum form
    #[arg(long)]
    checksum: bool,

    /// Match the contract address deployed at this nonce instead of the EOA
    #[arg(long, value_name = "N")]
    contract_nonce: Option<u64>,

    /// Stop after this many matches
    #[arg(short = 'n', long, default_value_t = 1)]
    count: usize,

    /// Worker threads [default: all cores]
    #[arg(short = 'j', long)]
    threads: Option<usize>,

    /// Also append each match to this file as JSON, created mode 0600
    #[arg(short, long, value_name = "FILE")]
    out: Option<PathBuf>,

    /// Print one JSON object per match instead of the human-readable form
    #[arg(long)]
    json: bool,

    /// Hide the progress meter
    #[arg(short, long)]
    quiet: bool,

    /// Measure the search rate, print the expected time, and exit
    #[arg(long)]
    estimate: bool,
}

fn main() {
    let args = Args::parse();
    if let Err(message) = run(&args) {
        eprintln!("vanity: {message}");
        std::process::exit(2);
    }
}

fn run(args: &Args) -> Result<(), String> {
    if args.count == 0 {
        return Err("--count must be at least 1".into());
    }
    let matcher = Matcher::new(
        args.prefix.as_deref(),
        args.suffix.as_deref(),
        args.contains.as_deref(),
        args.regex.as_deref(),
        args.checksum,
    )
    .map_err(|e| e.to_string())?;

    let threads = match args.threads {
        Some(0) => return Err("--threads must be at least 1".into()),
        Some(n) => n,
        None => std::thread::available_parallelism().map(|n| n.get()).unwrap_or(1),
    };

    let target = match args.contract_nonce {
        Some(nonce) => format!("a contract address at nonce {nonce}"),
        None => "an address".to_string(),
    };
    eprintln!("looking for {target} that {}", matcher.describe());

    let expected = matcher.expected_attempts();
    match expected {
        Some(n) => eprintln!(
            "1 in {} addresses qualifies (50% odds after {} tries)",
            magnitude(n),
            magnitude(n * std::f64::consts::LN_2)
        ),
        None => eprintln!("difficulty unknown (a regex pattern can't be scored up front)"),
    }

    if args.estimate {
        eprintln!("measuring search rate...");
        let per_thread = search::benchmark(Duration::from_millis(1500), args.contract_nonce);
        let rate = per_thread * threads as f64;
        eprintln!("{} addr/s on {threads} {}", magnitude(rate), plural("thread", threads));
        match expected {
            Some(n) => eprintln!(
                "expect a match in {} (50% odds within {})",
                duration(n / rate),
                duration(n * std::f64::consts::LN_2 / rate)
            ),
            None => eprintln!("no ETA available for a regex pattern"),
        }
        return Ok(());
    }

    eprintln!("searching on {threads} {}", plural("thread", threads));
    eprintln!("keys are printed in the clear — anyone who sees this output owns the address\n");

    let mut sink = match &args.out {
        Some(path) => Some(open_out(path)?),
        None => None,
    };

    let counter = AtomicU64::new(0);
    let stop = AtomicBool::new(false);
    let (tx, rx) = mpsc::channel::<Hit>();
    let started = Instant::now();
    let mut found = 0usize;

    std::thread::scope(|scope| -> Result<(), String> {
        for _ in 0..threads {
            let (matcher, counter, stop, tx) = (&matcher, &counter, &stop, tx.clone());
            scope.spawn(move || {
                search::worker(matcher, args.contract_nonce, counter, stop, &tx);
            });
        }
        drop(tx);

        let mut meter = Meter::new(args.quiet, expected);
        let result = loop {
            match rx.recv_timeout(Duration::from_millis(200)) {
                Ok(hit) => {
                    found += 1;
                    if found >= args.count {
                        stop.store(true, Ordering::Relaxed);
                    }
                    let tries = counter.load(Ordering::Relaxed);
                    meter.clear();
                    if let Err(e) = report(&hit, args, found, tries, started.elapsed(), &mut sink) {
                        stop.store(true, Ordering::Relaxed);
                        break Err(e);
                    }
                    if found >= args.count {
                        break Ok(());
                    }
                }
                Err(mpsc::RecvTimeoutError::Timeout) => {
                    meter.tick(counter.load(Ordering::Relaxed), started.elapsed());
                }
                // Every worker exited, which only happens once stopped.
                Err(mpsc::RecvTimeoutError::Disconnected) => break Ok(()),
            }
        };
        stop.store(true, Ordering::Relaxed);
        result
    })?;

    let tries = counter.load(Ordering::Relaxed);
    let elapsed = started.elapsed().as_secs_f64().max(1e-9);
    eprintln!(
        "\n{found} {} · {} tried in {} · {} addr/s",
        plural("address", found),
        magnitude(tries as f64),
        duration(elapsed),
        magnitude(tries as f64 / elapsed),
    );
    Ok(())
}

fn report(
    hit: &Hit,
    args: &Args,
    index: usize,
    tries: u64,
    elapsed: Duration,
    sink: &mut Option<std::fs::File>,
) -> Result<(), String> {
    let address = to_checksum_string(&hit.address);
    let secret = to_hex_string(&hit.secret);
    let contract = hit.contract.map(|(nonce, addr)| (nonce, to_checksum_string(&addr)));

    let json = || {
        let mut s = format!(
            "{{\"address\":\"{address}\",\"private_key\":\"{secret}\",\"attempts\":{tries},\"seconds\":{:.3}",
            elapsed.as_secs_f64()
        );
        if let Some((nonce, contract)) = &contract {
            s.push_str(&format!(",\"contract\":{{\"nonce\":{nonce},\"address\":\"{contract}\"}}"));
        }
        s.push('}');
        s
    };

    if args.json {
        println!("{}", json());
    } else {
        println!(
            "match {index}/{}  ·  {} tried in {}",
            args.count,
            magnitude(tries as f64),
            duration(elapsed.as_secs_f64())
        );
        println!("  address      {address}");
        println!("  private key  {secret}");
        if let Some((nonce, contract)) = &contract {
            println!("  contract     {contract}  (deployed by this address at nonce {nonce})");
        }
        println!();
    }
    let _ = std::io::stdout().flush();

    if let Some(file) = sink {
        writeln!(file, "{}", json()).map_err(|e| format!("writing results file: {e}"))?;
        file.flush().map_err(|e| format!("writing results file: {e}"))?;
    }
    Ok(())
}

fn open_out(path: &PathBuf) -> Result<std::fs::File, String> {
    let mut options = std::fs::OpenOptions::new();
    options.create(true).append(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    options.open(path).map_err(|e| format!("opening {}: {e}", path.display()))
}

/// The progress meter on stderr: a line redrawn in place on a terminal, an
/// occasional plain line when the output is a pipe or a log file.
struct Meter {
    silent: bool,
    terminal: bool,
    interval: Duration,
    expected: Option<f64>,
    last: Instant,
    dirty: bool,
}

impl Meter {
    fn new(silent: bool, expected: Option<f64>) -> Self {
        let terminal = std::io::stderr().is_terminal();
        let interval = if terminal { Duration::from_millis(500) } else { Duration::from_secs(30) };
        Meter { silent, terminal, interval, expected, last: Instant::now(), dirty: false }
    }

    fn tick(&mut self, tries: u64, elapsed: Duration) {
        if self.silent || self.last.elapsed() < self.interval {
            return;
        }
        self.last = Instant::now();
        let seconds = elapsed.as_secs_f64().max(1e-9);
        let odds = match self.expected {
            // Chance of having hit something by now, given the work done.
            Some(n) => {
                format!(" · {:.0}% odds so far", 100.0 * (1.0 - (-(tries as f64) / n).exp()))
            }
            None => String::new(),
        };
        let line = format!(
            "  {} tried · {} addr/s · {}{}",
            magnitude(tries as f64),
            magnitude(tries as f64 / seconds),
            duration(seconds),
            odds
        );
        if self.terminal {
            eprint!("\r{line}   ");
            self.dirty = true;
        } else {
            eprintln!("{line}");
        }
        let _ = std::io::stderr().flush();
    }

    fn clear(&mut self) {
        if self.dirty {
            eprint!("\r{:80}\r", "");
            let _ = std::io::stderr().flush();
            self.dirty = false;
        }
    }
}

/// Compact magnitudes: 812, 44.1k, 3.20M, 1.44G.
fn magnitude(n: f64) -> String {
    const UNITS: [(f64, &str); 6] =
        [(1e18, "E"), (1e15, "P"), (1e12, "T"), (1e9, "G"), (1e6, "M"), (1e3, "k")];
    for (scale, suffix) in UNITS {
        if n >= scale {
            let value = n / scale;
            return match value {
                v if v < 10.0 => format!("{v:.2}{suffix}"),
                v if v < 100.0 => format!("{v:.1}{suffix}"),
                v => format!("{v:.0}{suffix}"),
            };
        }
    }
    format!("{n:.0}")
}

fn duration(seconds: f64) -> String {
    if !seconds.is_finite() {
        return "forever".to_string();
    }
    let s = seconds.round() as u64;
    match s {
        0 => format!("{seconds:.1}s"),
        1..=59 => format!("{s}s"),
        60..=3599 => format!("{}m {}s", s / 60, s % 60),
        3600..=86_399 => format!("{}h {}m", s / 3600, (s % 3600) / 60),
        _ if s < 86_400 * 365 => format!("{}d {}h", s / 86_400, (s % 86_400) / 3600),
        _ => format!("{:.1} years", seconds / (86_400.0 * 365.0)),
    }
}

fn plural(word: &str, n: usize) -> String {
    if n == 1 {
        word.to_string()
    } else if word.ends_with('s') {
        format!("{word}es")
    } else {
        format!("{word}s")
    }
}
