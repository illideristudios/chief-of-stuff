"""The engine's stdio report/ask channel (design 10); all identities come from the launch environment."""
from __future__ import annotations

from typing import Literal

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

from guard_binding import Binding
from policy import DISPOSITIONS, SEVERITIES
from store import Store


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    severity: str = Field(json_schema_extra={"enum": list(SEVERITIES)})
    title: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    path: str | None = None
    line: int | None = Field(default=None, ge=1)
    suggestion: str
    finding_key: str | None = None  # an architect echoes the key and its disposition for each finding it was given
    disposition: str | None = Field(default=None, json_schema_extra={"enum": list(DISPOSITIONS)})

    @field_validator("disposition")
    @classmethod
    def disposition_known(cls, value: str | None) -> str | None:
        if value is not None and value not in DISPOSITIONS:
            raise ValueError(f"disposition must be one of {', '.join(DISPOSITIONS)}")
        return value

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


class AskInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    question: str = Field(min_length=1)
    options: list[StrictStr] = Field(default_factory=list)
    context: str


class EngineTools:
    def __init__(self, binding: Binding, store: Store) -> None:
        self.binding, self.store = binding, store

    def report(self, outcome: Literal["done", "failed", "blocked"], summary: str,
               findings: list[Finding] | None = None) -> dict:
        """Record this run's result. Call before stopping, including when blocked or failed."""
        data = ReportInput.model_validate({"outcome": outcome, "summary": summary,
                                           "findings": [] if findings is None else findings})
        b = self.binding
        report_id = self.store.add_report(b.issue_key, b.stage, b.attempt, b.session_id, data.outcome, data.summary,
                                          [f.model_dump(exclude_none=True) for f in data.findings])
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


def create_server(binding: Binding, store: Store) -> MCPServer:
    server = MCPServer("chief-of-stuff-engine")
    tools = EngineTools(binding, store)
    server.tool()(tools.report)
    server.tool()(tools.ask)
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
