# Issue #1030: accept released implementation work when its request exists

## Goal and acceptance criteria

When the latest completed segment for the current `implement` stage has outcome
`human_review`, a human's release of the hold accepts the implementation if an
open request exists on that segment's issue work branch. Follow the configured
success edge, normally implement → review, without starting another implement
attempt. A successful request read proving there is no matching open request
keeps the existing implementation retry behavior. A hold that remains set still
waits for human review.

The request proves that an implementation work product exists; it does not grant
forge approval, establish a green pipeline, or satisfy review findings or merge
policy. Preserve those independent checks. This change recognizes the work
product of `implement`; an open request alone cannot accept a plan, review,
architect consultation, or arbitrary custom stage.

## Current behavior and available evidence

- `scripts/reconcile.py::_item` already handles active workers, closed issues,
  ambiguous latest segments, human holds, manual stage changes, and workflow
  gates. After checking that the latest segment matches the current stage, its
  `latest.outcome != "done"` branch retries every `human_review` segment.
- `evals/test_reconcile.py::test_human_review_segment_retries_current_stage_after_hold_is_cleared`
  codifies that unconditional retry. Retain its no-request expectation and add
  the request-present regression before changing behavior.
- `scripts/engine.py::run_pass` materializes current forge snapshots, synchronizes
  the store, collects workers and facts, then calls the pure planner. Snapshot
  fields can be omitted, and `scripts/store.py::sync_from_forge` preserves omitted
  values. A cached false hold value alone must not establish a fresh release.
- `scripts/worker.py` persists the branch in `resume.runs["<stage>-<attempt>"]`
  and the shared implementation branch in `resume.work_branch`.
  `scripts/forge_review.py::Request` already exposes `branch`, `closes`, and
  request identity for both GitHub and GitLab. `Forge.requests()` supplies open
  requests and raises on failed or partial reads.
- `review_state` is approval state; `merge_state` is a merge verdict. Neither is
  sufficient proof of an open request on the correct branch. Do not infer
  existence from the reported incident's `ready` string.
- Suite holds already have a separate explicit-publication/release lifecycle:
  `suite_hold_action`, `_sync_suite_holds`, and `Store.finish_suite_hold`. That
  path accepts completion by updating its recorded outcome. Leave its contract
  intact and avoid a second acceptance mutation for ordinary holds.

## Implementation sequence

1. **Add failing planner regressions.** Give the released implement segment
   explicit current hold observations and per-attempt request evidence. Assert
   that a matching request yields `Review(issue, next_review_attempt)` while a
   known absent request yields the next implement `Dispatch`. Keep negative
   tests for holds and non-implementation outcomes.
2. **Gather explicit release and product facts.** Extend `reconcile.Facts` with
   immutable plain records for the current observed hold flag and implementation
   work-product existence, keyed by issue, stage, and attempt where applicable.
   In `engine.run_pass`, pass the already materialized snapshots' hold
   observations into `engine_facts.gather`; omitted fields remain unknown, never
   false. A current explicit false plus a historical `human_review` segment is
   sufficient release evidence, including issues released before this fix; do
   not require a newly introduced persisted release marker.
3. **Verify the matching open request in the fact collector.** Only gather this
   evidence for relevant completed implement segments, and read the request
   list at most once per pass when candidates exist. Use the segment's recorded
   branch first. Support older records through a nonblank `work_branch` fallback
   only for the matching current implementation attempt; do not guess branch
   names from issue numbers or unrelated worktrees. Match the source branch
   exactly and verify the request is associated with the same local issue using
   existing closing-reference conventions. An unrelated issue's request or a
   request on another branch cannot accept this segment. Closed and merged
   requests do not qualify because they are absent from the open-request read.
   Preserve a three-way result: present, known absent, unknown. A failed read,
   malformed evidence, or unidentifiable branch is unknown, rather than absent.
   Isolate failures so other issues can still reconcile.
4. **Use the existing success route in the planner.** After existing hold,
   latest-segment, and stage-match checks, treat an explicitly released
   `human_review` implement segment with a verified work product as effectively
   complete for edge selection. Reuse `_next`, `_gate`, `_work`, and `_merge`;
   do not hardcode review dispatch or waive any gate. Known absence retries
   implement. Unknown request or hold observations wait with a concrete reason
   rather than launching duplicate work or advancing without evidence. Ordinary
   failed, blocked, quota, unknown, and outcome-less segments retain their
   existing routes. Unsupported stages retain their existing retry behavior.
5. **Keep acceptance derived and verify engine handoff.** Preserve the original
   ordinary segment/report outcomes and human-review history; no schema,
   migration, store mutation, new action type, or repair of production records
   is needed. Until the normal stage write succeeds, each pass can derive the
   same success route again. Once review starts, its segment and the updated
   stage prevent another implementation retry. Existing reservation, pending
   stage write, hold restoration, and launch failure handling remain in charge.

## Test plan

Use generic fixtures, temporary stores, fixed test clocks, and fake forge/launch
ports. No live issue or PR mutations are needed.

