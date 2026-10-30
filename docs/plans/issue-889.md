# Issue #889: verify repository ownership before linking decision file references

## Outcome and scope

Render workspace file references as escaped text unless the renderer can establish that the referenced path belongs to the configured forge repository. For the reported case, the References card must show `chief-of-stuff.toml:54` and its label without an anchor to the issues-only tracker project. Use the issue's permitted plain-text fallback; do not add a source-serving endpoint or expose workspace file contents.

This stage changes only this plan. Implementation will change `scripts/decision_page.py`, `evals/test_decision_page.py`, and `evals/test_pages.py`.

## Findings in the current code

- `scripts/decision_page.py:143` defines `Linker`. Its `_url` uses only the `Backlog:` forge, and `resolve` constructs blob/tree URLs for syntactically recognized paths without verifying repository ownership or committed path existence.
- `render` constructs both a shared inline linker and a linker for each References row, including the row's optional `at` revision. Both call `resolve`, so a correction there covers plain references, backticked references, and whole-row anchors.
- `_find` checks workspace containment before reading a section or symbol, but that only locates a fragment; it does not establish that the forge contains the file.
- Production `write` and `render_all` supply the workspace root. Existing `LinkTest` cases assume code links can be fabricated without a root, and section/symbol tests use ordinary folders rather than repositories. These fixtures must change with the contract.
- `scripts/pages.py` already invalidates rendered pages when renderer Python files change. Its existing decision route calls the renderer; no production route change is needed.
- `scripts/git_view.py` supports ordinary clones and linked worktrees. `scripts/git_trees.py` provides bounded, isolated Git reads. The sanitized Git view deliberately replaces remote URLs with `/nonexistent`, so it cannot establish the real origin URL.

## Implementation

1. Add failing regression tests first. Use a temporary, non-Git workspace with a generic GitLab backlog such as `https://forge.example/team/tracker`, a local settings file, and a decision reference to `chief-of-stuff.toml:54`. Assert the References card retains the path, line, kind and descriptive label, contains no anchor for that reference, and contains no tracker blob URL. Cover the same value in inline text and a code span.

2. Add a small repository-ownership check inside `decision_page.py`, shared by every path branch in `Linker.resolve`:
   - Interpret paths relative to the workspace root. Refuse paths escaping the root, including symlink escapes. When there is no root, ownership is unknown and file references remain text.
   - Locate the nearest owning Git repository at or below the workspace root, respecting nested repositories and `.git` files for linked worktrees. Do not inherit an unrelated repository above the workspace, recursively search clones, or guess a repository from a basename or `[graph]` settings.
   - Read the owning repository's actual `origin` URL as bounded local configuration data. Use `git_view.locate` to obtain the common Git directory and an explicit-file Git config read with includes disabled, an isolated environment, argv arguments and a timeout. Check the config is a bounded regular file before parsing. Do not run normal repository-configured commands to obtain the remote; do not use the sanitized view's replacement URL.
   - Normalize ordinary HTTPS, SSH URL and SCP-style SSH origins, including a trailing `.git`, and compare host/project identity with the configured GitHub or GitLab backlog. Reject unsupported, ambiguous, local filesystem or mismatching origins. Never copy credentials from an origin into HTML or diagnostics. For this fix, a different source repository remains text; automatically linking other repositories is outside scope.
   - Derive the path relative to that repository, stripping workspace clone-directory prefixes. Verify its exact committed blob/tree entry at the reference's `at` revision, or `HEAD` when unpinned, through the existing isolated Git read helpers. An untracked/index-only file, missing commit/path, submodule entry, unreadable repository or failed Git probe yields no link. A directory needs a committed tree, not merely an existing folder. Use literal path matching and safe revision operands.
   - Keep positive and negative lookup results only for the current render and share them with pinned row linkers. Avoid a process-global cache or a subprocess per repeated token; do not fetch, contact the forge, or write repository state.

3. Gate both blob and tree URL construction on that check. Return `None` for an unplaceable path and let the existing escaped-text/code-span fallback render it. Preserve the original displayed workspace path even when the URL uses the repository-relative path. Keep section/symbol reads constrained to the verified file, and preserve GitHub/GitLab line-range formats, explicit anchors and revision pins for verified references. URL-encode path components and revision values without changing path separators or fragment semantics.

4. Keep issue, PR/MR, note, commit and pipeline references using their existing backlog rules. Explicit HTTP(S) links remain explicit links. Do not add a settings-file special case, suppress all code references, change the decision JSON schema, or edit workspace data.

5. Update the `Linker` and module documentation to state the ownership requirement and plain-text fallback. Convert existing positive path-link tests to real temporary repositories with committed files and recognized synthetic origins. Keep fixtures generic and use existing `evals/make_repo.py` helpers; set a synthetic remote after local fixture setup so tests make no network calls.

