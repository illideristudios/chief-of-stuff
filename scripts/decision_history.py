"""Decision history access rules and the coordinator's precedent-source seam (§17, #1181).

Pure Use Cases circle: judges history justifications strictly from the request text and
current leaves (never reading ancestors or rejected alternatives), and defines the
`PrecedentSource` seam behind which the decision graph replaces tracker Decisions rows.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Protocol, Sequence

from decision_graph import DecisionLeaf, PrecedentEntry
from md import cells as _cells, is_separator as _is_separator, section as _section

FREE_READERS = frozenset({"coordinator", "architect", "owner"})
REQUEST_KINDS = ("ancestors", "was_rejected")

REFUSAL_INSTRUCTION = (
    "History request refused: justify why history is required. "
    "State either two current decisions that contradict (and exactly what each requires that cannot both hold), "
    "a current decision that does not match concrete code evidence, "
    "or — for was_rejected — the specific approach you are about to build and why the answer is not obvious."
)

_DENIAL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\breviewer\b[^.\n]{0,80}\bfinding\b|\breview\s+(?:finding|comment|feedback)\b|\bfinding\s+contradicts\b",
            re.IGNORECASE,
        ),
        "Denied: a reviewer finding against a current leaf is ruled on from the leaf and its reason, without history.",
    ),
    (
        re.compile(r"\b(?:more|additional|extra|background)\s+context\b", re.IGNORECASE),
        "Denied: wanting more context is not a history grant case.",
    ),
    (
        re.compile(r"\b(?:looks|seems|appears|might\s+be)\s+related\b", re.IGNORECASE),
        "Denied: 'looks related' is not a history grant case.",
    ),
    (
        re.compile(r"\bdecision\s+is\s+unclear\b|\bunclear\s+decision\b", re.IGNORECASE),
        "Denied: 'the decision is unclear' is answered from current leaves or escalated, not by reading superseded history.",
    ),
    (
        re.compile(r"\bdouble[- ]check(?:ing)?\s+(?:my\s+)?(?:own\s+)?work\b", re.IGNORECASE),
        "Denied: double-checking your own work is not a history grant case.",
    ),
    (
        re.compile(
            r"\b(?:has\s+)?anything\s+(?:like\s+this|similar)\s+been\s+tried\b|\bwhat\s+(?:else|alternatives)\s+(?:was|were)\s+(?:tried|considered)\b",
            re.IGNORECASE,
        ),
        "Denied: vague fishing for past attempts names no specific approach.",
    ),
)

_LEAF_REF = re.compile(r"(?:#|(?:\bdecision|\bleaf)\s+#?)(\d+)\b", re.IGNORECASE)
_INCOMPATIBLE = re.compile(
    r"\b(?:contradict\w*|conflict\w*|incompatible|mutually\s+exclusive|cannot\s+both|can't\s+both|both\s+cannot)\b",
    re.IGNORECASE,
)
_REQUIREMENT_CLAUSE = re.compile(
    r"\b(?:requires?|forbids?|mandates?|prohibits?|must(?:\s+not)?|specifies|demands?|disallows?)\b",
    re.IGNORECASE,
)
_MISMATCH_CLAIM = re.compile(
    r"\b(?:does\s+not\s+match|doesn't\s+match|mismatch\w*|code\s+shows|contrary\s+to\s+(?:the\s+)?code|actual\s+code)\b",
    re.IGNORECASE,
)
_CODE_EVIDENCE = re.compile(
    r"(?:[\w./-]+\.(?:py|ts|tsx|js|jsx|sql|json|toml|md)(?::\d+)?|`[^`\n]+`|\b[A-Z]\w+\.\w+\b)",
)
_BUILD_INTENT = re.compile(
    r"\b(?:about\s+to\s+build|plan(?:ning)?\s+to\s+(?:build|implement)|going\s+to\s+(?:build|implement)|propos(?:e|ing)\s+to\s+build|implementing)\b",
    re.IGNORECASE,
)
_NON_OBVIOUS = re.compile(
    r"\b(?:not\s+obvious|silent\s+on|without\s+stating|does\s+not\s+(?:mention|state|cover|specify)|doesn't\s+(?:mention|state|cover|specify)|leave(?:s)?\s+open|unspecified)\b",
    re.IGNORECASE,
)
_MULTI_HOP = re.compile(
    r"\b(?:grandparent|two\s+hops|(?:\d+|three|two|four|five)\s+(?:levels?|generations?|ancestors|hops)|beyond\s+the\s+immediate\s+parent|parent\s+of\s+(?:its\s+)?parent)\b",
    re.IGNORECASE,
)
_DEPTH_DIGITS = re.compile(
    r"\b(\d+)\s+(?:levels?|generations?|ancestors|hops)\b",
    re.IGNORECASE,
)
_DEPTH_WORDS = re.compile(
    r"\b(two|three|four|five)\s+(?:levels?|generations?|ancestors|hops)|(?:\btwo\s+hops\b)|(?:\bgrandparent\b)|"
    r"(?:\bbeyond\s+the\s+immediate\s+parent\b)|(?:\bparent\s+of\s+(?:its\s+)?parent\b)",
    re.IGNORECASE,
)
_DEPTH_WORD_VALUES = {"two": 2, "three": 3, "four": 4, "five": 5}
_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset({
    "a", "an", "and", "any", "anything", "approach", "as", "at", "been", "by", "for", "from",
    "has", "have", "in", "into", "is", "it", "like", "of", "on", "or", "something", "that",
    "the", "this", "to", "tried", "use", "using", "via", "was", "way", "whether", "with",
})


@dataclass(frozen=True)
class HistoryRequest:
    kind: str
    justification: str = ""
    leaf_id: int | None = None
    other_leaf_id: int | None = None
    requested_depth: int = 1
    approach: str = ""
    evidence: str = ""
    issue_key: str = ""
    worker_id: str = ""


@dataclass(frozen=True)
class HistoryVerdict:
    approved: bool
    grant_case: str
    reason: str
    approved_depth: int | None = None
    report_to_architect: bool = False


class PrecedentSource(Protocol):
    """Seam between the coordinator's question-answering use case and where precedent lives."""

    def current_precedent(self) -> tuple[PrecedentEntry, ...]: ...


