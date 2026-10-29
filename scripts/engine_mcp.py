"""The engine's stdio report/ask/decisions channel (design 10, §17); all identities come from the launch environment."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import stat
from typing import Literal

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

import decision_history
from guard_binding import Binding
import inbox
from policy import SEVERITIES
from store import Store, verdict_fifo_path


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    severity: str = Field(json_schema_extra={"enum": list(SEVERITIES)})
    title: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    path: str | None = None
    line: int | None = Field(default=None, ge=1)
    suggestion: str

    @field_validator("severity")
    @classmethod
    def severity_known(cls, value: str) -> str:
        if value not in SEVERITIES:
            raise ValueError(f"severity must be one of {', '.join(SEVERITIES)}")
        return value


class ReportInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    outcome: Literal["done", "failed", "blocked"]
    summary: str = Field(min_length=1)
    findings: list[Finding] = Field(default_factory=list)
    passed: int | None = None
    failed: int | None = None
    skipped: int | None = None
    xfailed: int | None = None


class AskInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    question: str = Field(min_length=1)
    options: list[StrictStr] = Field(default_factory=list)
    context: str


class DecisionsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["leaves", "ancestors", "was_rejected"] = "leaves"
    leaf_id: int | None = None
    other_leaf_id: int | None = None
    requested_depth: int = Field(default=1, ge=1)
    approach: str = ""
    justification: str = ""
    evidence: str = ""


def _drain_fifo(fd: int) -> None:
    while True:
        try:
            chunk = os.read(fd, 256)
            if not chunk:
                break
        except (BlockingIOError, InterruptedError):
            break
        except OSError:
            break


class _VerdictWaiter:
    """Per-request non-blocking POSIX FIFO waiter registered directly on the asyncio event loop.

    Opening the FIFO with `O_RDWR | O_NONBLOCK` before notifying the coordinator keeps the kernel pipe buffer
    alive so `signal_history_verdict`'s non-blocking write never fails with `ENXIO`. On each wait cycle, draining
    before querying `store.history_request` implements race-free record-then-wake:
    - If the verdict was already recorded before the FIFO was opened or before `wait()` starts, the store check
      returns immediately without waiting on the pipe.
    - If the verdict commits after `history_request` observes `status == "pending"`, `signal_history_verdict` writes
      into the open `rfd` buffer and the level-triggered event-loop reader fires immediately.
    """

    def __init__(self, root: Path, request_id: int) -> None:
        self.request_id = request_id
        self.fifo: Path | None = verdict_fifo_path(root, request_id)
        self.rfd: int | None = None

    def __enter__(self) -> _VerdictWaiter:
        if self.fifo is not None:
            try:
                self.fifo.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.mkfifo(self.fifo, 0o600)
                except FileExistsError:
                    pass
                fd = os.open(self.fifo, os.O_RDWR | os.O_NONBLOCK)
                if stat.S_ISFIFO(os.fstat(fd).st_mode):
                    self.rfd = fd
                else:
                    os.close(fd)
                    self.fifo = None
            except OSError:
                self.fifo = None
                self.rfd = None
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        if self.rfd is not None:
            try:
                os.close(self.rfd)
            except OSError:
                pass
            self.rfd = None
        if self.fifo is not None:
            try:
                self.fifo.unlink(missing_ok=True)
            except OSError:
                pass

    async def wait(self, store: Store, *, fallback_interval: float | None = 1.0) -> None:
        loop = asyncio.get_running_loop()
        while True:
            if self.rfd is not None:
                _drain_fifo(self.rfd)
            row = store.history_request(self.request_id)
            if row is None or row.status != "pending":
                return
            if self.rfd is None:
                if fallback_interval is None:
                    raise RuntimeError("verdict FIFO is unavailable and fallback polling is disabled")
                await asyncio.sleep(min(fallback_interval, 0.05))
                continue

            fd = self.rfd
            woke: asyncio.Future[bool] = loop.create_future()

            def _on_readable(fut: asyncio.Future[bool] = woke, ready_fd: int = fd) -> None:
                _drain_fifo(ready_fd)
                if not fut.done():
                    fut.set_result(True)

            loop.add_reader(fd, _on_readable)
            try:
                if fallback_interval is None:
                    await woke
                else:
                    try:
                        await asyncio.wait_for(woke, timeout=fallback_interval)
                    except TimeoutError:
                        pass
            finally:
                loop.remove_reader(fd)


class EngineTools:
    def __init__(self, binding: Binding, store: Store) -> None:
        self.binding, self.store = binding, store
        self._verdict_fallback_interval: float | None = 1.0

    def report(self, outcome: Literal["done", "failed", "blocked"], summary: str,
               findings: list[Finding] | None = None, passed: int | None = None, failed: int | None = None,
               skipped: int | None = None, xfailed: int | None = None) -> dict:
        """Record this run's result. Call before stopping, including when blocked or failed.

        passed, failed, skipped and xfailed are the final suite run's counts. Omit a count when it is unknown.
        """
        data = ReportInput.model_validate({
            "outcome": outcome, "summary": summary, "findings": [] if findings is None else findings,
            "passed": passed, "failed": failed, "skipped": skipped, "xfailed": xfailed})
        b = self.binding
        report_id = self.store.add_report(
            b.issue_key, b.stage, b.attempt, b.session_id, data.outcome, data.summary,
            [f.model_dump(exclude_none=True) for f in data.findings],
            suite={"passed": data.passed, "failed": data.failed, "skipped": data.skipped, "xfailed": data.xfailed})
        return {"report_id": report_id}

    def ask(self, question: str, options: list[str] | None = None, context: str = "") -> dict:
        """Record a question for the engine. This never grants permission or waits for an answer."""
        data = AskInput.model_validate({"question": question, "options": [] if options is None else options,
                                       "context": context})
        b = self.binding
        ask_id = self.store.add_ask(b.issue_key, b.stage, b.attempt, b.session_id,
                                   data.question, data.options, data.context)
        return {"ask_id": ask_id, "message": "Report outcome=blocked, then stop and wait for the engine. "
                "An ask is not an approval; the engine routes the decision."}

    async def _await_verdict(
        self,
        request_id: int,
        *,
        waiter: _VerdictWaiter | None = None,
        fallback_interval: float | None = 1.0,
    ) -> None:
        if waiter is not None:
            await waiter.wait(self.store, fallback_interval=fallback_interval)
            return
        with _VerdictWaiter(self.binding.root, request_id) as owned:
            await owned.wait(self.store, fallback_interval=fallback_interval)

    async def decisions(
        self,
        action: Literal["leaves", "ancestors", "was_rejected"] = "leaves",
        leaf_id: int | None = None,
        other_leaf_id: int | None = None,
        requested_depth: int = 1,
        approach: str = "",
        justification: str = "",
        evidence: str = "",
    ) -> dict:
        """Read current decision leaves, or submit a justified history / was_rejected request and await the verdict."""
        data = DecisionsInput.model_validate({
            "action": action,
            "leaf_id": leaf_id,
            "other_leaf_id": other_leaf_id,
            "requested_depth": requested_depth,
            "approach": approach,
            "justification": justification,
            "evidence": evidence,
        })
        if data.action == "leaves":
            return {"leaves": [self.store._serialize_leaf(leaf) for leaf in self.store.decision_leaves()]}

        b = self.binding
        req = decision_history.HistoryRequest(
            kind=data.action,
            justification=data.justification,
            leaf_id=data.leaf_id,
            other_leaf_id=data.other_leaf_id,
            requested_depth=data.requested_depth,
            approach=data.approach,
            evidence=data.evidence,
            issue_key=b.issue_key,
            worker_id=b.session_id,
        )
        refusal = decision_history.validate_worker_request(req)
        if refusal is not None:
            return {"status": "refused", "kind": data.action, "message": refusal}

        row = self.store.submit_history_request(req, stage=b.stage, attempt=b.attempt)
        # Open the wakeup FIFO (`O_RDWR | O_NONBLOCK`) BEFORE posting to the coordinator mailbox so a fast
        # coordinator cannot signal an unopened FIFO (`ENXIO`) while `inbox.send_message` is still finishing.
        with _VerdictWaiter(b.root, row.id) as waiter:
            mailbox_dir = b.root / ".chief-of-stuff" / "mailbox"
            inbox.send_message(
                recipient="coordinator",
                sender=b.session_id,
                msg_type="ask",
                task=b.issue_key,
                mailbox_dir=mailbox_dir,
                body=(
                    f"Decision history request #{row.id} ({row.kind}) from {b.session_id} on {b.issue_key}: "
                    f"{row.justification}"
                ),
                payload={
                    "kind": "decision_history_request",
                    "request_id": row.id,
                    "request_kind": row.kind,
                    "issue_key": b.issue_key,
                    "stage": b.stage,
                    "attempt": b.attempt,
                    "session_id": b.session_id,
                    "leaf_id": row.leaf_id,
                    "other_leaf_id": row.other_leaf_id,
                    "requested_depth": row.requested_depth,
                    "approach": row.approach,
                    "justification": row.justification,
                    "evidence": row.evidence,
                },
            )
            await self._await_verdict(
                row.id,
                waiter=waiter,
                fallback_interval=self._verdict_fallback_interval,
            )
        return self.store.fulfill_history_request(row.id)


def create_server(binding: Binding, store: Store) -> MCPServer:
    server = MCPServer("chief-of-stuff-engine")
    tools = EngineTools(binding, store)
    server.tool()(tools.report)
    server.tool()(tools.ask)
    server.tool()(tools.decisions)
    return server


def main() -> None:
    binding = Binding.from_env()
    # Engine startup owns migrations; concurrent workers only open the existing store.
    if not binding.store_path.is_file():
        raise ValueError("engine store is missing; the engine must migrate it before launching workers")
    store = Store(binding.store_path)
    try:
        create_server(binding, store).run(transport="stdio")
    finally:
        store.engine.dispose()


if __name__ == "__main__":
    main()
