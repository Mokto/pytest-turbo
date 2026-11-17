mod config;
mod schedule;

use std::collections::{BTreeMap, HashMap, HashSet, VecDeque};
use std::fs::{self, File};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, ExitCode, Stdio};
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant};

use clap::Parser;
use console::style;
use indicatif::{ProgressBar, ProgressStyle};
use serde_json::{Value, json};

use schedule::{Durations, Task};

const WORKER_PY: &str = include_str!("worker.py");
const MAX_RESTARTS: usize = 4;

/// Run pytest test files in parallel, slowest first, in long-lived pytest workers.
#[derive(Parser)]
#[command(name = "pytest-turbo")]
struct Args {
    /// Worker processes (default: CPU count)
    #[arg(short = 'n', long = "workers")]
    workers: Option<usize>,
    /// Stop after the first failure
    #[arg(short = 'x', long = "exitfirst")]
    exitfirst: bool,
    /// Stop after this many failures
    #[arg(long)]
    maxfail: Option<usize>,
    /// Never split a slow file across workers (each chunk repeats module import and class setup)
    #[arg(long)]
    no_split: bool,
    /// Where test durations are kept between runs (default: .pytest-turbo/durations.json in the rootdir)
    #[arg(long)]
    durations_file: Option<PathBuf>,
    /// Python interpreter (default: $VIRTUAL_ENV, the nearest .venv, the env this binary is installed in, python3)
    #[arg(long)]
    python: Option<PathBuf>,
    /// Files or directories (default: testpaths from the pytest config, else ".")
    paths: Vec<PathBuf>,
    /// Extra arguments for each pytest worker, after --
    #[arg(last = true)]
    pytest_args: Vec<String>,
}

enum Event {
    Line(usize, String),
    Eof(usize),
}

struct Worker {
    child: Child,
    stdin: ChildStdin,
    inflight: Vec<Task>,
    exited: bool,
    /// Told there is no more work: it will not ask again.
    closed: bool,
    /// The test it is running: node id, index in its file, and where its output starts in the log.
    running: Option<(String, usize, u64)>,
}

struct TestResult {
    outcome: String,
    duration: f64,
    reruns: u64,
    subtests: BTreeMap<String, u64>,
    failures: Vec<Value>,
}