def reads_freely(role: str) -> bool:
    """Only the coordinator, the architect and the owner may read history without justification."""
    return role.strip().lower() in FREE_READERS


def validate_worker_request(req: HistoryRequest) -> str | None:
    """Return a refusal message instructing the worker to justify when `req` lacks required fields, else None."""
    if req.kind not in REQUEST_KINDS:
        return f"History request refused: unknown request kind {req.kind!r}; expected one of {', '.join(REQUEST_KINDS)}."
    if not isinstance(req.justification, str) or not req.justification.strip():
        return REFUSAL_INSTRUCTION
    if req.kind == "ancestors":
        if type(req.leaf_id) is not int or req.leaf_id < 1:
            return "History request refused: name the current leaf decision id and justify why its ancestors are needed."
        if type(req.requested_depth) is not int or req.requested_depth < 1:
            return "History request refused: requested_depth must be a positive integer."
    return None


def _referenced_leaf_ids(req: HistoryRequest, valid_ids: frozenset[int]) -> tuple[int, ...]:
    ordered: list[int] = []
    for candidate in (req.leaf_id, req.other_leaf_id):
        if type(candidate) is int and candidate in valid_ids and candidate not in ordered:
            ordered.append(candidate)
    for match in _LEAF_REF.finditer(req.justification):
        cid = int(match.group(1))
        if cid in valid_ids and cid not in ordered:
            ordered.append(cid)
    return tuple(ordered)


def _justified_depth(text: str) -> int | None:
    """Largest ancestor depth the justification text itself names; None when it names none."""
    depths: list[int] = []
    for match in _DEPTH_DIGITS.finditer(text):
        depths.append(int(match.group(1)))
    for match in _DEPTH_WORDS.finditer(text):
        word = (match.group(1) or "").lower()
        if word in _DEPTH_WORD_VALUES:
            depths.append(_DEPTH_WORD_VALUES[word])
        else:
            # grandparent / two hops / beyond immediate parent / parent of parent → one extra hop.
            depths.append(2)
    return max(depths) if depths else None


def approved_depth_for(req: HistoryRequest) -> int:
    """Grant only as far up as the justification requires (and never past requested_depth)."""
    requested = max(1, req.requested_depth)
    if requested == 1:
        return 1
    combined = f"{req.justification}\n{req.evidence}"
    if not _MULTI_HOP.search(combined):
        return 1
    justified = _justified_depth(combined)
    if justified is None:
        justified = 2
    return min(requested, justified)


def _specific_approach_tokens(approach: str) -> frozenset[str]:
    return frozenset(t for t in _WORD.findall(approach.lower()) if t not in _STOPWORDS)


