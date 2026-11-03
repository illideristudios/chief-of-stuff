"""Decision graph entities and pure DAG judgements (§17, #1181).

Current decisions are the unreplaced leaves of a directed acyclic provenance graph.
Each leaf carries its full decision text, its reason, and links to parent decisions;
rejected alternatives live only in ancestors, never in a leaf.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import re
from typing import Mapping, Sequence

_TOKEN = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset({
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "into", "is", "it",
    "of", "on", "or", "that", "the", "this", "to", "use", "using", "via", "with",
})


@dataclass(frozen=True)
class RejectedAlternative:
    approach: str
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.approach, str) or not self.approach.strip():
            raise ValueError("rejected alternative approach must be non-empty")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("rejected alternative reason must be non-empty")


@dataclass(frozen=True)
class DecisionLeaf:
    """A current unreplaced decision. Carries full text, reason and parent links; never rejected alternatives."""

    id: int
    decision: str
    reason: str
    parent_ids: tuple[int, ...] = ()
    topic: str = ""
    decided_by: str = ""
    issue_key: str = ""
    created_at: str = ""

    def __post_init__(self) -> None:
        if type(self.id) is not int or self.id < 1:
            raise ValueError("decision id must be a positive integer")
        if not isinstance(self.decision, str) or not self.decision.strip():
            raise ValueError("decision text must be non-empty")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("decision reason must be non-empty")


@dataclass(frozen=True)
class AncestorDecision:
    """A graph node as stored or traversed in history; rejected alternatives are valid only on ancestors."""

    id: int
    decision: str
    reason: str
    parent_ids: tuple[int, ...] = ()
    rejected_alternatives: tuple[RejectedAlternative, ...] = ()
    topic: str = ""
    decided_by: str = ""
    issue_key: str = ""
    created_at: str = ""

    def __post_init__(self) -> None:
        if type(self.id) is not int or self.id < 1:
            raise ValueError("decision id must be a positive integer")
        if not isinstance(self.decision, str) or not self.decision.strip():
            raise ValueError("decision text must be non-empty")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("decision reason must be non-empty")


@dataclass(frozen=True)
class PrecedentEntry:
    """One precedent item behind the coordinator's answer-source seam."""

    id: str
    subject: str
    decision: str
    reason: str
    recorded_at: str = ""


def _reaches(parents_by_node: Mapping[int, Sequence[int]], start: int, target: int) -> bool:
    seen: set[int] = set()
    queue: deque[int] = deque([start])
    while queue:
        current = queue.popleft()
        if current == target:
            return True
        if current in seen:
            continue
        seen.add(current)
        queue.extend(parents_by_node.get(current, ()))
    return False


def validate_parent_links(existing_parents: Mapping[int, Sequence[int]],
                          node_id: int | None,
                          parent_ids: Sequence[int]) -> tuple[int, ...]:
    """Deduplicate and verify parent provenance links preserve a finite DAG without self-links or cycles."""
    ordered: list[int] = []
    seen: set[int] = set()
    for pid in parent_ids:
        if type(pid) is not int or pid < 1:
            raise ValueError(f"parent id must be a positive integer: {pid!r}")
        if node_id is not None and pid == node_id:
            raise ValueError(f"decision {node_id} cannot link to itself as a parent")
        if pid not in existing_parents:
            raise ValueError(f"unknown parent decision id: {pid}")
        if node_id is not None and _reaches(existing_parents, pid, node_id):
            raise ValueError(f"parent link {node_id} -> {pid} introduces a cycle in the decision graph")
        if pid not in seen:
            seen.add(pid)
            ordered.append(pid)
    return tuple(ordered)


def ancestor_ids(parents_by_node: Mapping[int, Sequence[int]]) -> frozenset[int]:
    """Every node id that at least one child decision replaces."""
    known = set(parents_by_node)
    return frozenset(pid for links in parents_by_node.values() for pid in links if pid in known)


def leaf_ids(parents_by_node: Mapping[int, Sequence[int]]) -> frozenset[int]:
    """Current unreplaced decision ids: nodes nothing links to as a parent."""
    replaced = ancestor_ids(parents_by_node)
    return frozenset(nid for nid in parents_by_node if nid not in replaced)