fn main() -> ExitCode {
    let args = Args::parse();
    let start = Instant::now();
    let cwd = std::env::current_dir().expect("cwd");
    let mut cfg = config::load(&cwd);
    cfg.add_ignores(&args.pytest_args, &cwd);
    let roots = if !args.paths.is_empty() {
        args.paths.clone()
    } else if cfg.rootdir == cwd && !cfg.testpaths.is_empty() {
        cfg.testpaths.iter().map(PathBuf::from).collect()
    } else {
        vec![PathBuf::from(".")]
    };
    let files = config::discover(&roots, &cfg);
    let state = cfg.rootdir.join(".pytest-turbo");
    let logs = state.join("logs");
    fs::create_dir_all(&logs).expect("create .pytest-turbo/logs");
    let _ = fs::write(state.join(".gitignore"), "*\n");
    let durations_file = args.durations_file.clone().unwrap_or_else(|| state.join("durations.json"));
    let known = load_durations(&durations_file, &cfg.rootdir);
    let n = args.workers.unwrap_or_else(|| thread::available_parallelism().map_or(4, |n| n.get()));
    let tasks = schedule::plan(&files, &known, if args.no_split { 0 } else { n });
    let chunks_per_file: HashMap<String, usize> = tasks.iter().map(|t| (t.path.clone(), t.cuts.len() + 1)).collect();
    let n = n.min(tasks.len()).max(1);
    let discovery = start.elapsed();
    let python = args.python.clone().unwrap_or_else(|| find_python(&cwd));
    let maxfail = if args.exitfirst { Some(1) } else { args.maxfail };

    let expected: usize = files.iter().filter_map(|(f, _)| known.get(f).map(Vec::len)).sum();
    // With -s the workers write to this terminal, which a redrawn progress bar would garble.
    let show_output = args.pytest_args.iter().any(|a| a == "-s" || a == "--capture=no");
    let pb = if show_output {
        ProgressBar::hidden()
    } else if expected > 0 {
        ProgressBar::new(expected as u64)
    } else {
        ProgressBar::new_spinner()
    };
    pb.set_style(ProgressStyle::with_template("{spinner} [{elapsed}] {bar:30} {pos}/{len} {msg}").unwrap());
    pb.enable_steady_tick(Duration::from_millis(100));
    let say = |s: String| if pb.is_hidden() { println!("{s}") } else { pb.println(s) };

    let (tx, rx) = mpsc::channel();
    let mut workers: Vec<Worker> = Vec::new();
    let start_worker = |workers: &mut Vec<Worker>| workers.push(spawn(&python, &args.pytest_args, &logs, workers.len(), n, show_output, &tx));
    for _ in 0..n {
        start_worker(&mut workers);
    }

    let mut queue: VecDeque<Task> = tasks.into();
    let mut results: BTreeMap<String, TestResult> = BTreeMap::new();
    let mut durations: HashMap<String, Vec<f64>> = HashMap::new();
    let mut chunks_done: HashMap<String, usize> = HashMap::new();
    let mut errors: Vec<(String, String)> = Vec::new();
    let mut retried: HashSet<(String, usize, Option<String>)> = HashSet::new();
    let mut reruns: HashMap<String, u64> = HashMap::new();
    let mut awaiting_rerun: HashSet<String> = HashSet::new();
    let mut failed = 0;
    let mut restarts = 0;
    let mut stopping = false;
    let mut alive = n;

    while alive > 0 {
        let Ok(event) = rx.recv() else { break };
        match event {
            Event::Line(wid, line) => {
                let Ok(msg) = serde_json::from_str::<Value>(&line) else {
                    say(format!("pytest-turbo: unreadable worker output: {line}"));
                    continue;
                };
                let w = &mut workers[wid];
                match msg["type"].as_str().unwrap_or("") {
                    "next" => {
                        let task = if stopping { None } else { queue.pop_front() };
                        let line = task.as_ref().map_or_else(|| "\n".to_string(), Task::line);
                        match task {
                            Some(t) => w.inflight.push(t),
                            None => w.closed = true,
                        }
                        let _ = w.stdin.write_all(line.as_bytes());
                    }
                    "test" => {
                        let r = TestResult {
                            outcome: msg["outcome"].as_str().unwrap_or("").to_string(),
                            duration: msg["duration"].as_f64().unwrap_or(0.0),
                            reruns: msg["nodeid"].as_str().and_then(|id| reruns.get(id)).copied().unwrap_or(0),
                            subtests: serde_json::from_value(msg["subtests"].clone()).unwrap_or_default(),
                            failures: msg["failures"].as_array().cloned().unwrap_or_default(),
                        };
                        let nodeid = msg["nodeid"].as_str().unwrap_or("").to_string();
                        awaiting_rerun.remove(&nodeid);
                        w.running = None;
                        if let (Some(path), Some(i)) = (msg["path"].as_str(), msg["index"].as_u64()) {
                            let d = durations.entry(path.to_string()).or_default();
                            if d.len() <= i as usize {
                                d.resize(i as usize + 1, 0.0);
                            }
                            d[i as usize] = r.duration;
                        }
                        if matches!(r.outcome.as_str(), "failed" | "error") || r.subtests.contains_key("failed") {
                            failed += 1;
                            say(format!("{} {nodeid} {}", style("FAIL").red().bold(), style(format!("{:.2}s", r.duration)).dim()));
                            if maxfail.is_some_and(|m| failed >= m) && !stopping {
                                stopping = true;
                                queue.clear();
                                for w in &mut workers {
                                    let _ = w.child.kill();
                                }
                            }
                        } else if r.reruns > 0 {
                            say(format!("{} {nodeid} passed after {} rerun(s)", style("FLAKY").yellow().bold(), r.reruns));
                        }
                        results.insert(nodeid, r);
                        pb.inc(1);
                        pb.set_message(summary_counts(&results));
                    }
                    "done" => {
                        let path = msg["path"].as_str().unwrap_or("");
                        let chunk = msg["chunk"].as_u64().unwrap_or(0) as usize;
                        let only = msg["only"].as_str().filter(|s| !s.is_empty());
                        w.inflight.retain(|t| !(t.path == path && t.chunk == chunk && t.only.as_deref() == only));
                        if only.is_none() {
                            *chunks_done.entry(path.to_string()).or_default() += 1;
                        }
                    }
                    "start" => {
                        let offset = fs::metadata(log_path(&logs, wid)).map_or(0, |m| m.len());
                        w.running = msg["nodeid"].as_str().map(|id| (id.to_string(), msg["index"].as_u64().unwrap_or(0) as usize, offset));
                    }
                    "rerun" => {
                        w.running = None;
                        let nodeid = msg["nodeid"].as_str().unwrap_or("").to_string();
                        let attempt = reruns.entry(nodeid.clone()).or_default();
                        *attempt += 1;
                        if !stopping {
                            let path = msg["path"].as_str().unwrap_or("").to_string();
                            queue.push_front(Task { path, cuts: vec![], chunk: 0, estimate: None, only: Some(nodeid.clone()), attempt: *attempt, from: 0 });
                            awaiting_rerun.insert(nodeid);
                            if workers.iter().all(|w| w.closed) {
                                start_worker(&mut workers);
                                alive += 1;
                            }
                        }
                    }
                    "collect_error" => {
                        let path = msg["path"].as_str().unwrap_or("").to_string();
                        say(format!("{} {path}", style("COLLECT ERROR").red().bold()));
                        errors.push((format!("collect error: {path}"), msg["error"].as_str().unwrap_or("").to_string()));
                    }
                    "exit" => w.exited = true,
                    other => say(format!("pytest-turbo: unknown worker event {other}")),
                }
            }
            Event::Eof(wid) => {
                alive -= 1;
                let w = &mut workers[wid];
                w.closed = true;
                let _ = w.child.wait();
                if w.exited || stopping {
                    continue;
                }
                let log = fs::read_to_string(log_path(&logs, wid)).unwrap_or_default();
                let tail: Vec<&str> = log.lines().rev().take(30).collect();
                let tail = tail.into_iter().rev().collect::<Vec<_>>().join("\n");
                let mut tasks = std::mem::take(&mut w.inflight);
                // The test it was running fails, and its file goes on after it on another worker.
                if let (Some((nodeid, index, offset)), Some(current)) = (w.running.take(), tasks.first().cloned()) {
                    tasks.remove(0);
                    let since = log.get(offset as usize..).unwrap_or("");
                    let text = format!("worker died running this test\n{}", since.lines().take(60).collect::<Vec<_>>().join("\n"));
                    say(format!("{} {nodeid} worker {wid} died", style("CRASH").red().bold()));
                    awaiting_rerun.remove(&nodeid);
                    let r = TestResult {
                        outcome: "error".into(),
                        duration: 0.0,
                        reruns: reruns.get(&nodeid).copied().unwrap_or(0),
                        subtests: BTreeMap::new(),
                        failures: vec![json!({"when": "crash", "text": text, "sections": []})],
                    };
                    results.insert(nodeid, r);
                    pb.inc(1);
                    failed += 1;
                    if maxfail.is_some_and(|m| failed >= m) {
                        stopping = true;
                        queue.clear();
                        tasks.clear();
                        for w in &mut workers {
                            let _ = w.child.kill();
                        }
                    } else if current.only.is_none() {
                        queue.push_front(Task { from: index + 1, ..current });
                    }
                    queue.extend(tasks.drain(..)); // fetched ahead, not started
                }
                for task in tasks {
                    say(format!("{} worker {wid} died running {}", style("CRASH").red().bold(), task.path));
                    // Requeue once: tests after the one that killed the worker would otherwise never run.
                    if retried.insert((task.path.clone(), task.chunk, task.only.clone())) {
                        queue.push_front(task);
                    } else {
                        errors.push((format!("worker crashed twice running {} (chunk {})", task.path, task.chunk), tail.clone()));
                    }
                }
                if !queue.is_empty() && restarts < MAX_RESTARTS {
                    restarts += 1;
                    start_worker(&mut workers);
                    alive += 1;
                } else if !queue.is_empty() {
                    errors.push(("too many worker crashes, stopped".into(), tail));
                    queue.clear();
                }
            }
        }
    }
    pb.finish_and_clear();
    if !stopping {
        errors.extend(awaiting_rerun.into_iter().map(|id| (format!("rerun never reported: {id}"), String::new())));
    }

    let measured: Durations = durations.into_iter().filter(|(path, _)| chunks_done.get(path) == chunks_per_file.get(path)).collect();
    save_durations(&durations_file, &cfg.rootdir, &measured);
    let mut cache = known;
    cache.extend(measured);

    report(&results, &errors, &files, &cache, start, discovery, n, &cwd);
    let ok = errors.is_empty() && failed == 0;
    ExitCode::from(if results.is_empty() && ok { 5 } else if ok { 0 } else { 1 })
}

