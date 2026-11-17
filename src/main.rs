mod config;
mod schedule;

use std::collections::{BTreeMap, HashMap, HashSet, VecDeque};
use std::fs::{self, File};
use std::io::{self, BufRead, BufReader, Write};
use std::net::Shutdown;
use std::os::unix::net::{UnixListener, UnixStream};
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
    /// Start every worker on its own, instead of forking them from one process that has imported what the last run's workers imported (always on macOS)
    #[arg(long)]
    no_preload: bool,
    /// Files or directories (default: testpaths from the pytest config, else ".")
    paths: Vec<PathBuf>,
    /// Extra arguments for each pytest worker, after --
    #[arg(last = true)]
    pytest_args: Vec<String>,
}

enum Event {
    Line(usize, String),
    Eof(usize),
    /// A forked worker connected: its task stream and pid.
    Connected(usize, UnixStream, u32),
    ZygoteGone,
}

enum Proc {
    Spawned(Child),
    /// The pid once the worker has connected.
    Forked(Option<u32>),
}

/// Unless --no-preload: the process the workers are forked from.
struct Zygote {
    child: Child,
    stdin: ChildStdin,
    socket: PathBuf,
}

impl Zygote {
    fn send(&mut self, command: Value) -> bool {
        writeln!(self.stdin, "{command}").is_ok()
    }
}

struct Worker {
    proc: Proc,
    stdin: Option<Box<dyn Write + Send>>,
    inflight: Vec<Task>,
    exited: bool,
    /// Told there is no more work: it will not ask again.
    closed: bool,
    /// The test it is running: node id, index in its file, where its output starts in the log, reruns it has left.
    running: Option<(String, usize, u64, u64)>,
    /// Forked after the zygote died and replaced by a spawned worker: whatever it sends is ignored.
    ghost: bool,
}

impl Worker {
    fn new(proc: Proc, stdin: Option<Box<dyn Write + Send>>) -> Worker {
        Worker { proc, stdin, inflight: vec![], exited: false, closed: false, running: None, ghost: false }
    }
}

fn kill_all(workers: &mut [Worker], zygote: &mut Option<Zygote>) {
    for w in workers {
        match &mut w.proc {
            Proc::Spawned(child) => {
                let _ = child.kill();
            }
            Proc::Forked(Some(pid)) => {
                if let Some(z) = zygote {
                    z.send(json!({"kill": pid}));
                }
            }
            Proc::Forked(None) => {} // told there is no work when it asks
        }
    }
}

/// One snapshots/snap_<module>.py under --snapshot-update, as the workers report it.
#[derive(Default)]
struct SnapFile {
    /// Task paths whose tests used it: unvisited entries go only when all of them ran in full.
    paths: HashSet<String>,
    /// Statement source by key, as the file has it.
    entries: Option<BTreeMap<String, String>>,
    visited: HashSet<String>,
    updates: BTreeMap<String, String>,
}