def validate_rejected_target(parents_by_node: Mapping[int, Sequence[int]], node_id: int) -> None:
    """Enforce that rejected alternatives live only in ancestors, never in a leaf."""
    if node_id not in parents_by_node:
        raise ValueError(f"unknown decision id: {node_id}")
    if node_id not in ancestor_ids(parents_by_node):
        raise ValueError("rejected alternatives live only in ancestors, never in a leaf")


def current_leaves(nodes: Sequence[AncestorDecision],
                   parents_by_node: Mapping[int, Sequence[int]]) -> tuple[DecisionLeaf, ...]:
    """Project only current unreplaced leaves, stripping any ancestor-only state."""
    current = leaf_ids(parents_by_node)
    selected = sorted((n for n in nodes if n.id in current), key=lambda n: n.id)
    return tuple(
        DecisionLeaf(
            id=n.id,
            decision=n.decision,
            reason=n.reason,
            parent_ids=tuple(parents_by_node.get(n.id, n.parent_ids)),
            topic=n.topic,
            decided_by=n.decided_by,
            issue_key=n.issue_key,
            created_at=n.created_at,
        )
        for n in selected
    )


def ancestor_slice(nodes_by_id: Mapping[int, AncestorDecision],
                   start_id: int,
                   max_depth: int | None = None) -> tuple[AncestorDecision, ...]:
    """Return ancestors of `start_id` in BFS order up to `max_depth` hops (all reachable when None)."""
    if start_id not in nodes_by_id:
        raise KeyError(f"unknown decision id: {start_id}")
    if max_depth is not None and (type(max_depth) is not int or max_depth < 1):
        raise ValueError("max_depth must be None or a positive integer")
    visited: set[int] = {start_id}
    out: list[AncestorDecision] = []
    queue: deque[tuple[int, int]] = deque([(start_id, 0)])
    while queue:
        nid, depth = queue.popleft()
        if max_depth is not None and depth >= max_depth:
            continue
        node = nodes_by_id.get(nid)
        if node is None:
            continue
        for pid in node.parent_ids:
            if pid in visited or pid not in nodes_by_id:
                continue
            visited.add(pid)
            parent = nodes_by_id[pid]
            out.append(parent)
            queue.append((pid, depth + 1))
    return tuple(out)


def _normalize(text: str) -> str:
    return " ".join(_TOKEN.findall(text.lower()))


def _content_tokens(text: str) -> frozenset[str]:
    tokens = _TOKEN.findall(text.lower())
    filtered = frozenset(t for t in tokens if t not in _STOPWORDS)
    return filtered or frozenset(tokens)


def find_rejected_alternative(nodes_by_id: Mapping[int, AncestorDecision],
                              parents_by_node: Mapping[int, Sequence[int]],
                              approach: str) -> tuple[int, RejectedAlternative] | None:
    """Match a named approach against rejected alternatives in ancestors only; never inspects leaves."""
    norm_query = _normalize(approach)
    if not norm_query:
        return None
    query_tokens = _content_tokens(approach)
    ancestors = ancestor_ids(parents_by_node)
    best: tuple[int, int, int, RejectedAlternative] | None = None
    for nid in sorted(ancestors):
        node = nodes_by_id.get(nid)
        if node is None:
            continue
        for alt in node.rejected_alternatives:
            norm_alt = _normalize(alt.approach)
            if not norm_alt:
                continue
            alt_tokens = _content_tokens(alt.approach)
            score = 0
            if norm_query == norm_alt:
                score = 4
            elif len(norm_query) >= 4 and (norm_query in norm_alt or norm_alt in norm_query):
                score = 3
            elif query_tokens and alt_tokens and query_tokens == alt_tokens:
                score = 3
            elif len(query_tokens) >= 2 and alt_tokens and (query_tokens <= alt_tokens or alt_tokens <= query_tokens):
                score = 2
            if score:
                candidate = (score, nid, -abs(len(norm_alt) - len(norm_query)), alt)
                if best is None or candidate[:3] > best[:3]:
                    best = candidate
    return (best[1], best[3]) if best is not None else None