fn spawn(python: &Path, pytest_args: &[String], logs: &Path, wid: usize, n: usize, show_output: bool, tx: &mpsc::Sender<Event>) -> Worker {
    let mut cmd = Command::new(python);
    if show_output {
        cmd.env("PYTEST_TURBO_OUTPUT", "1").stderr(Stdio::inherit());
    } else {
        cmd.stderr(File::create(log_path(logs, wid)).expect("worker log"));
    }
    let mut child = cmd
        .arg("-c")
        .arg(WORKER_PY)
        .args(pytest_args)
        // Same names as pytest-xdist: suites use them to give each worker its own database.
        .env("PYTEST_XDIST_WORKER", format!("gw{wid}"))
        .env("PYTEST_XDIST_WORKER_COUNT", n.to_string())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap_or_else(|e| panic!("cannot start {}: {e}", python.display()));
    let stdout = child.stdout.take().unwrap();
    let tx = tx.clone();
    thread::spawn(move || {
        for line in BufReader::new(stdout).lines().map_while(Result::ok) {
            if tx.send(Event::Line(wid, line)).is_err() {
                return;
            }
        }
        let _ = tx.send(Event::Eof(wid));
    });
    let stdin = child.stdin.take().unwrap();
    Worker { child, stdin, inflight: vec![], exited: false, closed: false, running: None }
}

