use std::fs;
use std::path::{Path, PathBuf};

use globset::{Glob, GlobMatcher};

const DEFAULT_NORECURSE: &[&str] = &["*.egg", ".*", "_darcs", "build", "CVS", "dist", "node_modules", "venv", "{arch}"];

pub struct Config {
    pub rootdir: PathBuf,
    pub testpaths: Vec<String>,
    python_files: Vec<GlobMatcher>,
    norecursedirs: Vec<GlobMatcher>,
    ignore: Vec<PathBuf>,
    ignore_glob: Vec<GlobMatcher>,
}

/// The pytest settings that decide which files exist as tests, read the way pytest finds its
/// config: the first of these, walking up from `cwd`, that has a pytest section.
pub fn load(cwd: &Path) -> Config {
    let found = cwd.ancestors().find_map(|dir| {
        [("pytest.ini", "pytest"), (".pytest.ini", "pytest"), ("pyproject.toml", ""), ("tox.ini", "pytest"), ("setup.cfg", "tool:pytest")]
            .iter()
            .find_map(|(name, section)| {
                let text = fs::read_to_string(dir.join(name)).ok()?;
                let get = if name.ends_with(".toml") { toml_options(&text)? } else { ini_section(&text, section)? };
                Some((dir.to_path_buf(), get))
            })
    });
    let (rootdir, options) = found.unwrap_or_else(|| (cwd.to_path_buf(), Vec::new()));
    let get = |key: &str| options.iter().find(|(k, _)| k == key).map(|(_, v)| v.clone());
    Config {
        rootdir,
        testpaths: get("testpaths").unwrap_or_default(),
        python_files: globs(get("python_files").unwrap_or_else(|| vec!["test_*.py".into(), "*_test.py".into()])),
        norecursedirs: globs(get("norecursedirs").unwrap_or_else(|| DEFAULT_NORECURSE.iter().map(|s| s.to_string()).collect())),
        ignore: Vec::new(),
        ignore_glob: Vec::new(),
    }
}

impl Config {
    /// `--ignore` and `--ignore-glob` from the pytest arguments. pytest never applies them to the paths
    /// on its command line, and the workers get every file that way, so discovery has to.
    pub fn add_ignores(&mut self, pytest_args: &[String], cwd: &Path) {
        let cwd = cwd.canonicalize().unwrap_or_else(|_| cwd.to_path_buf());
        let mut args = pytest_args.iter();
        while let Some(arg) = args.next() {
            for (flag, glob) in [("--ignore", false), ("--ignore-glob", true)] {
                let value = match arg.strip_prefix(flag) {
                    Some("") => args.clone().next().cloned(),
                    Some(v) if v.starts_with('=') => Some(v[1..].to_string()),
                    _ => None,
                };
                let Some(value) = value else { continue };
                let path = cwd.join(&value);
                if glob {
                    // pytest fnmatches the absolute path, where `*` also crosses `/`
                    self.ignore_glob.extend(Glob::new(&path.display().to_string()).ok().map(|g| g.compile_matcher()));
                } else {
                    self.ignore.push(path.canonicalize().unwrap_or(path));
                }
            }
        }
    }

    fn ignored(&self, path: &Path) -> bool {
        self.ignore.iter().any(|p| p == path) || self.ignore_glob.iter().any(|g| g.is_match(path))
    }
}

type Options = Vec<(String, Vec<String>)>;

fn toml_options(text: &str) -> Option<Options> {
    let doc: toml::Table = text.parse().ok()?;
    let opts = doc.get("tool")?.get("pytest")?.get("ini_options")?.as_table()?;
    Some(
        opts.iter()
            .map(|(k, v)| {
                let vals = match v {
                    toml::Value::Array(a) => a.iter().filter_map(|x| x.as_str().map(String::from)).collect(),
                    toml::Value::String(s) => s.split_whitespace().map(String::from).collect(),
                    _ => vec![],
                };
                (k.clone(), vals)
            })
            .collect(),
    )
}

/// `key = a b` with indented continuation lines, inside `[section]`.
fn ini_section(text: &str, section: &str) -> Option<Options> {
    let header = format!("[{section}]");
    let mut lines = text.lines().skip_while(|l| l.trim() != header);
    lines.next()?;
    let mut opts: Options = Vec::new();
    for line in lines.take_while(|l| !l.trim_start().starts_with('[')) {
        if line.trim().is_empty() || line.trim_start().starts_with(['#', ';']) {
            continue;
        }
        if line.starts_with(char::is_whitespace) {
            if let Some((_, vals)) = opts.last_mut() {
                vals.extend(line.split_whitespace().map(String::from));
            }
        } else if let Some((k, v)) = line.split_once(['=', ':']) {
            opts.push((k.trim().to_string(), v.split_whitespace().map(String::from).collect()));
        }
    }
    Some(opts)
}

fn globs(patterns: Vec<String>) -> Vec<GlobMatcher> {
    patterns.iter().filter_map(|p| Glob::new(p).ok()).map(|g| g.compile_matcher()).collect()
}

/// Test files under `roots`, absolute, with their size in bytes. Explicit file arguments are
/// kept as given, like pytest does.
pub fn discover(roots: &[PathBuf], cfg: &Config) -> Vec<(String, u64)> {
    let mut out = Vec::new();
    for root in roots {
        let Ok(root) = root.canonicalize() else {
            eprintln!("pytest-turbo: no such path: {}", root.display());
            continue;
        };
        if root.is_file() {
            push(&mut out, &root);
        } else {
            walk(&root, cfg, &mut out);
        }
    }
    out.sort();
    out.dedup();
    out
}

fn walk(dir: &Path, cfg: &Config, out: &mut Vec<(String, u64)>) {
    let Ok(entries) = fs::read_dir(dir) else { return };
    for entry in entries.flatten() {
        let path = entry.path();
        let name = entry.file_name();
        let Ok(kind) = entry.file_type() else { continue };
        if cfg.ignored(&path) {
            continue;
        }
        if kind.is_dir() {
            let skip = name == "__pycache__"
                || path.join("pyvenv.cfg").exists()
                || cfg.norecursedirs.iter().any(|g| g.is_match(&name));
            if !skip {
                walk(&path, cfg, out);
            }
        } else if cfg.python_files.iter().any(|g| if g.glob().glob().contains('/') { g.is_match(&path) } else { g.is_match(&name) }) {
            push(out, &path);
        }
    }
}

fn push(out: &mut Vec<(String, u64)>, path: &Path) {
    out.push((path.display().to_string(), fs::metadata(path).map_or(0, |m| m.len())));
}
