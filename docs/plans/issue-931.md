# Plan: issue #931 — one history step per kept tree, not one per pass

## Source caveat (read first)

This plan was written from the issue's quoted code (`scripts/cleanup.py:266-270`, installed 0.129.98) and from `scripts/cleanup.py` at commit `03323001`. The planning worktree is based on `main` at 0.128.67, which has no `scripts/cleanup.py` at all, so the code could not be read at 0.129.98. The implementer starts from a base that has `cleanup.py` with the dedup block and re-confirms the line numbers and the `claims()` text before editing.

## Cause

`claims()` returns one string per live process: `live process in tree (pid N)`. `main()` joins every claim into the keep line and logs it unless it equals the previous keep line exactly. A worker that spawns shells changes the pid set between passes, and with it the length, so `clean()` truncates at a different place; no two keeps match.

## Change

1. `scripts/cleanup.py` `claims()`: replace the per-process list comprehension with one entry, added only when at least one other process has its cwd in the tree: `"live process in tree"`. The keep line then names the reason, not the pids, and the existing exact-match dedup holds with no change to the dedup block.
2. Leave the dedup block, `clean()` and the store alone. No schema change and no migration: the 255 existing rows stay, the first pass after the upgrade writes one new line, and later passes match it.
3. The other pid-bearing claims (`live worker NAME (pid N)`, `live one-shot (pid N)`) stay: the pid of a running worker or one-shot does not change while it runs, so they are already stable.
4. Not done: normalising pids inside the dedup comparison with a regex. It would leave the variable-length run-on list and the truncation, and would hide pids only from the comparison; collapsing at the source is smaller and fixes the line shown on the page.

## Tests (`evals/test_cleanup.py`, same real-origin and clone fixtures)

- Two `--remove` passes over one tree, with a different set of processes in the tree each time (patch `cleanup.processes`): exactly one `keep ` row in the store for the issue, whose text contains `live process in tree` and no `pid`.
- A tree with several live processes yields `live process in tree` once, not once per process.
- Reason change: after the process-only keep, the same tree with the process gone but an uncommitted file adds a second row (new reason, new step).
- Update any existing assertion that matches `live process in tree (pid`; grep the whole repo (`evals/`, `scripts/`, `docs/`, `architecture/`) for it before editing.
- Run the full `evals` suite. `require_green_tests` needs passed > 0 and failed, skipped and xfailed all 0.

## Risks

- Operators lose the pids from the printed and logged line. They are still in `lsof`, and the issue accepts it ("pids, if shown at all").
- The counts in the git guards (`2 uncommitted`, `2 unsaved by git status --ignored`) still change as a worker commits or edits, so a progressing worker can still add a step when those counts move. The issue's expected behaviour names the reason changing, not the counts; if the board crawl still shows churn after this lands, normalise the counts in the guard text in a follow-up rather than widening this change.
- The architect owns `ARCHITECTURE.md` and `compliance.md`; the implementer does not edit them. If they quote the keep text, the implementer says so in the report.

## Logic

```
P := a live process is in the tree           W := the keep line equals the previous keep line
R := the set of keep reasons is unchanged    S := a new history step is written
N := pids appear in the keep line

1. S ↔ ¬W                                    [cleanup.py:266-270 dedup]
2. today: N ∧ pids vary between passes → ¬W  [issue Observed; claims() emits "(pid N)" per process]
3. fix: P → "live process in tree" once, ¬N   [change 1]
4. ¬N ∧ R → W                                [the line is built only from reasons]
∴  with R holding, S is false: one step until the reason changes
```

## File ownership proposed for the implementation

- `scripts/cleanup.py`: write
- `evals/test_cleanup.py`: write
