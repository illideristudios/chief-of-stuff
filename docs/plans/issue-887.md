# Issue 887: Build columns follow Flow, and cards sort by issue number

The Build figure draws one column per lane stage and puts a card in the column named by `task_forge.effective_stage`, which is the tracker stage cell unless a merged change moves it to the lane's last stage (`scripts/render_board.py` `build_columns`, `scripts/task_forge.py` `effective_stage`). Cards stay in tracker-row order. Flow's note for a task that has a Log move is that move's stage (`scripts/flow_chart.py` `build`), not the cell.

On the board that filed this issue the cell is `design` for all 18 tasks, so the design column holds 18 and every later column, main included, holds 0. Eight of those tasks are open and their Flow note is `plan`. Ten are `done` and sit in design with the open ones. Inside the column the cards run `#1`, `#10`, `#11`, … `#18`, `#2`, which is tracker order of the issue text, not numeric order.

## Behavior

One rule for where a laned task's card goes. A task with a blank lane or stage still draws no card.

1. A `done` task whose lane has stages and whose stage cell is not that lane's last stage draws no Build card. It stays in the tracker, the tiles, and the Flow charts. A `done` task already at the lane's last stage stays a card there, which is what the current fixtures assert. Merged-today cards are unchanged and still append to `main` in merge-time order, after the task cards, so a pile of old done work cannot push them into the fold.
2. Any other laned task draws in the column of the stage Flow would use for it: the last Log move `flow_chart.build` keeps for that task (a `stage:` line, or a launcher line from before the first `stage:` line), when that stage is a stage of some lane. Otherwise the card stays on the tracker cell. The existing `pr` fold still applies after that choice, so a `pr` card draws in the next column and there is still no `pr` column.
3. Within a column, task cards sort by `tracker.issue_number` ascending. A card with no issue number sorts after the numbered ones, and equal numbers keep the tracker's order. Merged-today cards are not part of this sort.

`effective_stage` is the only place the stage is chosen. Drift and holds must use that same stage, so a card drawn in `plan` is not judged or held as `design`. `render` already builds the move list for Flow; build that list first and pass it into `hold_causes` and `build_columns`.

Do not change `columns.py`, the kanban label sync, or the tracker writer. The cell stays the record. The board stops pretending a stale `design` cell is where the task sits when the Log, which Flow already trusts, says otherwise.

## Tests

TDD. Add the failing cases to `evals/test_flow_board.py` before editing production code. Fixtures stay generic: invented task names, `#2` and `#10`, lanes shaped like the two real ones (`design` then `plan`, both ending at `main`). No workspace data.

- Two open tasks, cell `design`, Log moves to `plan`, tracker order `#10` then `#2`. Both cards are in `plan`, in order `#2` then `#10`. `design` does not hold them.
- An open task with no Log move stays on its cell.
- A `done` task at `design` on a lane that ends at `main` has no Build card. A `done` task whose cell is already `main` still has one, in `main`.
- A logged `pr` still draws in the following column.
- Merged-today cards remain the tail of `main`, newest first.

`python3 -m unittest evals/test_flow_board.py evals/test_columns.py evals/test_board_flow_design.py` is the check. Those last two pin a done-at-`main` card and must stay green. If either goes red, the new rule is wrong; do not weaken them to hide a card that is already at the lane's end.

## Risks

- A stage cell edited without `chief-of-stuff log --stage` loses to an older Log line. Flow already works that way. The official writer updates the cell and the Log together.
- A launcher line from before any `stage:` line can place a card at `implement`, `pr`, or `review`. That is the same move Flow draws. Ignoring launcher lines would leave those two views apart.
- Leaving early `done` tasks off the lanes, rather than moving them to `main`, is the choice. Moving them would bury merged-today cards past the three-card fold. The issue allows either.
- A `done` task on a lane that has no stage list still draws at its cell. `render` without lanes is that case, and the sha-on-the-card test depends on it.
