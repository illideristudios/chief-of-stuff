# Issue 313: models-rotation-out-of-quota stays red

Plan for [illideristudios/chief-of-stuff#313](https://github.com/illideristudios/chief-of-stuff/issues/313). The case is red on main with Sonnet because the recorded run never advances the implement rotation off `claude:haiku@high`. This worktree is `50091f80`. No product code changes in this plan.

## What is wrong

Two defects, and either one reddens the case. The 2026-10-06 transcript is not in the repo, so the plan does not pick which one that run hit. The counts were: `--after` 0 (want ≥ 1), next-entry launch 0 (want ≥ 1), out-of-quota launch 2 (want 0), and no Log line naming both opus and quota. The same family reddened `models-rotation-first-entry` once in four runs when the worker was passed `--model claude-haiku-4-5` and the grader wanted `--model haiku`.

`evals/run.py` `_tool_used` searches `json.dumps` of the whole tool input, description included. The rotation `input_match` patterns are bare lookaheads. The effort case already stopped doing that, and its test says the description decides nothing (`evals/test_cases.py`, `test_effort_graders_judge_each_worker_launch`).

Confirmed against the current patterns, searching the JSON the way `_tool_used` does:

| Command | Description | Advances `--after` | Launches opus@low | Counted as the haiku launch |
|---|---|---|---|---|
| `models --class implement --after "claude:haiku@high"` | mentions `--model haiku` | pass | — | no |
| `worker --dry-run --model opus --effort low` | `do not --model haiku` | — | pass | yes |
| `worker --dry-run --model "opus" --effort "low"` | `do not --model haiku` | — | fail | yes |
| `worker --dry-run --class implement` | empty | — | fail | yes |
| `worker --dry-run --model claude-haiku-4-5 --effort high` | empty | — | fail | no |

A correct quoted opus launch whose description mentions the forbidden flag is graded as launching haiku and as not launching opus. A real `--class implement` launch is also graded as launching haiku, which matches the launcher: `scripts/spawn_session.py` takes `rotation[0]` when `--class` is set and `--model` / `--runtime` are absent (`entry = settings.models[args.model_class][0]`). On this fixture that entry is `claude:haiku@high`.

The dispatch skill offers that shortcut in the same section as the quota rule, and it tells the model to verify an exact id through the CLI. `skills/dispatch/SKILL.md` is 14354 bytes under a 14400 ceiling (`evals/test_agent_budget.py`, `SKILL_CEILING`), so 46 bytes of room.

```
Q := the run called models --after for the haiku entry
G := the launch graders accept a quoted verbatim command and ignore the Bash description
S := the skill still equates worker --class with the printed entry, and still says to verify an exact CLI id
C := worker --class with no --model launches rotation[0]

1. ¬Q                         [issue: 0 --after calls]
2. C                          [spawn_session.py takes rotation[0]]
3. S                          [dispatch skill, same section as the quota sentence]
4. □(green → G)               [quoted opus + a description that says --model haiku is red today]
5. G ↛ Q                      [a grader fix does not create the missing call]
∴  □G ∧ □¬(S when the Log says the first entry is out of quota)
```

Correct the graders, and stop authorizing the shortcut and the expanded id when `[models]` has already named the entry. Neither half alone makes the case green.

## Implementation

Leave `evals/run.py` alone. The JSON search is the contract the effort graders test. Copy their command atom into the rotation patterns: step over a quoted argument whole, stop the command at `;`, `&`, `|`, or a newline, and do not let the description match.

Out of quota (`evals/cases/models-rotation-out-of-quota/case.json`), three Bash graders:

- Advances: a `models` command with `--class implement` and `--after` whose value contains `haiku`, quotes allowed.
- Launches the next entry: a `worker` or `spawn_session.py` command with `--dry-run`, `--model opus`, and `--effort low`, quotes allowed. `--model claude-opus-4-5` does not count. The id in the rotation entry is `opus`.
- Never launches the out-of-quota entry: the same launch shape with `--model haiku`, or with `--class` and no `--model`. A description that says `--model haiku` does not count. `--model claude-haiku-4-5` does not count as haiku and does not count as opus.

First entry (`evals/cases/models-rotation-first-entry/case.json`): the same atom. `--model haiku` and `--model "haiku"` with `--effort high`, and bare `--class implement`, still satisfy the launch. `--model claude-haiku-4-5` still fails the launch and still hits "launches no other model".

Do not change the Log grader. It already wants a tracker line that names opus and quota or a usage limit. The fixture line about haiku does not satisfy it. Do not change `file_unchanged` on `chief-of-stuff.toml`. Do not teach `spawn_session.py` to read the Log. Quota is the coordinator's fact.

Skill replacements in `skills/dispatch/SKILL.md`, net +41 bytes, new size 14395, ceiling 14400. Add nothing else. If the file would pass 14400, shorten a replacement. Move `SKILL_CEILING["dispatch"]` and `SKILLS_TOTAL` only if the ceiling itself has to move, and then by the same 50-byte step the comment already uses.

- Replace the choosing sentence's tail `, or pass `--class <class>` to the worker call.` so the sentence ends at `effort included.` and a new sentence reads `Pass `--class <class>` only to launch that first entry.`
- Extend the quota sentence with `, never with `--class``.
- In the launch sentence, replace `with the model ID verbatim` with `with that entry's model ID verbatim`.

Those three strings are the `rule` values quoted by the rotation Read graders and by every effort grader that quotes the launch sentence. Update them together in the case files, in `evals/test_cases.py` (`expected` for the dispatch Read graders), and in `DISPATCH_RULES` in `evals/test_agent_budget.py`. A sentence listed there has to occur once in the skill.

## Tests

TDD. The new table fails on today's patterns. Land it first, then the pattern and sentence edits.

Add `test_rotation_graders_judge_the_command_not_the_description` in `evals/test_cases.py`, beside the effort table. Build the hit the way that test does: `json.dumps({"command": command, "description": said})` with `said` naming `--model haiku`, `--effort low`, and `--class implement`. One row per command, three booleans for the out-of-quota Bash graders (advances, launches opus@low, counted as the haiku launch) and, for the first-entry rows, the two launch booleans.

Cover at least:

- Unquoted and double-quoted `--after claude:haiku@high` on `models --class implement`: advances, and is not a haiku launch.
- Unquoted and quoted `--model opus` / `--effort low` with `--dry-run`: launches the next entry, and the description does not make it a haiku launch.
- `--class implement` with `--dry-run` and no `--model`: a haiku launch, not the next entry. This stays the behavior of `rotation[0]`.
- `--model haiku`: a haiku launch.
- `--model claude-haiku-4-5` and `--model claude-opus-4-5`: neither the next entry nor the verbatim haiku launch.
- A `models` command is not a worker launch.
- The effort-case ceiling still holds: an unclosed quote hides the flags after it.

Then `python3 -m unittest discover -s evals -p 'test_*.py'`. Passed > 0, and failed, skipped, and xfailed all 0.

Acceptance, after that suite is green, is the issue's repro plus the related case. Claude's default model is Sonnet.

```bash
python3 evals/run.py --runtime claude --arm agent --runs 2 --case models-rotation-out-of-quota
python3 evals/run.py --runtime claude --arm agent --runs 2 --case models-rotation-first-entry
```

Both runs of each case green. If a run is still red, read that transcript before touching a grader again. A Log line that names `claude:haiku@high` is a later fixture change, and only when the transcript shows the model did not connect "claude haiku" to the rotation entry. Do not accept `--class` or a full Claude id as a pass.

## Ownership

This plan stage was given write access to `evals/run.py` only. That file is the wrong layer. Implement needs:

- `evals/cases/models-rotation-out-of-quota/case.json`
- `evals/cases/models-rotation-first-entry/case.json`
- `evals/cases/models-effort-whatever-the-runtime/case.json` (the quoted launch sentence only)
- `evals/test_cases.py`
- `evals/test_agent_budget.py`
- `skills/dispatch/SKILL.md`

## Risks

- The live case can stay red after the wording change. Sonnet missed the quota line on both runs in the report. The unit table does not prove the model will advance. The repro is the proof, and a red transcript is not a reason to widen the grader.
- Five bytes under the skill ceiling. An extra word fails `test_agent_budget`.
- The sentence edits are byte-identical across the skill, `DISPATCH_RULES`, and every grader `rule` that quotes them. One stale copy fails `test_dispatch_graders_load_and_quote_moved_rules` or `test_every_dispatch_sentence_moves_once_into_the_skill_and_the_kept_ones_stay`.
- `CHIEF_OF_STUFF_WORKSPACE` is set on an engine worker. `scripts/flow.py` replaces a `--root` that has no `CLAUDE.md` with that workspace, so `test_main_exits_zero_cleanly_when_not_a_workspace` fails for the environment. Run the suite with the variable unset. The plan run started `python3 -m unittest discover -s evals -p 'test_*.py'` that way. It shells out to git and had only reached the first audit-task tests after several minutes, so this document does not invent a count. The report carries the counts if that run finishes, and omits them if it does not.
