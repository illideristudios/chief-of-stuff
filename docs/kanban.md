# Kanban walk

The tracker's `lane` and `stage` remain the record of a task's progress. A `[kanban]` table in the workspace settings file maps each precise stage to one board column. The `!! - HUMAN REVIEW REQUIRED` label is an additional hold on that column. A coordinator records the decision and tracker transition first, then syncs the corresponding issue. `audit_tasks.py` reports disagreement without changing either side.

For the existing six-column GitLab board, configure the workspace TOML like this:

```toml
[lanes]
designed = { stages = ["design", "test", "implement", "pr", "review", "triage", "fix", "verify", "merge", "main"] }
build = { stages = ["plan", "test", "implement", "pr", "review", "triage", "fix", "verify", "merge", "main"] }

[kanban]
stages = [
  "00 - ARCHITECTURE / PLANNING",
  "01 - ARCHITECTURE / PLAN REVIEW",
  "02 - TEST IMPLEMENTATION",
  "03 - IMPLEMENTATION",
  "04 - CODE REVIEW",
  "05 - REFACTOR / ADDRESS ISSUES",
]
human_review_label = "!! - HUMAN REVIEW REQUIRED"
terminal_stages = ["main"]

[kanban.map]
design = 0
plan = 0
test = 2
implement = 3
pr = 4
review = 4
triage = 4
fix = 5
verify = 4
merge = 4
```

New code tasks pass through planning, tests, implementation and review without owner yes-gates. Fixes return to review. Legacy lane `gates` and kanban `hold_stages` settings are accepted and ignored. A problem hold records an architectural question or two failures, with research, options and a recommendation; a split or retry-exhausted trio also waits on the owner. The worker guard remains in force. Existing tasks already in later stages stay there.

For a GitHub backlog, omit `[kanban.github_project]` to use issue labels as the columns. To use a GitHub Project's single-select Status field instead, add:

```toml
[kanban.github_project]
owner = "your-org"
number = 12
field = "Status"
options = ["Planning", "Plan Review", "Testing", "Implementation", "Code Review", "Fixes"]
```

The options correspond to `[kanban].stages` in order. Terminal stages are additional marks named exactly like the tracker stage: the example needs a literal `main` label in label mode, or a `main` Status option in Project mode. Keep `main` in `terminal_stages`; it is additional to the numbered `stages` and their corresponding `options`.

In label mode, create every label listed under `stages`, every name under `terminal_stages`, and `human_review_label`. For the example's terminal label:

```sh
# GitHub
gh label create main --repo your-org/your-repo
# GitLab
glab label create --name main --color '#428bca' --repo your-group/your-repo
```

In Project mode, create the Status options `Planning`, `Plan Review`, `Testing`, `Implementation`, `Code Review`, `Fixes`, and `main` in the Project's field settings. The numbered stage lives in Status and `!!` remains an issue label. The updater adds the issue to the configured Project if it is absent. These labels and Status options must already exist; the updater reports missing marks and tells you to create them. Workspace initialization provisions the configured stage, terminal, and hold labels in label mode; in Project mode it provisions the hold label and checks all configured Status options, including terminal stages.

Project reads refuse truncated item or field metadata, including Projects with more than 100 fields.

Preview all active tracker issues:

```sh
python3 scripts/kanban_cli.py --root /path/to/workspace --date YYYY-MM-DD
```

Sync a single transition after recording the new tracker stage:

```sh
python3 scripts/kanban_cli.py --root /path/to/workspace --date YYYY-MM-DD --issue '#42' --from-stage implement --commit
```

For an issue that has no stage yet, preview it and replace `--from-stage` with `--initialize`. A commit always names one issue. A completed task is excluded from the bulk preview, but can be selected by `--issue` to replace its numbered board stage with its terminal mark, such as `main`. If a board stage changed after the coordinator last observed it, the updater refuses the write and the coordinator reconciles it with the user. Existing `kind::`, `severity::`, and other unrelated labels survive every move.