const SNAPSHOT_HEADER: &str = "# Written by pytest-turbo --snapshot-update.\nsnapshots = {}\n";

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
    let preload_file = durations_file.with_extension("modules.json");
    let preload_key = cwd.strip_prefix(&cfg.rootdir).map_or(cwd.display().to_string(), |p| p.display().to_string());
    // macOS system frameworks aren't fork-safe: a forked worker hangs in urllib's proxy lookup (httpx calls it for every client).
    let preload = !args.no_preload && !cfg!(target_os = "macos");
    let mut zygote = None;
    if preload {
        let saved = read_json(&preload_file).get(&preload_key).cloned().unwrap_or(Value::Null);
        zygote = start_zygote(&python, &args.pytest_args, &logs, n, show_output, &saved, &tx);
    }
    let mut workers: Vec<Worker> = Vec::new();
    let start_worker = |workers: &mut Vec<Worker>, zygote: &mut Option<Zygote>| {
        let wid = workers.len();
        let log = (!show_output).then(|| log_path(&logs, wid));
        if zygote.as_mut().is_some_and(|z| z.send(json!({"fork": wid, "log": log}))) {
            workers.push(Worker::new(Proc::Forked(None), None));
        } else {
            workers.push(spawn(&python, &args.pytest_args, &logs, wid, n, show_output, preload, &tx));
        }
    };
    for _ in 0..n {
        start_worker(&mut workers, &mut zygote);
    }
    // What the workers imported, in the order first seen.
    let mut imported: Vec<Value> = vec![];
    let mut seen: HashSet<String> = HashSet::new();

    let mut queue: VecDeque<Task> = tasks.into();
    let mut results: BTreeMap<String, TestResult> = BTreeMap::new();
    let mut durations: HashMap<String, Vec<f64>> = HashMap::new();
    let mut chunks_done: HashMap<String, usize> = HashMap::new();
    let mut errors: Vec<(String, String)> = Vec::new();
    let mut retried: HashSet<(String, usize, Option<String>)> = HashSet::new();
    let mut reruns: HashMap<String, u64> = HashMap::new();
    let mut awaiting_rerun: HashSet<String> = HashSet::new();
    let mut snapfiles: BTreeMap<String, SnapFile> = BTreeMap::new();
    let mut partial: HashSet<String> = HashSet::new();
    let mut failed_paths: HashSet<String> = HashSet::new();
    let mut failed = 0;
    let mut restarts = 0;
    let mut stopping = false;
    let mut alive = n;

    while alive > 0 {
        let Ok(event) = rx.recv() else { break };
        match event {
            Event::Connected(wid, stream, pid) => {
                let w = &mut workers[wid];
                if w.ghost {
                    let _ = stream.shutdown(Shutdown::Both);
                } else {
                    w.proc = Proc::Forked(Some(pid));
                    w.stdin = Some(Box::new(stream));
                }
            }
            Event::ZygoteGone => {
                let log = fs::read_to_string(logs.join("zygote.log")).unwrap_or_default();
                let tail: Vec<&str> = log.lines().rev().take(30).collect();
                say(format!(
                    "pytest-turbo: the process the workers are forked from died, starting workers without it\n{}",
                    tail.into_iter().rev().collect::<Vec<_>>().join("\n")
                ));
                zygote = None;
                for wid in 0..workers.len() {
                    let w = &mut workers[wid];
                    if matches!(w.proc, Proc::Forked(None)) && !w.closed {
                        w.ghost = true;
                        w.closed = true;
                        start_worker(&mut workers, &mut zygote);
                    }
                }
            }
            Event::Line(wid, _) | Event::Eof(wid) if workers[wid].ghost => {}
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
                        if let Some(stdin) = w.stdin.as_mut() {
                            let _ = stdin.write_all(line.as_bytes());
                        }
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
                            failed_paths.insert(msg["path"].as_str().unwrap_or("").to_string());
                            say(format!("{} {nodeid} {}", style("FAIL").red().bold(), style(format!("{:.2}s", r.duration)).dim()));
                            if maxfail.is_some_and(|m| failed >= m) && !stopping {
                                stopping = true;
                                queue.clear();
                                kill_all(&mut workers, &mut zygote);
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
                        let left = msg["reruns"].as_u64().unwrap_or(0);
                        w.running = msg["nodeid"].as_str().map(|id| (id.to_string(), msg["index"].as_u64().unwrap_or(0) as usize, offset, left));
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
                                start_worker(&mut workers, &mut zygote);
                                alive += 1;
                            }
                        }
                    }
                    "snapfile" => {
                        let sf = snapfiles.entry(msg["file"].as_str().unwrap_or("").to_string()).or_default();
                        sf.paths.insert(msg["path"].as_str().unwrap_or("").to_string());
                        if sf.entries.is_none() {
                            sf.entries = serde_json::from_value(msg["entries"].clone()).ok();
                        }
                    }
                    "snapshot" => {
                        let sf = snapfiles.entry(msg["file"].as_str().unwrap_or("").to_string()).or_default();
                        sf.paths.insert(msg["path"].as_str().unwrap_or("").to_string());
                        let key = msg["key"].as_str().unwrap_or("").to_string();
                        if let Some(entry) = msg["entry"].as_str() {
                            sf.updates.insert(key.clone(), entry.to_string());
                        }
                        sf.visited.insert(key);
                    }
                    "partial" => {
                        partial.insert(msg["path"].as_str().unwrap_or("").to_string());
                    }
                    "collect_error" => {
                        let path = msg["path"].as_str().unwrap_or("").to_string();
                        say(format!("{} {path}", style("COLLECT ERROR").red().bold()));
                        errors.push((format!("collect error: {path}"), msg["error"].as_str().unwrap_or("").to_string()));
                    }
                    "exit" => {
                        w.exited = true;
                        for m in msg["modules"].as_array().into_iter().flatten() {
                            if seen.insert(m.to_string()) {
                                imported.push(m.clone());
                            }
                        }
                    }
                    other => say(format!("pytest-turbo: unknown worker event {other}")),
                }
            }
            Event::Eof(wid) => {
                alive -= 1;
                let w = &mut workers[wid];
                w.closed = true;
                if let Proc::Spawned(child) = &mut w.proc {
                    let _ = child.wait();
                }
                if w.exited || stopping {
                    continue;
                }
                let log = fs::read_to_string(log_path(&logs, wid)).unwrap_or_default();
                let tail: Vec<&str> = log.lines().rev().take(30).collect();
                let tail = tail.into_iter().rev().collect::<Vec<_>>().join("\n");
                let mut tasks = std::mem::take(&mut w.inflight);
                // The test it was running is rerun or fails, and its file goes on after it on another worker.
                if let (Some((nodeid, index, offset, left)), Some(current)) = (w.running.take(), tasks.first().cloned()) {
                    tasks.remove(0);
                    let since = log.get(offset as usize..).unwrap_or("");
                    let text = format!("worker died running this test\n{}", since.lines().take(60).collect::<Vec<_>>().join("\n"));
                    awaiting_rerun.remove(&nodeid);
                    if current.only.is_none() {
                        queue.push_front(Task { from: index + 1, ..current.clone() });
                    }
                    // A crash has no exception for --only-rerun to match, so any --reruns covers it.
                    if left > 0 {
                        say(format!("{} {nodeid} worker {wid} died, rerunning", style("CRASH").yellow().bold()));
                        let attempt = reruns.entry(nodeid.clone()).or_default();
                        *attempt += 1;
                        queue.push_front(Task { path: current.path, cuts: vec![], chunk: 0, estimate: None, only: Some(nodeid.clone()), attempt: *attempt, from: 0 });
                        awaiting_rerun.insert(nodeid);
                        queue.extend(tasks.drain(..));
                        if restarts < MAX_RESTARTS {
                            restarts += 1;
                            start_worker(&mut workers, &mut zygote);
                            alive += 1;
                        } else {
                            errors.push(("too many worker crashes, stopped".into(), tail));
                            queue.clear();
                        }
                        continue;
                    }
                    say(format!("{} {nodeid} worker {wid} died", style("CRASH").red().bold()));
                    failed_paths.insert(current.path.clone());
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
                        kill_all(&mut workers, &mut zygote);
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
                    start_worker(&mut workers, &mut zygote);
                    alive += 1;
                } else if !queue.is_empty() {
                    errors.push(("too many worker crashes, stopped".into(), tail));
                    queue.clear();
                }
            }
        }
    }
    pb.finish_and_clear();
    if let Some(z) = zygote.take() {
        let Zygote { mut child, stdin, socket } = z;
        drop(stdin); // it exits at the end of its input
        let _ = child.wait();
        let _ = fs::remove_file(&socket);
    }
    if preload && !stopping && !imported.is_empty() {
        update_json(&preload_file, |saved| {
            saved.insert(preload_key, json!({"modules": imported}));
        });
    }
    if !stopping {
        errors.extend(awaiting_rerun.into_iter().map(|id| (format!("rerun never reported: {id}"), String::new())));
    }

    let complete = |p: &String| {
        !stopping && !p.contains("::") && !partial.contains(p) && !failed_paths.contains(p) && chunks_done.get(p) == chunks_per_file.get(p)
    };
    let snapshot_summary = write_snapshots(snapfiles, complete, &mut errors);

    let measured: Durations = durations.into_iter().filter(|(path, _)| chunks_done.get(path) == chunks_per_file.get(path)).collect();
    save_durations(&durations_file, &cfg.rootdir, &measured);
    let mut cache = known;
    cache.extend(measured);

    report(&results, &errors, &files, &cache, start, discovery, n, &cwd);
    if let Some(s) = snapshot_summary {
        println!("{}", style(s).dim());
    }
    let ok = errors.is_empty() && failed == 0;
    ExitCode::from(if results.is_empty() && ok { 5 } else if ok { 0 } else { 1 })
}

