# Issue 1113: Share latest-segment selection and tie detection

## Outcome and current behavior

Extract one pure helper in `scripts/reconcile.py` and call it from both
`reconcile._item` and `engine_facts._implementation_requests`. Preserve the
planner's existing behavior: the latest start wins, the highest attempt wins
within that start, and different stages sharing the latest start are ambiguous
even when their attempt numbers differ.

Currently `_item` selects with `(_time(started_at), attempt)` and checks distinct
stages at the parsed latest timestamp. `_implementation_requests` repeats that
selection but checks ties with raw `started_at` equality. Equivalent instants
encoded with different timezone offsets can therefore disagree: the planner
waits while the fact collector may gather request evidence. The shared helper
must use `_time` for both ordering and tie detection.

## Implementation

1. Add public `latest_segment(completed: Sequence[Segment]) ->
   tuple[Segment | None, bool]` near `_time`. The arguments are already eligible,
   completed segments. Return `(latest, ambiguous)`, with `(None, False)` for an
   empty sequence. Choose `latest` by `(_time(s.started_at), s.attempt)`; set
   `ambiguous` when more than one stage has the same parsed start as `latest`.
   A larger attempt never resolves a cross-stage tie. Keep the selected original
   segment, do not mutate or reorder the input, and perform no I/O. Preserve
   `_time`'s timezone validation and existing errors rather than inventing a
   fallback for malformed timestamps. Exact duplicate keys retain `max`'s
   existing first-match behavior; do not introduce another ordering rule.
2. In `_item`, retain its existing `completed` filter, call the helper, and use
   the ambiguity flag for the existing wait reason (including its human-review
   variant). Remove the local `max` and distinct-stage tie calculation.
3. Import the public helper alongside `Facts` in `engine_facts`. Retain its
   existing `completed` filter, call the helper, and reject ambiguous candidates
   before any request read. Remove its local `max` and raw timestamp tie check.
   Preserve the stage/outcome checks, branch provenance, current-attempt legacy
   fallback, and absent/unknown/present request evidence semantics.

Eligibility stays with callers because they have different context: `_item`
includes architect when it is a workflow stage and ignores completed merge
segments while at merge; request gathering excludes architect. Both continue to
block on running segments before selecting completed work. This extraction
centralizes selection and ambiguity without adding workflow arguments or
changing those policies. Leave `latest_review` and the separate prior-work
selection for the architect-to-merge return route unchanged; neither is the
copied request-evidence rule targeted by this issue. No store migration,
configuration, dependency, or architecture-document change is needed.

## Tests first

Make the red test change before adding the helper or changing either caller;
record the failing command and assertion in implementation evidence. Keep this
planning stage limited to this document.

In `evals/test_reconcile.py`, add direct helper tests using its existing segment
fixtures:

- Empty input returns `(None, False)`; a single candidate returns that object.
- A newer start wins over an older start with a larger attempt, in either input
  order. At equal starts within one stage, the higher attempt wins without
  ambiguity, in either order.
- Different stages at the latest start are ambiguous, including unequal
  attempts and reversed input order. Cross-stage ties only at an older start do
  not poison a unique newer segment.
- A datetime and ISO strings with different offsets representing the same
  instant are tied. Include same-stage and cross-stage cases, and retain
  rejection of naive timestamps through `_time`.

These tests must fail against the missing helper before implementation. Also add
a behavior regression in `evals/test_engine_facts.py` before editing production
code: record an implementation `human_review` segment at attempt 2 and a review
segment at attempt 1 with differently encoded but equivalent aware starts.
Using the existing release fixtures, expect no `implementation_requests` entry
and no forge request call. The current raw equality check should incorrectly
collect implementation evidence, making this an independent behavioral red
test. Verify the test fixture preserves the distinct timestamp encodings so it
actually exercises that defect.

After adding the helper and wiring both callers, add or extend integration
coverage for a same-stage tied start selecting the higher implementation
attempt and its recorded branch, and a unique newer start winning over a larger
older attempt. Extend the existing planner tie tests with equivalent-offset
timestamps to verify it still waits. Retain existing coverage for running work,
architect workflow stages versus consultations, merge selection, released holds,
unknown evidence, and branch provenance. Prefer behavior assertions over mocks
that merely assert that the helper was called.

Run the focused new tests red, then green. Run both complete affected suites:

```sh
python3 -m unittest evals.test_reconcile evals.test_engine_facts
```

Then run the repository's CI command in its supported dependency environment
(CI uses Python 3.14 and `requirements.txt`):

```sh
python3 -m unittest discover -s evals -p 'test_*.py'
```

Capture actual passed, failed (including unittest errors), skipped, and xfailed
counts from the final run. Green requires passed > 0 and all other counts zero;
do not infer missing counts or conceal an environment failure. Keep commits
small, record the red result before the green implementation, and never merge.

## Risks and validation

Planning baseline: both affected suites passed unchanged using the engine's
configured dependency virtual environment: 131 passed, 0 failed, 0 skipped,
0 xfailed. The system Python could not import `alembic`; rerunning through the
existing engine environment resolved that setup issue. The full repository
suite and the new red/green tests remain implementation-stage validation.

- Attempt precedence can accidentally override cross-stage ambiguity. Direct
  tests must cover a higher attempt in one of two simultaneous stages.
- Raw string comparison can silently return in one caller. Equivalent-offset
  integration cases pin both decisions to the same normalized timestamp rule.
- Moving caller eligibility into the helper could change architect, merge, or
  running-work behavior. Keep the filters and early running checks in place and
  run the existing integration suites.
- The helper exposes a selected candidate even when ambiguous. Both callers
  must check the flag before advancing or gathering request evidence.
- This is a behavior-preserving extraction except for aligning request-gathering
  timestamp equality and validation with reconcile. Preserve conservative
  evidence handling and avoid unrelated cleanup of other selection functions.

## Proposed implementation file ownership

- write: `scripts/reconcile.py`
- write: `scripts/engine_facts.py`
- write: `evals/test_reconcile.py`
- write: `evals/test_engine_facts.py`
- read: `scripts/store.py`
- read: `requirements.txt`
- read: `.github/workflows/tests.yml`
- read: `.github/ci/Dockerfile`