fn log_path(logs: &Path, wid: usize) -> PathBuf {
    logs.join(format!("worker-{wid}.log"))
}

/// Keys are stored relative to the rootdir, so the file stays valid when the checkout moves (CI).
fn load_durations(file: &Path, rootdir: &Path) -> Durations {
    let stored: Durations = fs::read_to_string(file).ok().and_then(|s| serde_json::from_str(&s).ok()).unwrap_or_default();
    stored.into_iter().map(|(k, v)| (rootdir.join(k).display().to_string(), v)).collect()
}

/// Merged into the file as it is now (other suites may have written to it meanwhile), then
/// written to a temporary file and renamed, so concurrent runs never see half of one.
fn save_durations(file: &Path, rootdir: &Path, measured: &Durations) {
    let mut relative: Durations = fs::read_to_string(file).ok().and_then(|s| serde_json::from_str(&s).ok()).unwrap_or_default();
    relative.extend(
        measured.iter().map(|(k, v)| (Path::new(k).strip_prefix(rootdir).map_or(k.clone(), |p| p.display().to_string()), v.clone())),
    );
    let tmp = file.with_extension(format!("tmp{}", std::process::id()));
    let written = file.parent().map_or(Ok(()), fs::create_dir_all).and_then(|_| fs::write(&tmp, serde_json::to_string(&relative).unwrap()));
    if written.and_then(|_| fs::rename(&tmp, file)).is_err() {
        eprintln!("pytest-turbo: could not write {}", file.display());
        let _ = fs::remove_file(&tmp);
    }
}

