#!/usr/bin/env python3
"""Coordinator, architect and owner CLI for the decision graph and history verdicts (§17, #1181).

Workers must reach the decision graph only through the engine MCP `decisions` tool;
this CLI refuses execution when worker binding variables (`CHIEF_ISSUE_KEY` / `CHIEF_SESSION_ID`) are set.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _vendor.toon_format import encode as toon_encode
import decision_graph
import decision_history
import inbox
import precedent_source
import store
import store_access


def _emit(value: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, indent=2, ensure_ascii=False))
    elif value == []:
        print("[]")
    else:
        print(toon_encode(value))


def _serialize_request(row: store.DecisionHistoryRequest) -> dict[str, Any]:
    return {
        "id": row.id,
        "issue_key": row.issue_key,
        "stage": row.stage,
        "attempt": row.attempt,
        "session_id": row.session_id,
        "kind": row.kind,
        "leaf_id": row.leaf_id,
        "other_leaf_id": row.other_leaf_id,
        "requested_depth": row.requested_depth,
        "approach": row.approach,
        "justification": row.justification,
        "evidence": row.evidence,
        "status": row.status,
        "grant_case": row.grant_case,
        "verdict_reason": row.verdict_reason,
        "approved_depth": row.approved_depth,
        "report_to_architect": row.report_to_architect,
        "decided_by": row.decided_by,
        "created_at": row.created_at,
        "decided_at": row.decided_at,
    }


def _serialize_grant(row: store.DecisionHistoryGrant) -> dict[str, Any]:
    return {
        "id": row.id,
        "request_id": row.request_id,
        "issue_key": row.issue_key,
        "session_id": row.session_id,
        "kind": row.kind,
        "grant_case": row.grant_case,
        "leaf_id": row.leaf_id,
        "approved_depth": row.approved_depth,
        "returned_ids": list(row.returned_ids),
        "matched_approach": row.matched_approach,
        "granted_at": row.granted_at,
    }


def _notify_architect_mismatch(root: Path, row: store.DecisionHistoryRequest) -> None:
    if not row.report_to_architect:
        return
    mailbox_dir = Path(root) / ".chief-of-stuff" / "mailbox"
    inbox.send_message(
        recipient="architect",
        sender=row.decided_by or "coordinator",
        msg_type="report",
        task=row.issue_key or None,
        mailbox_dir=mailbox_dir,
        body=(
            f"Decision #{row.leaf_id} code mismatch reported on history request #{row.id} "
            f"({row.issue_key or 'workspace'}): {row.justification}"
        ),
        payload={
            "kind": "decision_code_mismatch",
            "request_id": row.id,
            "leaf_id": row.leaf_id,
            "issue_key": row.issue_key,
            "session_id": row.session_id,
            "justification": row.justification,
            "evidence": row.evidence,
        },
    )


def _parse_rejected_pair(spec: str) -> decision_graph.RejectedAlternative:
    if "=" not in spec:
        raise ValueError(f"--rejected expects APPROACH=REASON, got {spec!r}")
    approach, reason = spec.split("=", 1)
    return decision_graph.RejectedAlternative(approach=approach.strip(), reason=reason.strip())


def main(argv: list[str] | None = None) -> int:
    if os.environ.get("CHIEF_ISSUE_KEY") or os.environ.get("CHIEF_SESSION_ID"):
        print(
            "refused: workers must reach the decision graph only through the engine MCP decisions tool",
            file=sys.stderr,
        )
        return 1

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=Path("."), help="workspace root holding store.db")
    ap.add_argument("--json", action="store_true", dest="as_json", help="emit JSON instead of TOON")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("leaves", help="list current unreplaced decision leaves")

    p_add = sub.add_parser("add", help="append a new decision leaf to the graph")
    p_add.add_argument("--decision", required=True, help="full decision text")
    p_add.add_argument("--reason", required=True, help="reason for the decision")
    p_add.add_argument("--topic", default="", help="short decision topic")
    p_add.add_argument("--parent", dest="parents", type=int, action="append", default=[],
                       help="parent decision id this decision replaces; repeatable")
    p_add.add_argument("--decided-by", default="owner", help="decision authority (e.g. owner, trio, coordinator)")
    p_add.add_argument("--issue", default="", help="optional forge issue key")
    p_add.add_argument("--rejected", action="append", default=[],
                       help="rejected alternative APPROACH=REASON attached to the superseded parent; repeatable")

    p_rej = sub.add_parser("reject", help="record a rejected alternative on an ancestor decision")
    p_rej.add_argument("--ancestor", type=int, required=True, help="ancestor decision id")
    p_rej.add_argument("--approach", required=True, help="rejected approach")
    p_rej.add_argument("--reason", required=True, help="why the approach was rejected")

    p_read = sub.add_parser("read", help="free-reader graph traversal (coordinator, architect or owner)")
    p_read.add_argument("--role", required=True, choices=sorted(decision_history.FREE_READERS))
    p_read.add_argument("--leaf", type=int, default=None, help="optional leaf id to slice ancestors from")
    p_read.add_argument("--depth", type=int, default=None, help="optional max ancestor depth")

    sub.add_parser("pending", help="list pending worker history requests (justification text only, no history)")

    p_judge = sub.add_parser("judge", help="judge pending worker history requests without reading history")
    p_judge.add_argument("--request", type=int, default=None, help="judge one specific request id")

    p_decide = sub.add_parser("decide", help="record an explicit coordinator verdict on a pending request")
    p_decide.add_argument("--request", type=int, required=True, help="request id")
    verdict_group = p_decide.add_mutually_exclusive_group(required=True)
    verdict_group.add_argument("--approve", action="store_true")
    verdict_group.add_argument("--deny", action="store_true")
    p_decide.add_argument("--reason", required=True, help="verdict rationale")
    p_decide.add_argument("--case", default="", help="grant case name when approving")
    p_decide.add_argument("--depth", type=int, default=None, help="approved ancestor depth")
    p_decide.add_argument("--report-mismatch", action="store_true",
                          help="report a code mismatch to the architect")

    sub.add_parser("grants", help="list logged decision history grants")

    p_prec = sub.add_parser("precedent", help="query precedent behind the coordinator's PrecedentSource seam")
    p_prec.add_argument("--query", default="", help="optional question or search terms")
    p_prec.add_argument("--date", default=None, help="optional tracker date fallback (YYYY-MM-DD)")

    args = ap.parse_args(argv)
    root = args.root.resolve()

    if args.cmd == "precedent":
        source = precedent_source.workspace_precedent_source(root, date=args.date)
        entries = decision_history.lookup_precedent(source, args.query)
        _emit(
            [
                {
                    "id": e.id,
                    "subject": e.subject,
                    "decision": e.decision,
                    "reason": e.reason,
                    "recorded_at": e.recorded_at,
                }
                for e in entries
            ],
            as_json=args.as_json,
        )
        return 0

    try:
        st = store_access.migrated_store(root)
    except Exception as exc:  # noqa: BLE001
        print(f"decisions: {exc}", file=sys.stderr)
        return 1

    try:
        if args.cmd == "leaves":
            _emit([st._serialize_leaf(leaf) for leaf in st.decision_leaves()], as_json=args.as_json)
            return 0

        if args.cmd == "add":
            alts = [_parse_rejected_pair(spec) for spec in args.rejected]
            leaf = st.add_graph_decision(
                args.decision,
                args.reason,
                parent_ids=args.parents,
                topic=args.topic,
                decided_by=args.decided_by,
                issue_key=args.issue,
                rejected_alternatives=alts,
            )
            _emit(st._serialize_leaf(leaf), as_json=args.as_json)
            return 0

        if args.cmd == "reject":
            alt = st.add_rejected_alternative(args.ancestor, args.approach, args.reason)
            _emit({"ancestor_id": args.ancestor, "approach": alt.approach, "reason": alt.reason}, as_json=args.as_json)
            return 0

        if args.cmd == "read":
            view = st.read_decision_graph(role=args.role, leaf_id=args.leaf, max_depth=args.depth)
            _emit(view, as_json=args.as_json)
            return 0

        if args.cmd == "pending":
            _emit([_serialize_request(r) for r in st.pending_history_requests()], as_json=args.as_json)
            return 0

        if args.cmd == "judge":
            if args.request is not None:
                prior = st.history_request(args.request)
                was_pending = prior is not None and prior.status == "pending"
                decided_rows = [st.judge_history_request(args.request)]
                if was_pending and decided_rows[0].report_to_architect:
                    _notify_architect_mismatch(root, decided_rows[0])
            else:
                decided_rows = st.judge_pending_history_requests()
                for row in decided_rows:
                    if row.report_to_architect:
                        _notify_architect_mismatch(root, row)
            _emit([_serialize_request(r) for r in decided_rows], as_json=args.as_json)
            return 0

        if args.cmd == "decide":
            prior = st.history_request(args.request)
            was_pending = prior is not None and prior.status == "pending"
            decided = st.decide_history_request(
                args.request,
                approved=bool(args.approve),
                reason=args.reason,
                grant_case=args.case,
                approved_depth=args.depth,
                report_to_architect=args.report_mismatch,
            )
            if was_pending and decided.report_to_architect:
                _notify_architect_mismatch(root, decided)
            _emit(_serialize_request(decided), as_json=args.as_json)
            return 0

        if args.cmd == "grants":
            _emit([_serialize_grant(g) for g in st.history_grants()], as_json=args.as_json)
            return 0
    except (KeyError, PermissionError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    finally:
        st.engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
