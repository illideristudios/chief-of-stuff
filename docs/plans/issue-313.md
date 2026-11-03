# Issue 313 implementation plan

## Objective and stage scope

Make `models-rotation-out-of-quota` reliably enforce the intended dispatch: notice the Log's quota restriction, advance from `claude:haiku@high` with `models --class implement --after claude:haiku@high`, launch `claude:opus@low` with its effort, and record the selected entry and reason. Preserve the related first-entry case's requirement to use the configured model ID verbatim.

This stage changes only this plan. No implementation, fixture, grader, or skill changes are part of this commit. The assignment's existing implementation ownership (`evals/run.py`) should be replaced by the ownership proposal below. The earlier draft at `docs/specs/issue-313-rotation-quota-plan.md` is context; this plan specifies the implementation and validation to perform.

## Findings verified in this worktree

- The quota fixture has the implement rotation `claude:haiku@high`, `claude:opus@low`, `claude:sonnet@medium`. Its tracker explicitly says that Claude Haiku is out of quota until tomorrow.
- `scripts/settings.py` already implements advancing through `pick(..., after=...)`. `scripts/spawn_session.py` selects `settings.models[args.model_class][0]` for a class launch with neither explicit model nor runtime. Thus `worker --class implement` selects Haiku again; an earlier `models --after` call does not mutate that selection.
- `skills/dispatch/SKILL.md` offers `worker --class` as an alternative to launching the printed entry, while separately directing quota fallback. Its general exact-model verification guidance can also encourage expanding a configured alias. Tighten the rotation-specific guidance so the fallback uses the printed runtime, model ID, and effort verbatim.
- `evals/run.py::_tool_used` intentionally searches `json.dumps(tool_input)` and counts denied calls as attempts. The rotation graders' unbounded lookaheads can consume the description or a later shell command. The effort case already has a tested command-bounded pattern; use that local precedent rather than changing the shared runner contract.

Direct regex probes against the current quota case produced these results (a match on the forbidden-entry grader fails the case):

| Command / description | Matches next-entry launch | Matches forbidden-entry launch |
| --- | --- | --- |
| `worker --dry-run --model opus --effort low`, empty description | yes | no |
| `worker --dry-run --model "opus" --effort "low"`, empty description | no | no |
| Unquoted Opus command above, description `Avoid --model haiku` | yes | yes |
| `worker --dry-run --class implement`, empty description | no | yes |
| `worker --dry-run; echo --model opus --effort low`, empty description | yes | no |
| `worker --dry-run --model claude-haiku-4-5 --effort high`, empty description | no | no |

The issue's original transcript is not supplied, so these findings establish grader defects and ambiguous guidance, not which defect caused each reported call. A grader correction cannot manufacture the missing `--after` or Log update.

## Implementation sequence

1. Add failing regression tables to `evals/test_cases.py`, next to the effort grader tests. Exercise the actual case JSON through the same JSON serialization and tool matching used by `_tool_used`; include misleading descriptions and multiple launches in one Bash call. First demonstrate that the new tests fail against the current rotation patterns.
2. Correct the Bash graders in both rotation case JSON files. Reuse the effort case's convention for quoted arguments and command boundaries (`;`, `&`, `|`, newline, and the JSON command-string boundary). Flags must belong to the same actual invocation. A description, quoted task prose, `echo`, or a subsequent command must not supply them. Keep matching the supported CLI and Python launcher forms. Do not add a general shell parser or change `evals/run.py`.
3. Clarify `skills/dispatch/SKILL.md`: class-only worker launches select the first entry; after quota fallback, launch the entry returned by `models --after` with explicit runtime, verbatim model ID, and effort. A compact wording is to end the choosing sentence at `effort included.`, add `Pass --class <class> only to launch that first entry.` (with the existing inline-code formatting), append `, never with --class` to the fallback sentence, and change `with the model ID verbatim` to `with that entry's model ID verbatim`.
4. Update every corresponding `rule` quotation in the two rotation cases, the effort case, `evals/test_cases.py`, and `DISPATCH_RULES` in `evals/test_agent_budget.py`. Keep sentences byte-identical to the skill, including punctuation and backticks. Keep the existing byte ceilings: dispatch is currently 14,354 bytes against 14,400. The three suggested replacements total +41 bytes; verify the actual result, shortening wording if needed rather than raising ceilings for this fix.
5. Run deterministic checks, then replay the live cases and inspect their tool transcripts. Commit small, coherent changes; never merge. A plan-stage baseline pass does not establish that the implementation or live case passes.

## Grader behavior and regression coverage

For quota advancement, require a `models` invocation for class `implement` with `--after` naming the configured Haiku entry (with or without its effort, as `pick` permits). Accept ordinary unquoted, single-quoted, double-quoted, and `--flag=value` spellings. Reject a different class or entry and flags supplied outside that invocation.

For the quota next-entry launch, require a dry-run worker invocation with model `opus` and effort `low`. An explicit runtime must be `claude`; omitting it is valid because the launcher defaults to Claude. Class-only dispatch cannot satisfy this grader. Missing or wrong effort, another model, another explicit runtime, and a full expanded Opus ID cannot satisfy the configured-entry requirement.

For forbidden quota launches, catch Haiku launches, including the observed expanded spelling `claude-haiku-4-5`, and class-only implement launches that actually select the fixture's first entry. Detect forbidden attempts even when another valid Opus launch occurs in the same Bash call. Explicit model/runtime flags disable class selection in the launcher; a class argument alone must not make an explicit Opus launch look like Haiku. Do not build a general provider alias registry: retain the fixture's exact positive model requirement and cover the observed expanded Haiku spelling explicitly.

