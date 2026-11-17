use std::collections::HashMap;

/// Per-test durations from earlier runs: file path -> durations in collection order.
pub type Durations = HashMap<String, Vec<f64>>;

#[derive(Debug, Clone, PartialEq)]
pub struct Task {
    pub path: String,
    /// Item indices where chunks start; the worker runs `items[cuts[chunk-1]..cuts[chunk]]`
    /// with an implicit 0 in front and the file's end behind, so chunks always cover the
    /// whole file even when it gained or lost tests since the durations were recorded.
    pub cuts: Vec<usize>,
    pub chunk: usize,
    pub estimate: Option<f64>,
}

impl Task {
    pub fn line(&self) -> String {
        let cuts: Vec<String> = self.cuts.iter().map(|c| c.to_string()).collect();
        format!("{}\t{}\t{}\n", self.path, cuts.join(","), self.chunk)
    }
}

/// Longest first; files never timed go first, largest first. A file that alone takes longer
/// than a worker's fair share is split into chunks of roughly equal recorded duration.
pub fn plan(files: &[(String, u64)], known: &Durations, workers: usize) -> Vec<Task> {
    let total: f64 = files.iter().filter_map(|(f, _)| known.get(f)).flatten().sum();
    let share = total / workers.max(1) as f64;
    let mut tasks = Vec::new();
    for (path, _) in files {
        let Some(durations) = known.get(path) else {
            tasks.push(Task { path: path.clone(), cuts: vec![], chunk: 0, estimate: None });
            continue;
        };
        let cuts = split(durations, share, workers);
        let mut bounds = vec![0];
        bounds.extend(&cuts);
        bounds.push(durations.len());
        for chunk in 0..=cuts.len() {
            let estimate = durations[bounds[chunk]..bounds[chunk + 1]].iter().sum();
            tasks.push(Task { path: path.clone(), cuts: cuts.clone(), chunk, estimate: Some(estimate) });
        }
    }
    let size: HashMap<&String, u64> = files.iter().map(|(f, s)| (f, *s)).collect();
    tasks.sort_by(|a, b| match (a.estimate, b.estimate) {
        (None, None) => size[&b.path].cmp(&size[&a.path]),
        (None, Some(_)) => std::cmp::Ordering::Less,
        (Some(_), None) => std::cmp::Ordering::Greater,
        (Some(x), Some(y)) => y.total_cmp(&x),
    });
    tasks
}

fn split(durations: &[f64], share: f64, workers: usize) -> Vec<usize> {
    let total: f64 = durations.iter().sum();
    if share <= 0.0 || total <= share || durations.len() < 2 {
        return vec![];
    }
    let chunks = ((total / share).ceil() as usize).min(workers).min(durations.len());
    let mut cuts = Vec::new();
    let mut acc = 0.0;
    for (i, d) in durations.iter().enumerate() {
        let next = cuts.len() + 1;
        if next < chunks && i > 0 && acc + d / 2.0 >= total * next as f64 / chunks as f64 {
            cuts.push(i);
        }
        acc += d;
    }
    // Every chunk re-imports the module and repeats class setup: don't cut off slivers.
    let mut bounds = cuts.clone();
    bounds.push(durations.len());
    let mut kept = Vec::new();
    let mut start = 0;
    for (j, &cut) in cuts.iter().enumerate() {
        let before: f64 = durations[start..cut].iter().sum();
        let after: f64 = durations[cut..bounds[j + 1]].iter().sum();
        if before >= share / 4.0 && after >= share / 4.0 {
            kept.push(cut);
            start = cut;
        }
    }
    kept
}

#[cfg(test)]
mod tests {
    use super::*;

    fn files(names: &[&str]) -> Vec<(String, u64)> {
        names.iter().enumerate().map(|(i, n)| (n.to_string(), i as u64)).collect()
    }

    #[test]
    fn long_file_is_split_into_balanced_chunks() {
        let known = Durations::from([("big".into(), vec![1.0; 12]), ("small".into(), vec![1.0; 4])]);
        let tasks = plan(&files(&["big", "small"]), &known, 2);
        let big: Vec<_> = tasks.iter().filter(|t| t.path == "big").collect();
        assert_eq!(big.len(), 2);
        assert_eq!(big[0].cuts, vec![6]);
        assert_eq!(big.iter().map(|t| t.chunk).collect::<Vec<_>>(), vec![0, 1]);
        assert_eq!(tasks.last().unwrap().path, "small");
    }

    #[test]
    fn unknown_files_go_first_and_are_not_split() {
        let known = Durations::from([("old".into(), vec![50.0, 50.0])]);
        let tasks = plan(&files(&["old", "new_small", "new_big"]), &known, 4);
        let order: Vec<_> = tasks.iter().map(|t| t.path.as_str()).collect();
        assert_eq!(order, vec!["new_big", "new_small", "old", "old"]);
    }

    #[test]
    fn single_slow_test_is_never_split() {
        assert_eq!(split(&[100.0], 1.0, 8), Vec::<usize>::new());
        assert_eq!(split(&[100.0, 0.1, 0.1], 10.0, 8), Vec::<usize>::new());
        assert_eq!(split(&[5.0, 5.0, 5.0, 5.0], 10.0, 8), vec![2]);
    }
}