#[allow(clippy::too_many_arguments)]
fn spawn(python: &Path, pytest_args: &[String], logs: &Path, wid: usize, n: usize, show_output: bool, record: bool, tx: &mpsc::Sender<Event>) -> Worker {
    let mut cmd = Command::new(python);
    if record {
        cmd.env("PYTEST_TURBO_RECORD", "1");
    }
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
    thread::spawn(move || forward(wid, BufReader::new(stdout).lines(), &tx));
    let stdin = child.stdin.take().unwrap();
    Worker::new(Proc::Spawned(child), Some(Box::new(stdin)))
}

fn forward(wid: usize, lines: impl Iterator<Item = io::Result<String>>, tx: &mpsc::Sender<Event>) {
    for line in lines.map_while(Result::ok) {
        if tx.send(Event::Line(wid, line)).is_err() {
            return;
        }
    }
    let _ = tx.send(Event::Eof(wid));
}

/// Starts the process the workers are forked from. `None` if it can't start: the workers are then spawned.
fn start_zygote(
    python: &Path,
    pytest_args: &[String],
    logs: &Path,
    n: usize,
    show_output: bool,
    saved: &Value,
    tx: &mpsc::Sender<Event>,
) -> Option<Zygote> {
    let socket = std::env::temp_dir().join(format!("pytest-turbo-{}.sock", std::process::id()));
    let _ = fs::remove_file(&socket);
    let listener = UnixListener::bind(&socket).map_err(|e| eprintln!("pytest-turbo: not preloading, cannot listen on {}: {e}", socket.display())).ok()?;
    let mut cmd = Command::new(python);
    if show_output {
        cmd.env("PYTEST_TURBO_OUTPUT", "1").stderr(Stdio::inherit());
    } else {
        cmd.stderr(File::create(logs.join("zygote.log")).expect("zygote log"));
    }
    let mut child = cmd
        .arg("-c")
        .arg(WORKER_PY)
        .args(pytest_args)
        .env("PYTEST_TURBO_ZYGOTE", &socket)
        .env("PYTEST_TURBO_RECORD", "1")
        .env("PYTEST_XDIST_WORKER_COUNT", n.to_string())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .map_err(|e| eprintln!("pytest-turbo: not preloading, cannot start {}: {e}", python.display()))
        .ok()?;
    let mut stdin = child.stdin.take().unwrap();
    writeln!(stdin, "{saved}").ok()?;
    let mut stdout = child.stdout.take().unwrap();
    let gone = tx.clone();
    // The workers close their copy of this pipe, so its end means the zygote itself has exited.
    thread::spawn(move || {
        let _ = io::copy(&mut stdout, &mut io::sink());
        let _ = gone.send(Event::ZygoteGone);
    });
    let tx = tx.clone();
    thread::spawn(move || {
        for stream in listener.incoming().map_while(Result::ok) {
            let tx = tx.clone();
            thread::spawn(move || {
                let Ok(read) = stream.try_clone() else { return };
                let mut lines = BufReader::new(read).lines();
                let Some(hello) = lines.next().and_then(Result::ok).and_then(|l| serde_json::from_str::<Value>(&l).ok()) else { return };
                let (Some(wid), Some(pid)) = (hello["wid"].as_u64(), hello["pid"].as_u64()) else { return };
                if tx.send(Event::Connected(wid as usize, stream, pid as u32)).is_ok() {
                    forward(wid as usize, lines, &tx);
                }
            });
        }
    });
    Some(Zygote { child, stdin, socket })
}