For first-entry dispatch, retain `haiku` with effort `high`, or `--class implement` with no explicit runtime/model and no conflicting effort. Accept quoted values without treating them as other models. Keep `claude-haiku-4-5` rejected as a non-verbatim ID; it must fail the positive launch grader and trigger the existing other-model prohibition. The issue's related failure is addressed by guidance to preserve the configured ID, not by relaxing the configuration contract.

Regression tables should cover:

- Correct and incorrect advancement, including quoted full entry values and `=` syntax.
- Correct Opus/low and Haiku/high launches with reordered flags and quoted values; missing/wrong effort and runtime.
- Class-only selection versus explicit model/runtime overrides; an explicit effort must not contradict the selected entry.
- Descriptions mentioning every relevant flag, both before and after the command key in serialized input; descriptions containing a complete fake launch.
- A valid and a forbidden launch in one call; flags distributed across commands separated by `;`, `&&`, `|`, and newlines; separators inside quoted task text.
- Flags and launcher text inside quoted task prose or `echo` that must not become a launch. Model/effort suffixes must not pass as exact IDs or values.
- The observed expanded Claude model IDs: never satisfy the verbatim-entry positive graders, and expanded Haiku remains forbidden in the quota case.
- Python launcher spellings already supported by the cases. Preserve the effort case's documented ceiling for unclosed quotes; do not claim full shell parsing.

Keep the Log grader requiring an Opus entry and quota/usage-limit reason, the configuration-unchanged grader, and dispatch-skill-read ordering. Do not alter the quota fixture to teach the answer, make `worker --class` remember a prior CLI query, or teach the launcher to interpret prose in the Log. If live replay exposes another defect, inspect that transcript before changing scope or assertions.

## Validation and acceptance

During implementation, run the new regression tests red first, then make them pass. Run:

```text
env -u CHIEF_OF_STUFF_WORKSPACE python3 -m unittest discover -s evals -p 'test_cases.py'
env -u CHIEF_OF_STUFF_WORKSPACE python3 -m unittest discover -s evals -p 'test_agent_budget.py'
env -u CHIEF_OF_STUFF_WORKSPACE python3 -m unittest discover -s evals -p 'test_settings.py'
env -u CHIEF_OF_STUFF_WORKSPACE python3 -m unittest discover -s evals -p 'test_*.py'
```

Unsetting `CHIEF_OF_STUFF_WORKSPACE` prevents a worker environment from substituting the real workspace into isolated CLI fixture tests. Record passed, failed, skipped, and expected-failure counts from the completed suite; investigate environmental failures rather than silently counting them as green.

After deterministic checks pass, replay with Sonnet (the Claude harness default):

```text
python3 evals/run.py --runtime claude --arm agent --runs 2 --case models-rotation-out-of-quota
python3 evals/run.py --runtime claude --arm agent --runs 2 --case models-rotation-first-entry
```

Require both runs of each case green. Inspect actual command inputs to confirm advancement, the explicit Opus/low fallback, no Haiku attempt, the Log explanation, and unchanged TOML. Preserve output/transcript locations in the implementation report. If a replay is unavailable, report that limitation explicitly; passing unit tests alone is not proof the model now follows the fallback rule. For any CLI access problem, check resolution/configuration in the user's configured interactive login shell without printing secrets before concluding installation or authentication is needed.

Plan-stage validation completed on the unchanged implementation: one combined unittest suite discovering `test_cases.py`, `test_agent_budget.py`, and `test_settings.py`, with `CHIEF_OF_STUFF_WORKSPACE` unset, passed **174**, failed **0**, skipped **0**, xfailed **0** (3.159 seconds). The full Python suite and live model replays were not run in this planning stage. The diagnostic probes above describe current defects; they are not passing regression tests for a fix.

## Proposed implementation file ownership

Write:

- `evals/cases/models-rotation-out-of-quota/case.json`
- `evals/cases/models-rotation-first-entry/case.json`
- `evals/cases/models-effort-whatever-the-runtime/case.json` (rule quotations only)
- `evals/test_cases.py`
- `evals/test_agent_budget.py` (sentence expectations only; retain ceilings)
- `skills/dispatch/SKILL.md`

Read:

- `evals/run.py`
- `scripts/spawn_session.py`
- `scripts/settings.py`
- `evals/test_settings.py`
- `evals/cases/models-rotation-out-of-quota/fixture/chief-of-stuff.toml`
- `evals/cases/models-rotation-out-of-quota/fixture/daily/{{today}}-tracker.md`
- `evals/cases/models-rotation-first-entry/fixture/chief-of-stuff.toml`

## Risks and limits

- Model behavior is probabilistic: the supplied failures show absent actions, which regex fixes alone cannot resolve. Live replay is required to validate the wording change.
- JSON-escaped shell regexes are easy to broaden incorrectly. Review negative rows as carefully as positive rows and preserve every forbidden launch as a failure even alongside a valid launch.
- Expanding aliases would weaken the configured-entry contract. Preserve verbatim positive matching while preventing the observed expanded Haiku spelling from bypassing the quota prohibition.
- Skill headroom is only 46 bytes before edits. Stale rule quotations or a few extra bytes can fail the sentence-integrity and budget tests. Keep the wording change compact and synchronized.
- The original Sonnet transcript is unavailable here. Further fixture or runner changes require evidence from replay and an updated ownership proposal rather than assumptions about that historical run.