def judge_justification(req: HistoryRequest, current_leaves: Sequence[DecisionLeaf]) -> HistoryVerdict:
    """Harsh coordinator judgement over justification text and current leaves only (never reads history)."""
    refusal = validate_worker_request(req)
    if refusal is not None:
        return HistoryVerdict(approved=False, grant_case="", reason=refusal)

    combined = f"{req.justification}\n{req.evidence}\n{req.approach}".strip()
    for pattern, denial_reason in _DENIAL_PATTERNS:
        if pattern.search(combined):
            return HistoryVerdict(approved=False, grant_case="", reason=denial_reason)

    leaves_by_id = {leaf.id: leaf for leaf in current_leaves}
    valid_ids = frozenset(leaves_by_id)

    if req.kind == "ancestors":
        if req.leaf_id not in leaves_by_id:
            return HistoryVerdict(
                approved=False,
                grant_case="",
                reason=f"Denied: decision #{req.leaf_id} is not a current decision leaf.",
            )
        prose = f"{req.justification}\n{req.evidence}".strip()
        referenced = _referenced_leaf_ids(req, valid_ids)
        req_clauses = _REQUIREMENT_CLAUSE.findall(prose)

        if len(referenced) >= 2 and _INCOMPATIBLE.search(prose) and len(req_clauses) >= 2:
            return HistoryVerdict(
                approved=True,
                grant_case="contradiction",
                reason=(
                    f"Approved: current leaves #{referenced[0]} and #{referenced[1]} state "
                    "mutually incompatible requirements."
                ),
                approved_depth=approved_depth_for(req),
                report_to_architect=False,
            )

        if _MISMATCH_CLAIM.search(prose) and _CODE_EVIDENCE.search(prose):
            return HistoryVerdict(
                approved=True,
                grant_case="code_mismatch",
                reason=(
                    f"Approved: current leaf #{req.leaf_id} mismatches cited code evidence; "
                    "reporting mismatch to the architect."
                ),
                approved_depth=approved_depth_for(req),
                report_to_architect=True,
            )

        return HistoryVerdict(
            approved=False,
            grant_case="",
            reason="Denied: justification meets neither the leaf-contradiction nor the code-mismatch grant case.",
        )

    # req.kind == "was_rejected"
    approach = req.approach.strip()
    tokens = _specific_approach_tokens(approach)
    if not approach or len(tokens) < 2:
        return HistoryVerdict(
            approved=False,
            grant_case="",
            reason="Denied: was_rejected must name a concrete, specific approach.",
        )
    if not _BUILD_INTENT.search(req.justification) or not _NON_OBVIOUS.search(req.justification):
        return HistoryVerdict(
            approved=False,
            grant_case="",
            reason=(
                "Denied: was_rejected must state the specific approach you are about to build "
                "and why the answer is not obvious from current leaves."
            ),
        )
    return HistoryVerdict(
        approved=True,
        grant_case="rejected_approach",
        reason=f"Approved narrow rejected-alternative check for approach {approach!r}.",
        approved_depth=None,
        report_to_architect=False,
    )


def tracker_precedent_entries(tracker_text: str) -> tuple[PrecedentEntry, ...]:
    """Read precedent from the legacy `## Decisions` markdown table (`time | item | words`)."""
    rows = [_cells(line) for line in _section(tracker_text, "## Decisions") if line.strip().startswith("|")]
    parsed = [(row + ["", "", ""])[:3] for row in rows[1:] if not _is_separator(row)]
    out: list[PrecedentEntry] = []
    for idx, (recorded_at, item, words) in enumerate(parsed, start=1):
        if not item.strip() and not words.strip():
            continue
        out.append(
            PrecedentEntry(
                id=f"tracker:{idx}",
                subject=item.strip(),
                decision=words.strip(),
                reason="",
                recorded_at=recorded_at.strip(),
            )
        )
    return tuple(out)


def graph_precedent_entries(leaves: Sequence[DecisionLeaf]) -> tuple[PrecedentEntry, ...]:
    """Project current decision graph leaves behind the coordinator's `PrecedentSource` seam."""
    return tuple(
        PrecedentEntry(
            id=f"decision:{leaf.id}",
            subject=leaf.topic or f"decision-{leaf.id}",
            decision=leaf.decision,
            reason=leaf.reason,
            recorded_at=leaf.created_at,
        )
        for leaf in leaves
    )


def lookup_precedent(source: PrecedentSource, question: str = "") -> tuple[PrecedentEntry, ...]:
    """Query `source` without knowing whether precedent comes from tracker rows or the decision graph."""
    entries = source.current_precedent()
    query_tokens = frozenset(t for t in _WORD.findall(question.lower()) if t not in _STOPWORDS)
    if not query_tokens:
        return entries
    scored: list[tuple[int, int, PrecedentEntry]] = []
    for idx, entry in enumerate(entries):
        haystack = f"{entry.subject} {entry.decision} {entry.reason}".lower()
        entry_tokens = frozenset(_WORD.findall(haystack))
        overlap = len(query_tokens & entry_tokens)
        if overlap:
            scored.append((overlap, -idx, entry))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return tuple(item[2] for item in scored)