| Layer | Cases and required assertions |
| --- | --- |
| Planner | Released latest implement `human_review` + matching open request advances along the configured success edge; missing request retries implement; hold still present waits. Exercise an alternate success target and a gated edge so acceptance cannot bypass workflow policy. |
| Planner safety | Unknown or omitted hold/request observations wait; failed or blocked outcomes still retry; old implement segments cannot skip a later fix round; a changed current stage, active segment, closed issue, or tied latest stages keeps existing behavior. Open requests cannot turn a human-review review segment into done review evidence. |
| Fact collection | Exact branch and local issue match for GitHub-style and GitLab-style references; wrong branch/issue rejected; empty open list is known absence; draft, red, or running requests still prove existence independently of merge readiness; read exceptions and partial/malformed results remain unknown. Cover missing branch, safe legacy fallback, and stale attempts. Verify one request-list read for multiple eligible issues and no read when none qualify. |
| Engine | With a real temporary store and fake forge/launcher: observe hold set on completed implement-2, remove it, expose its open request, then assert review starts and implement-3 never does. Repeat with no request and assert implement-3 starts. Cover an already released historical segment, omitted hold field, request-read failure, and an unaffected sibling issue. |
| Handoff recovery | A second pass cannot launch duplicate work. A failed stage write or failed review launch follows existing recovery without returning to implement. Verify shared board labels and pending transitions still preserve the precise workflow stage. |
| Existing holds | Existing suite-hold acceptance and label-publication retry tests stay green, as do review-findings freshness, review retry, quota, launcher-owned holds, and merge-gate tests. |

Start with the new failing cases, implement the fact plumbing and predicate, then
run `python -m unittest evals.test_reconcile evals.test_engine_facts evals.test_engine`.
Before implementation completion run the repository suite:
`python -m unittest discover -s evals -p 'test_*.py'` in the engine dependency
environment (CI specifies Python 3.14 plus `requirements.txt`). Report the final
run's actual passed/failed/skipped/xfailed counts; never infer green from a zero
exit status or conceal skipped tests.

## Risks and boundaries

- **False acceptance:** cached status strings, omitted hold fields, wrong branches,
  stale segments, and unrelated requests must not establish acceptance. Bind
  observations to the current candidate and distinguish missing evidence from a
  successful empty read. The expected branch/request match is the work-product
  contract; do not add a pipeline-green condition to acceptance.
- **Legacy records:** a missing branch cannot be reconstructed safely from an
  issue title. Use the documented fallback or wait with an actionable evidence
  reason. No backfill or migration is part of this issue.
- **Races:** a request may close after facts are gathered. Existing review launch
  revalidation (`worker.launch` reads the closing request) and normal failed
  handoff recovery must contain that race; exercise it without inventing a new
  merge or stage-write protocol.
- **Scope creep:** no generic acceptance of all human-review outcomes and no
  automatic merge, production replay, worktree deletion, or architecture edits.
  Existing suite acceptance remains separate. The original outcome stays
  visible, while the success edge records progress through normal engine actions.
- **Read cost:** request enumeration can be expensive because it includes CI and
  thread evidence. Restrict the added read to eligible candidates and share it
  within a pass; do not make per-issue request API calls.

## Proposed implementation file ownership

| Mode | Repository-relative path | Purpose |
| --- | --- | --- |
| write | `scripts/reconcile.py` | Plain evidence records and released-work success predicate. |
| write | `scripts/engine_facts.py` | Branch-bound open-request evidence collection. |
| write | `scripts/engine.py` | Supply fresh snapshot hold observations to collection. |
| write | `evals/test_reconcile.py` | Planner regression and policy boundaries. |
| write | `evals/test_engine_facts.py` | Evidence matching, unknowns, and read isolation. |
| write | `evals/test_engine.py` | Release-to-review lifecycle and handoff recovery. |
| read | `scripts/forge.py` | Existing request selection and snapshot contracts. |
| read | `scripts/forge_review.py` | Request branch and closing-reference representation. |
| read | `scripts/forge_port.py` | Existing forge failure boundary. |
| read | `scripts/worker.py` | Branch provenance and review launch revalidation. |
| read | `scripts/store.py` | Segment, resume, sync, and suite-acceptance contracts. |
| read | `scripts/policy.py` | Workflow success edges and gates. |
| read | `scripts/completion.py` | Existing completion/hold boundaries. |
| read | `scripts/readiness.py` | Preserve dispatch readiness checks. |
| read | `evals/test_forge.py` | Existing fake forge fixtures. |
| read | `architecture/ARCHITECTURE.md` | Respect planner/driver and engine ownership boundaries. |
| read | `architecture/compliance.md` | Check documented constraints without editing architecture. |
| read | `CLAUDE.md` | Repository development and verification rules. |
| read | `requirements.txt` | Engine test dependencies. |
| read | `.github/ci/Dockerfile` | Supported full-suite test environment. |

## Plan-stage verification

This stage changes only this plan; no regression implementation is included.
The initial system-Python test attempt passed the 92 planner tests but could not
import the two engine modules because that interpreter lacks Alembic and
SQLAlchemy. Login/interactive shell resolution and the supervisor's environment
selection were checked; baseline verification was then rerun with the existing
engine virtual environment.

Final baseline command:
`/Users/locriani/.claude/plugins/data/chief-of-stuff-chief-of-stuff/venv-cc2edd13350e11bd.current/bin/python -m unittest evals.test_reconcile evals.test_engine_facts evals.test_engine`

Result: 352 passed, 0 failed, 0 skipped, 0 xfailed (42.465 seconds). The full
repository suite was not run for this documentation-only planning stage; it is
required above for the subsequent implementation. These baseline results verify
the inspected code, not the proposed behavior, whose regression tests are future
implementation work.