/// Writes each snapshot file once, after every worker is done: entries kept as they were, updated
/// ones replaced, and unvisited ones removed when every test that uses the file ran (snapshottest
/// removed them per worker, which is why it could not split a file).
fn write_snapshots(files: BTreeMap<String, SnapFile>, complete: impl Fn(&String) -> bool, errors: &mut Vec<(String, String)>) -> Option<String> {
    if files.is_empty() {
        return None;
    }
    let (mut written, mut removed, mut changed) = (0, 0, 0);
    for (file, sf) in files {
        let mut entries = sf.entries.unwrap_or_default();
        written += sf.updates.len();
        entries.extend(sf.updates);
        if sf.paths.iter().all(&complete) {
            let before = entries.len();
            entries.retain(|k, _| sf.visited.contains(k));
            removed += before - entries.len();
        }
        let path = Path::new(&file);
        let old = fs::read_to_string(path).ok();
        let result = if entries.is_empty() {
            old.map_or(Ok(false), |_| fs::remove_file(path).map(|_| true))
        } else {
            let text = SNAPSHOT_HEADER.to_string() + &entries.values().map(|s| format!("\n{s}\n")).collect::<String>();
            if old.as_deref() == Some(text.as_str()) {
                Ok(false)
            } else {
                path.parent().map_or(Ok(()), fs::create_dir_all).and_then(|_| fs::write(path, text)).map(|_| true)
            }
        };
        match result {
            Ok(true) => changed += 1,
            Ok(false) => {}
            Err(e) => errors.push((format!("could not write {file}"), e.to_string())),
        }
    }
    Some(format!("snapshots: {written} written, {removed} removed, {changed} files changed"))
}