## Tests and acceptance criteria

Add or update focused tests in `evals/test_decision_page.py` for:

- The reported non-Git workspace/settings-file case in a full rendered References card, including its descriptive label; repeat through inline and backtick rendering.
- An untracked file inside a valid repository; a directory with only untracked contents; missing paths; no root; no origin; unknown/mismatching origin; invalid revision; Git errors/timeouts. All remain readable text without fabricated forge URLs or rendering failures.
- Verified GitHub and GitLab files, line ranges, directories, document anchors, sections and symbols; nested clone prefixes are removed from URL paths. Existing issue/PR/MR/note/commit/pipeline and explicit URL behavior remains intact.
- An HTTPS origin, an SSH URL origin, an SCP-style origin and a linked worktree; a repository above the workspace and an unrelated nested repository cannot supply false ownership.
- A pinned reference uses the committed path at that revision, including a path absent from the current checkout; a path absent at the requested revision cannot link just because it exists at HEAD. A staged addition without a committed entry also remains text.
- Traversal/symlink escapes and untrusted origin text cannot produce external-path links, leak credentials or inject HTML. Unsupported absolute paths remain text without linking a matched suffix.
- Repeated references share ownership lookups within one render; a later render observes changed repository facts rather than a permanent cached result.

Add one route-level regression to `evals/test_pages.py` using its temporary pages-server fixture: GET a decision with the workspace settings reference and an ordinary issue reference. Assert HTTP 200, readable settings path/label, no tracker blob anchor, and the correct issue anchor. This verifies the path used by the reported decision URL; no real server/workspace or forge credentials are needed.

Validation order for implementation:

1. Run the new regression tests before changing production code and confirm failure on the fabricated blob URL.
2. Run `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s evals -p 'test_decision_page.py'`.
3. Run `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s evals -p 'test_pages.py'`.
4. Run the repository-required suite: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s evals -p 'test_*.py'`. Record actual passed, failed, skipped and expected-failure counts; a green engine report requires passed > 0 with the other three counts zero.
5. Review the diff and confirm only the three implementation-owned files changed. Commit small meaningful changes and never merge.

## Planning-stage validation

- Existing decision-page suite: 86 tests passed, zero failures, skips or expected failures.
- Final baseline suite run, `test_pages.py`: 71 tests ran; 69 passed and two distinct tests errored, with zero skips or expected failures. `EnsureTest.test_an_older_server_is_restarted` and `EnsureTest.test_an_older_server_is_stopped_only_by_its_own_pid` hit `PermissionError: Operation not permitted: ps`; the first also produced a cleanup `ProcessLookupError`, giving three unittest error entries across the two tests.
- Checked command resolution in the configured interactive login shell: `ps` resolves to `/bin/ps`, and invoking it there is also sandbox-denied. This is an execution restriction, not a missing tool or authentication issue. The session prohibits escalation, so these baseline errors remain recorded and the full suite was not run during planning. Implementation needs validation in an environment that permits process inspection.

## Risks and boundaries

- Conservative verification intentionally turns unverifiable repository-relative references into text, including references with no local clone or with a different origin than the backlog. This is preferable to emitting a guessed tracker URL. Recognized same-repository references retain their links through real repository fixtures.
- Origin identity and local committed objects establish a plausible repository target, not remote availability or access permissions. This fix does not promise to detect deleted/private remote repositories or unpublished commits through online probes.
- Git reads add render work. Bound them, reuse results per render and fall back to text on failure. Existing safe-view helpers protect object reads; their rewritten remote configuration must not be mistaken for the real origin.
- No new file-serving route means no new workspace content exposure. Do not expand the server's render-source dependency graph for Git metadata in this issue; a newly rendered page rechecks ownership, and existing renderer invalidation covers deployment of this fix. Live refresh solely on origin/commit changes is a separate concern.

## Proposed implementation file ownership

| Path | Mode | Purpose |
| --- | --- | --- |
| `scripts/decision_page.py` | write | Ownership verification, shared linker behavior and documentation. |
| `evals/test_decision_page.py` | write | Regression cases and verified repository-link fixtures. |
| `evals/test_pages.py` | write | Decision-route regression. |
| `scripts/git_view.py` | read | Repository layout and safe-read behavior. |
| `scripts/git_trees.py` | read | Isolated bounded Git helpers. |
| `scripts/backlog_ref.py` | read | GitHub/GitLab repository identity. |
| `scripts/fragment.py` | read | Existing escaped link rendering. |
| `scripts/pages.py` | read | Decision routing and renderer invalidation. |
| `evals/make_repo.py` | read | Real local Git fixtures. |
| `CLAUDE.md` | read | TDD and required suite instructions. |
| `docs/plans/issue-889.md` | read | Approved implementation plan. |
