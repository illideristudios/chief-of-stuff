# Plan: Build columns draw every card (#938)

## Problem

`scripts/columns.py` still folds: `render(cols, limit=LIMIT)` (`LIMIT = 3`) draws `col.cards[:limit]` and puts `col.cards[limit:]` in a closed `<details class="columns-more">` whose summary is `+ {tally(rest)}`.

The only production caller, `render_board.py:33` (`columns.render(build_cols, inline=..., linked_inline=...)`), passes no `limit`, so every Build column folds past three, and the plan column hides 29 of 32 cards, 25 of them NEEDS INPUT.

#216 required the opposite: "A column draws every card; its count equals the cards drawn; no `+ N more` fold." The fold is a `columns.py` feature that nothing ever removed.

## Change

Delete the fold; do not make it configurable, because no caller wants it (`limit=` is passed only by `test_columns.py`).

`scripts/columns.py`:

- Delete `LIMIT`, the `limit` parameter of `render` and `_column`, the `shown, rest` split and the `more` string, so `_column` emits every card in `col.cards` after the head.
- Delete `tally` (no other user in `scripts/` or `evals/`; grep confirmed) and the `Counter` import.
- Delete the three `.columns-more` rules in `BASE_CSS`.
- Reword the module docstring ("with an overflow fold", "overflow is a native `<details>`"), the `render` docstring ("At most `limit` cards per column") and the `Card.kind` comment ("what overflow tallies").
- `render(cols, inline=_esc, linked_inline=None)`: `limit` was the second positional parameter, but every caller uses keywords, so no shim.

No change to `render_board.py`, `page-theme.css`, README, ARCHITECTURE or `kanban.md`: none mentions the fold (grep for `columns-more`, `fold`, `+ N more`), and the architecture agent owns architecture docs.

## Tests (red first, then green)

`evals/test_columns.py`:

- Replace `test_overflow_folds_past_the_limit_and_tallies_what_it_hides` with `test_a_column_draws_every_card_and_its_count_equals_the_cards_drawn`: a 244-card column and a 32-card column with mixed kinds; assert the `columns-card` count equals the sum of the cards, each section's `aria-label` count and `columns-count` equal the cards in that section, and neither `columns-more` nor `<details` appears. This is the #216 acceptance line by line.
- Rename `test_render_takes_the_columns_and_a_limit_and_draws_no_footer_strip` to drop "and a limit", fix its comment (`render(cols, limit=LIMIT)`), and add `assertRaises(TypeError)` for `columns.render(..., limit=2)` so the parameter stays gone.
- In `Css`: assert `columns.css()` has no `columns-more`.

`evals/test_flow_board.py`:

- `test_main_holds_the_changes_merged_today` (about line 290) renders `main.cards * 5` and asserts `"+ <span>2 merged today</span>"`; change it to assert the five cards are drawn and `columns-more` is absent.
- Add a board-level regression: a tracker with more than three cards in one stage and a NEEDS INPUT card past the third; `columns.render(rb.build_columns(...))` contains that card's `NEEDS INPUT` flag. This is the observed failure, reproduced through `build_columns`, not only through `columns.render`.

`evals/test_page_markdown.py` renders one card per column; no change expected, but it is in the run.

Run `python3 -m unittest evals.test_columns evals.test_flow_board evals.test_page_markdown`, then the full suite (`.github/workflows/tests.yml` has the CI command). The `require_green_tests` bar is passed > 0 with failed, skipped and xfailed all 0.

## Risks

- **Page weight and length**: every card is drawn, so the engine board's roughly 300 Build cards add on the order of 100–200 KB of HTML and the implement column (244 cards) becomes a very long stack; that is what #216 asks for, and shortening the done column is a separate decision this plan does not make, so flag it to the user rather than re-adding a fold.
- **Hidden callers**: only `render_board.py` and tests call `columns.render`; the full suite and a final grep for `limit=`, `LIMIT`, `tally` and `columns-more` over `scripts/` and `evals/` close this.
- **Live board**: it runs the installed copy (0.129.98), so the fix reaches it on release install followed by `pages.py --ensure`, not on merge.
- **Test baseline**: not run at this stage, because the command was blocked by a permission ask; the implementer runs it first to record the red and green counts.

## File ownership

- write: `scripts/columns.py`, `evals/test_columns.py`, `evals/test_flow_board.py`
- read: `scripts/render_board.py`, `evals/test_page_markdown.py`, `.github/workflows/tests.yml`

## Logic

```
c := a column draws every one of its cards          f := columns.py folds cards past LIMIT
n := the column count equals the cards drawn        h := a NEEDS INPUT card can be out of sight at rest
r := requirement #216 (closed, milestone 1.0)       t := a caller passes `limit` to render
k := columns-more, LIMIT, tally and `limit` removed a := #216 is adopted

1.  r → (c ∧ n ∧ ¬f)                       [#216 Acceptance]
2.  f → (¬c ∧ h)                           [columns.py:86-91: cards[limit:] go in a closed <details>]
3.  f                                      [issue #938 Evidence; render_board.py:33 passes no limit]
4.  ¬t                                     [grep: only test_columns.py passes `limit=`]
5.  k → (c ∧ ¬f)                           [every card is rendered from col.cards]
6.  c → n                                  [the count is len(col.cards), the cards drawn]
7.  (¬t ∧ k) → no caller loses behaviour   [from 4]
8.  T(r) ∧ P(r) ∧ B(r) ∧ E(r) ∧ L(r)       [the requirement is professional, buildable, ethical and legal; the change builds it]
∴   k meets r, so a once the tests in "Tests" pass, and the fold cannot return without a failing test
```