fn log_path(logs: &Path, wid: usize) -> PathBuf {
    logs.join(format!("worker-{wid}.log"))
}

/// Keys are stored relative to the rootdir, so the file stays valid when the checkout moves (CI).
fn load_durations(file: &Path, rootdir: &Path) -> Durations {
    let stored: Durations = serde_json::from_value(Value::Object(read_json(file))).unwrap_or_default();
    stored.into_iter().map(|(k, v)| (rootdir.join(k).display().to_string(), v)).collect()
}

fn save_durations(file: &Path, rootdir: &Path, measured: &Durations) {
    update_json(file, |saved| {
        for (k, v) in measured {
            saved.insert(Path::new(k).strip_prefix(rootdir).map_or(k.clone(), |p| p.display().to_string()), json!(v));
        }
    });
}

fn read_json(file: &Path) -> serde_json::Map<String, Value> {
    fs::read_to_string(file).ok().and_then(|s| serde_json::from_str(&s).ok()).unwrap_or_default()
}

/// Applied to the file as it is now (other suites may have written to it meanwhile), then
/// written to a temporary file and renamed, so concurrent runs never see half of one.
fn update_json(file: &Path, update: impl FnOnce(&mut serde_json::Map<String, Value>)) {
    let mut saved = read_json(file);
    update(&mut saved);
    let tmp = file.with_extension(format!("tmp{}", std::process::id()));
    let written = file.parent().map_or(Ok(()), fs::create_dir_all).and_then(|_| fs::write(&tmp, Value::Object(saved).to_string()));
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