fn find_python(cwd: &Path) -> PathBuf {
    if let Some(venv) = std::env::var_os("VIRTUAL_ENV") {
        return Path::new(&venv).join("bin/python");
    }
    let beside_exe = std::env::current_exe().ok().and_then(|e| Some(e.parent()?.join("python")));
    cwd.ancestors()
        .map(|d| d.join(".venv/bin/python"))
        .chain(beside_exe)
        .find(|p| p.exists())
        .unwrap_or_else(|| PathBuf::from("python3"))
}

fn summary_counts(results: &BTreeMap<String, TestResult>) -> String {
    let mut counts: BTreeMap<String, u64> = BTreeMap::new();
    for r in results.values() {
        *counts.entry(r.outcome.clone()).or_default() += 1;
        if r.reruns > 0 && r.outcome == "passed" {
            *counts.entry("flaky".into()).or_default() += 1;
        }
        for (k, v) in &r.subtests {
            *counts.entry(format!("subtests {k}")).or_default() += v;
        }
    }
    let order = ["failed", "error", "passed", "flaky", "skipped", "xfailed", "xpassed"];
    let mut keys: Vec<&String> = counts.keys().collect();
    keys.sort_by_key(|k| order.iter().position(|o| o == k).unwrap_or(order.len()));
    keys.iter().map(|k| format!("{} {k}", counts[*k])).collect::<Vec<_>>().join(", ")
}

#[allow(clippy::too_many_arguments)]
fn report(
    results: &BTreeMap<String, TestResult>,
    errors: &[(String, String)],
    files: &[(String, u64)],
    cache: &Durations,
    start: Instant,
    discovery: Duration,
    n: usize,
    cwd: &Path,
) {
    for (nodeid, r) in results {
        for f in &r.failures {
            let when = f["when"].as_str().unwrap_or("");
            println!("\n{} {}", style(format!("━━ {nodeid}")).red().bold(), style(format!("({when}, {:.2}s)", r.duration)).dim());
            println!("{}", f["text"].as_str().unwrap_or("").trim_end());
            for section in f["sections"].as_array().into_iter().flatten() {
                let (title, body) = (section[0].as_str().unwrap_or(""), section[1].as_str().unwrap_or(""));
                if !body.trim().is_empty() {
                    println!("{}\n{}", style(format!("── {title}")).dim(), body.trim_end());
                }
            }
        }
    }
    for (title, body) in errors {
        println!("\n{}\n{}", style(format!("━━ {title}")).red().bold(), body.trim_end());
    }
    let mut slow: Vec<(&String, f64)> = files.iter().filter_map(|(f, _)| cache.get(f).map(|d| (f, d.iter().sum()))).collect();
    slow.sort_by(|a, b| b.1.total_cmp(&a.1));
    if !slow.is_empty() {
        println!("\n{}", style("slowest files").dim());
        for (f, d) in slow.iter().take(5) {
            let rel = Path::new(f).strip_prefix(cwd).map_or(f.to_string(), |p| p.display().to_string());
            println!("  {d:6.2}s {rel}");
        }
    }
    println!(
        "\n{} {}",
        summary_counts(results),
        style(format!("in {:.2}s ({n} workers, discovery {:.0}ms)", start.elapsed().as_secs_f64(), discovery.as_secs_f64() * 1000.0)).dim()
    );
}
