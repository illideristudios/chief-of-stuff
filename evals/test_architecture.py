"""What reads and writes the workspace never loads what draws it.

The page modules render HTML. Everything else — the tracker and coordinator models, the hook that runs before
every tool call, the launchers, the audit — has to import without them. When the tracker parser lived in the
board renderer, `chief-of-stuff models --class` failed on a syntax error in the decision page, and the
PreToolUse hook loaded the board, the page server and the forge cache to read one `Board:` line.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

PAGES = frozenset({"render_board", "decision_page", "issue_page", "source_page", "workers_page", "pages",
                   "flow_chart", "gantt", "panels", "columns", "fragment", "page_common", "module_graph", "page_reload"})

# The leaf page modules: each imports at most fragment (the shared escaping), so a chart or the reload check lifts out with it.
LEAVES = ("fragment", "gantt", "columns", "panels", "page_reload")

# Every script module that draws nothing. A new one belongs here unless it renders a page.
DRAWS_NOTHING = (
    "md", "clock", "workspace", "tracker", "settings", "backlog_ref", "backlog", "forge_review",
    "board_guard", "board_sources", "tracker_write", "make_worktree", "dispatch_prompt", "kanban", "kanban_cli", "pipeline_cli", "audit_tasks",
    "one_shot", "spawn_session", "session_exec", "shell_setup", "process_status", "one_shot_result", "adopt_one_shots", "merge_approved", "merge_ready",
    "review_threads", "notify", "init_workspace", "install_model_guidance", "migrate_backlog", "inbox",
    "probe_health", "runtimes", "estimate", "task_forge", "tracker_log", "findings", "tree_state", "orphans",
    "git_trees", "git_view", "tree_claims", "notify_service", "tracker_read",
    "one_shot_report", "flow", "sync_page_theme", "store", "store_access", "private_state",
    "model_port",
    "claude_adapter",
    "host_adapter",
    "policy",
    "forge",
    "forge_port",
    "engine_merge",
    "issue_gate",
    "trio_gate",
    "decision_actions",
    "decision_escalation",
    "performance",
    "performance_report",
    "limits", "limits_history", "limits_probe", "limits_cli", "session_logs", "github_admission", "github_graphql_transport", "github_scan",
    "supervisor", "release_selection", "engine_intent",
    "startup",
    "guard_commands",  # Entities: pure shell command gate judgements.
    "guard_rules",  # Use Cases: pure Stop, permission-ask and resume judgements.
    "guard_binding",  # Frameworks and Drivers: launch env and plugin paths.
    "worker_guard",  # Frameworks and Drivers: command hook JSON and store access.
    "engine_mcp",  # Frameworks and Drivers: MCP stdio tools over the store gateway.

    "slots",
    "reconcile",
    "readiness",
    "completion",
    "worker_health",
    "cleanup",
    "engine",
    "engine_facts",
    "worker",
    "guard_commands",  # Entities: pure shell command gate judgements.
    "guard_rules",  # Use Cases: pure Stop, permission-ask and resume judgements.
    "guard_binding",  # Frameworks and Drivers: launch env and plugin paths.
    "worker_guard",  # Frameworks and Drivers: command hook JSON and store access.
    "engine_mcp",  # Frameworks and Drivers: MCP stdio tools over the store gateway.
    "work",
    "decision_actions",  # Use Cases: pure decision lifecycle transitions.
    "decision_escalation",  # Use Cases: pure trio-retry, owner-wait and ask-link judgements.
)

PROBE = "import sys; sys.path.insert(0, sys.argv[1]); import {name}; print(' '.join(sorted(set(sys.modules) & set(sys.argv[2:]))))"

# The forge client's HTTP stack. Reading a `Backlog:` line needs none of it, and the hook runs before every tool call.
HTTP = ("urllib.request", "http.client", "ssl")

# The files that load script modules package-style, as `scripts.X`, with the repo root on the path and not scripts/.
PACKAGE_STYLE = ("start_coordinator.py", "chief_of_stuff.py", "evals/run.py")


def imports(path: Path) -> set[str]:
    """Every module a file imports, at the top or inside a function. `from scripts import x` counts as `scripts.x`.
    Package-relative forms (`from . import forge`, `from .forge import X`) count as the sibling stem.
    `from scripts.migrations import env` counts as `scripts.migrations.env` (path key after circle_target)."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module == "scripts":
            names.update(f"scripts.{alias.name}" for alias in node.names if alias.name != "*")
        elif isinstance(node, ast.ImportFrom) and node.level:
            if node.module:
                names.add(node.module.split(".")[0])
            else:
                names.update(alias.name for alias in node.names if alias.name != "*")
        elif isinstance(node, ast.ImportFrom) and node.module and (
                node.module == "migrations" or node.module.startswith("migrations.")
                or node.module.startswith("scripts.migrations")):
            for alias in node.names:
                names.add(node.module if alias.name == "*" else f"{node.module}.{alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def script(name: str) -> str:
    """The scripts/ module an import names, whether spelled `x` or `scripts.x`."""
    return name.removeprefix("scripts.").split(".")[0]


def import_graph(scripts: Path) -> dict[str, set[str]]:
    """The script modules each script module imports, by either spelling, wherever the import sits."""
    stems = {path.stem for path in scripts.glob("*.py")}
    return {path.stem: ({script(name) for name in imports(path)} & stems) - {path.stem}
            for path in scripts.glob("*.py")}


def forbidden_reach(scripts: Path, start: str, banned_scripts: set[str], banned_names: frozenset[str]) -> list[str]:
    """`<module> imports <name>` for each import, at the top or inside a function, of `start` or of any script it reaches,
    that names a banned script or one of the banned modules."""
    graph, seen, todo, found = import_graph(scripts), set(), [start], []
    while todo:
        module = todo.pop()
        if module in seen:
            continue
        seen.add(module)
        todo.extend(graph[module])
        found += [f"{module} imports {name}" for name in sorted(imports(scripts / f"{module}.py"))
                  if name in banned_names or script(name) in banned_scripts]
    return sorted(found)


# What the report reader must not reach: the launcher, the backlog client, and the forge client's HTTP stack. A bare `urllib` or
# `http` is `from urllib import request`; `urllib.parse` is not on the list.
READER_BANS = ({"one_shot", "backlog"}, frozenset({*HTTP, "urllib", "http"}))

# Design 7a and §4/§13: only the host adapters launch a host CLI, and the registry is where they learn the executable.
ADAPTER = ("scripts/claude_adapter.py", "scripts/host_adapter.py", "scripts/runtimes.py")
# The modules that named a host executable before the adapter. The list may only shrink; each entry must still name one.
# session_exec execs the worker CLI for spawn_session, and process_status matches the executable to check liveness.
# evals/run.py remains exempt only for its non-Claude backends until 1.1.
LEGACY_LAUNCHERS = ("scripts/one_shot.py", "scripts/spawn_session.py", "start_coordinator.py", "evals/run.py",
                    "scripts/session_exec.py", "scripts/process_status.py")
HOST_EXECUTABLES = frozenset({"claude", "claude-as", "codex", "agent", "agy"})


def _is_executable(nodes: list[ast.expr]) -> bool:
    return bool(nodes) and isinstance(nodes[0], ast.Constant) and nodes[0].value in HOST_EXECUTABLES


def names_a_host_executable(path: Path) -> list[str]:
    """`<file>:<line>` wherever a file reaches a host CLI's executable: a call to the registry's `program` or `binary`,
    a record's `.binary`, an argv literal or a `which`/`resolve` that starts with an executable's name, or `claude-as`.
    Comparing a runtime's name, as in `runtime == "claude"` or `runtime in ("codex", "agy")`, is not reaching it."""
    tree, found = ast.parse(path.read_text(encoding="utf-8"), filename=str(path)), []
    names = {id(side) for node in ast.walk(tree) if isinstance(node, ast.Compare) for side in node.comparators}
    for node in ast.walk(tree):
        if id(node) in names:  # `runtime in ("codex", "cursor")`
            continue
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            hit = name in ("program", "binary") or (name in ("which", "resolve") and _is_executable(node.args))
        elif isinstance(node, (ast.List, ast.Tuple)):
            hit = _is_executable(node.elts)
        elif isinstance(node, ast.Attribute):
            hit = node.attr == "binary"
        else:
            hit = isinstance(node, ast.Constant) and node.value == "claude-as"
        if hit:
            found.append(f"{path.name}:{node.lineno}")
    return found


def launch_sources() -> list[str]:
    """Every file that could launch a host: the scripts, the launchers at the root, the hooks and the eval runner."""
    paths = [*SCRIPTS.glob("*.py"), *ROOT.glob("*.py"), *(ROOT / "hooks").glob("*.py"), ROOT / "evals" / "run.py"]
    return sorted(str(path.relative_to(ROOT)) for path in paths)


def cycle_in(graph: dict[str, set[str]]) -> list[str]:
    """One import cycle as `[a, b, ..., a]`, or `[]` when the graph has none."""
    done: set[str] = set()
    path: list[str] = []

    def visit(node: str) -> list[str]:
        if node in path:
            return path[path.index(node):] + [node]
        if node in done:
            return []
        path.append(node)
        for nxt in sorted(graph[node]):
            if found := visit(nxt):
                return found
        path.pop()
        done.add(node)
        return []

    for node in sorted(graph):
        if found := visit(node):
            return found
    return []


# Section 4 circle table. Split modules (audit_tasks, inbox, flow) sit in the outermost circle
# they occupy until the §8 split lands. Root launchers are Drivers.
CIRCLES: dict[str, frozenset[str]] = {
    "entities": frozenset({
        "tracker", "md", "clock", "runtimes", "policy", "findings", "orphans",
        "estimate", "tracker_log", "tree_state", "guard_commands", "limits",
    }),
    "use_cases": frozenset({
        "dispatch_prompt", "one_shot", "kanban", "merge_ready", "merge_approved", "review_threads",
        "task_forge", "model_port", "issue_gate", "trio_gate", "decision_actions", "decision_escalation", "engine_merge",
        "forge_port",
        "reconcile", "readiness", "completion", "worker_health", "guard_rules", "performance",
    }),
    "adapters": frozenset({
        "fragment", "page_common", "columns", "panels", "gantt", "flow_chart", "render_board", "decision_page",
        "issue_page", "source_page", "module_graph", "workers_page", "page_reload", "store", "forge", "private_state",
        "backlog", "backlog_ref", "board_sources", "workspace", "settings", "tracker_read",
        "tracker_write", "forge_review", "one_shot_report", "tree_claims", "slots", "limits_history", "session_logs", "github_admission", "github_graphql_transport", "github_scan",
        # §4: store's migrations/ — path keys, not top-level stems.
        "migrations/env",
        "migrations/versions/0001_initial",
        "migrations/versions/0002_segment_runner",
        "migrations/versions/0003_worker_channel",
        "migrations/limits_versions/0001_limits",
        "migrations/versions/0004_report_suite",
        "migrations/versions/0005_segment_human_review",
        "migrations/versions/0006_log_operation",
        "migrations/versions/0007_engine_log",
        "migrations/versions/0008_withdraw_scope_decisions",
        "migrations/versions/0009_drop_ownership",
        "migrations/versions/0010_worker_messages",
    }),
    "drivers": frozenset({
        "claude_adapter", "host_adapter", "git_trees", "git_view", "notify", "notify_service", "supervisor", "release_selection", "engine_intent",
        "startup", "store_access", "kanban_cli", "pipeline_cli", "process_status", "session_exec", "spawn_session",
        "probe_health", "pages", "chief_of_stuff", "start_coordinator", "board_guard",
        "init_workspace", "install_model_guidance", "make_worktree", "migrate_backlog",
        "one_shot_result", "shell_setup", "cleanup", "engine", "engine_facts", "work", "worker", "worker_guard",
        "guard_binding", "engine_mcp", "adopt_one_shots", "sync_page_theme", "performance_report",
        "audit_tasks", "inbox", "flow", "limits_probe", "limits_cli",
        "hooks/hooks.json", "guard/hooks/hooks.json",
    }),
}
CIRCLE_RANK = {"entities": 0, "use_cases": 1, "adapters": 2, "drivers": 3}
MODULE_CIRCLE = {name: circle for circle, names in CIRCLES.items() for name in names}

# Section 5: inward→outward imports known today. The set may only shrink; each entry is a compliance gap.
OUTWARD_IMPORTS = frozenset({
    "board_sources→process_status",
    "decision_page→notify_service",
    "dispatch_prompt→backlog_ref",
    "dispatch_prompt→settings",
    "dispatch_prompt→workspace",
    "estimate→workspace",
    "kanban→backlog",
    "kanban→settings",
    "merge_approved→backlog",
    "merge_approved→forge_review",
    "merge_approved→settings",
    "merge_approved→workspace",
    "merge_ready→backlog",
    "merge_ready→forge_review",
    "merge_ready→settings",
    "merge_ready→workspace",
    "module_graph→git_trees",
    "one_shot→backlog",
    "one_shot→forge",
    "one_shot→git_trees",
    "one_shot→git_view",
    "one_shot→notify_service",
    "one_shot→one_shot_report",
    "one_shot→process_status",
    "one_shot→settings",
    "one_shot→shell_setup",
    "one_shot→tracker_write",
    "one_shot→workspace",
    "review_threads→backlog",
    "review_threads→forge_review",
    "review_threads→settings",
    "review_threads→workspace",
    "task_forge→board_sources",
    "task_forge→settings",
    "tracker→backlog_ref",
    "tracker→workspace",
})


LAUNCHERS = frozenset({"chief_of_stuff", "start_coordinator"})


def discovered_hooks() -> frozenset[str]:
    """Every `hooks/hooks.json` under the repo root, excluding vendored trees."""
    skip = {".venv", "_vendor", "node_modules", ".git"}
    return frozenset(
        p.relative_to(ROOT).as_posix()
        for p in ROOT.glob("**/hooks/hooks.json")
        if not skip.intersection(p.parts)
    )


def migration_paths() -> dict[str, Path]:
    """`migrations/…` key → file, for store's Adapter migrations (§4)."""
    root = SCRIPTS / "migrations"
    if not root.is_dir():
        return {}
    return {p.relative_to(SCRIPTS).as_posix().removesuffix(".py"): p
            for p in root.rglob("*.py")}


def circle_target(name: str) -> str:
    """Map an import name to a circle-graph key; migrations keep their path form."""
    raw = name.removeprefix("scripts.")
    if raw == "migrations" or raw.startswith("migrations."):
        return raw.replace(".", "/")
    return raw.split(".")[0]


def circle_graph() -> dict[str, set[str]]:
    """Import graph over top-level scripts/, store migrations, and root launchers.

    Launchers are both sources and targets. Migration modules use path keys under
    `migrations/` and count as Adapters with store.
    """
    migrations = migration_paths()
    targets = {p.stem for p in SCRIPTS.glob("*.py")} | LAUNCHERS | set(migrations)
    graph: dict[str, set[str]] = {}
    for path in SCRIPTS.glob("*.py"):
        graph[path.stem] = ({circle_target(name) for name in imports(path)} & targets) - {path.stem}
    for key, path in migrations.items():
        graph[key] = ({circle_target(name) for name in imports(path)} & targets) - {key}
    for stem in LAUNCHERS:
        path = ROOT / f"{stem}.py"
        graph[stem] = ({circle_target(name) for name in imports(path)} & targets) - {stem}
    return graph


def outward_imports(graph: dict[str, set[str]] | None = None) -> set[str]:
    """`src→dst` for each direct import from an inner circle to an outer one."""
    graph = circle_graph() if graph is None else graph
    found: set[str] = set()
    for src, deps in graph.items():
        src_circle = MODULE_CIRCLE.get(src)
        if src_circle is None:
            continue
        for dst in deps:
            dst_circle = MODULE_CIRCLE.get(dst)
            if dst_circle is None:
                continue
            if CIRCLE_RANK[src_circle] < CIRCLE_RANK[dst_circle]:
                found.add(f"{src}→{dst}")
    return found


class DependencyRuleTest(unittest.TestCase):
    def test_every_script_launcher_and_hook_is_in_exactly_one_circle(self) -> None:
        hooks = discovered_hooks()
        inventory = ({p.stem for p in SCRIPTS.glob("*.py")} | set(LAUNCHERS) | set(hooks)
                     | set(migration_paths()))
        listed = {name for names in CIRCLES.values() for name in names}
        self.assertEqual(sorted(inventory - listed), [], "unclassified module")
        self.assertEqual(sorted(listed - inventory), [], "circle entry with no file")
        self.assertEqual({n for n in listed if n.endswith("hooks/hooks.json")}, set(hooks))
        overlap = [name for name, n in ((n, sum(n in s for s in CIRCLES.values())) for n in listed) if n > 1]
        self.assertEqual(overlap, [])

    def test_nested_scripts_are_only_migrations_or_vendor(self) -> None:
        # Migrations are Adapter circle entries; _vendor is third-party and unlisted.
        allowed = ("migrations/", "_vendor/")
        nested = sorted(p.relative_to(SCRIPTS).as_posix()
                        for p in SCRIPTS.rglob("*.py") if p.parent != SCRIPTS)
        self.assertTrue(any(path.startswith("migrations/") for path in nested), nested)
        self.assertEqual([path for path in nested if not path.startswith(allowed)], [])

    def test_inward_to_outward_imports_match_the_known_list(self) -> None:
        # §5: fail on a new outward import; the allowlist may only shrink.
        self.assertEqual(outward_imports(), set(OUTWARD_IMPORTS))

    def test_the_outward_check_sees_an_inner_import_of_an_outer_module(self) -> None:
        graph = {"tracker": {"forge"}, "forge": set()}
        self.assertEqual(outward_imports(graph), {"tracker→forge"})

    def test_the_outward_check_sees_an_import_of_a_root_launcher(self) -> None:
        graph = {"tracker": {"chief_of_stuff"}, "chief_of_stuff": set()}
        self.assertEqual(outward_imports(graph), {"tracker→chief_of_stuff"})

    def test_imports_sees_package_relative_sibling_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "tracker.py")
            path.write_text("from . import forge\nfrom .settings import load\n", encoding="utf-8")
            self.assertEqual(imports(path), {"forge", "settings"})

    def test_a_migration_import_of_a_driver_is_outward(self) -> None:
        graph = {"migrations/env": {"process_status"}, "process_status": set()}
        self.assertEqual(outward_imports(graph), {"migrations/env→process_status"})

    def test_circle_target_keeps_migration_path_keys(self) -> None:
        self.assertEqual(circle_target("migrations.env"), "migrations/env")
        self.assertEqual(circle_target("scripts.migrations.versions.0001_initial"),
                         "migrations/versions/0001_initial")
        self.assertEqual(circle_target("forge.client"), "forge")

    def test_from_scripts_migrations_import_alias_is_a_path_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "tracker.py")
            path.write_text("from scripts.migrations import env\n", encoding="utf-8")
            self.assertEqual(imports(path), {"scripts.migrations.env"})
            self.assertEqual(circle_target("scripts.migrations.env"), "migrations/env")

    def test_guard_command_judgements_import_no_other_script_or_io(self) -> None:
        self.assertEqual(import_graph(SCRIPTS)["guard_commands"], set())
        self.assertEqual(forbidden_reach(SCRIPTS, "guard_commands", set(),
                         frozenset({"subprocess", "sqlite3", "sqlalchemy", "mcp", *HTTP})), [])

    def test_guard_rules_import_no_other_script_or_io(self) -> None:
        self.assertEqual(import_graph(SCRIPTS)["guard_rules"], set())
        self.assertEqual(forbidden_reach(SCRIPTS, "guard_rules", set(),
                         frozenset({"subprocess", "sqlite3", "sqlalchemy", "mcp", *HTTP})), [])


    def test_reconcile_reaches_no_adapter_or_io_module(self) -> None:
        self.assertEqual(forbidden_reach(SCRIPTS, "reconcile",
                         {"store", "forge", "claude_adapter", "settings", "trio_gate"},
                         frozenset({"subprocess", "socket", "sqlite3", "sqlalchemy", *HTTP})), [])

    def test_decision_actions_reaches_no_adapter_or_io_module(self) -> None:
        self.assertEqual(forbidden_reach(SCRIPTS, "decision_actions",
                         {"store", "store_access", "forge", "claude_adapter", "settings", "trio_gate", "pages"},
                         frozenset({"subprocess", "socket", "sqlite3", "sqlalchemy", *HTTP})), [])

    def test_decision_escalation_reaches_no_adapter_or_io_module(self) -> None:
        self.assertEqual(forbidden_reach(SCRIPTS, "decision_escalation",
                         {"store", "store_access", "forge", "claude_adapter", "settings", "trio_gate", "pages"},
                         frozenset({"subprocess", "socket", "sqlite3", "sqlalchemy", *HTTP})), [])

    def test_every_script_is_classified(self) -> None:
        scripts = {p.stem for p in SCRIPTS.glob("*.py")}
        self.assertEqual(sorted(scripts - PAGES - set(DRAWS_NOTHING)), [])

    def test_a_module_that_emits_markup_is_a_page_module(self) -> None:
        # page_reload holds a <script> that draws a notice; listed as drawing nothing, its probe passes for want of imports.
        drawing = [n for n in DRAWS_NOTHING if re.search(r"<(script|div|p|body)\b", (SCRIPTS / f"{n}.py").read_text(encoding="utf-8"))]
        self.assertEqual(drawing, [])

    def test_a_module_that_draws_nothing_imports_no_page(self) -> None:
        for name in DRAWS_NOTHING:
            with self.subTest(module=name):
                proc = subprocess.run([sys.executable, "-c", PROBE.format(name=name), str(SCRIPTS), *sorted(PAGES)],
                                      capture_output=True, text=True, timeout=60)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout.split(), [], f"{name} loads page modules")

    def test_a_leaf_page_module_imports_nothing_but_the_shared_fragment(self) -> None:
        # page_reload moved into PAGES, which only says it draws; this says what it may reach. Any import, at the top or in a function.
        graph = import_graph(SCRIPTS)
        for name in LEAVES:
            with self.subTest(module=name):
                self.assertEqual(sorted(graph[name] - {"fragment"}), [], f"{name} imports a plugin module beyond fragment")

    def test_the_leaf_check_sees_an_import_in_a_leaf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "leaf.py").write_text("def f():\n    import backlog\n", encoding="utf-8")
            Path(tmp, "backlog.py").write_text("", encoding="utf-8")
            self.assertEqual(sorted(import_graph(Path(tmp))["leaf"] - {"fragment"}), ["backlog"])

    def test_the_hook_loads_no_http_client(self) -> None:
        proc = subprocess.run([sys.executable, "-c", PROBE.format(name="board_guard"), str(SCRIPTS), *HTTP],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.split(), [], "board_guard loads the forge client")

    def test_the_report_reader_loads_neither_the_launcher_nor_an_http_client(self) -> None:
        # `result` reads a file. Importing the launcher for a path constant once loaded ten modules and the forge client with it.
        proc = subprocess.run([sys.executable, "-c", PROBE.format(name="one_shot_result"), str(SCRIPTS), "one_shot", "urllib.request"],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.split(), [], "one_shot_result loads the launcher or the forge client")


class ModelAdapterTest(unittest.TestCase):
    """Design 7a and plan M3 item 7: nothing outside the model adapter and the registry names a host's executable."""

    def test_only_the_adapter_the_registry_and_the_legacy_launchers_name_a_host_executable(self):
        naming = [path for path in launch_sources() if names_a_host_executable(ROOT / path)]
        self.assertEqual(sorted(set(naming) - set(ADAPTER) - set(LEGACY_LAUNCHERS)), [])

    def test_every_legacy_launcher_still_names_one_so_the_list_only_shrinks(self):
        for path in LEGACY_LAUNCHERS:
            with self.subTest(path=path):
                self.assertTrue(names_a_host_executable(ROOT / path), f"{path} no longer launches a host: drop it from the list")

    def test_the_check_sees_each_way_of_reaching_an_executable_and_not_a_runtime_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "m.py")
            for source in ('subprocess.run(["claude", "-p"])', 'shutil.which("claude")', 'runtimes.program("claude")',
                           'runtimes.get("codex").binary', 'resolve("agy")', 'x = "claude-as"'):
                with self.subTest(source=source):
                    path.write_text(source + "\n", encoding="utf-8")
                    self.assertEqual(names_a_host_executable(path), ["m.py:1"])
            path.write_text('if runtime == "claude" or runtime in ("codex", "agy"):\n    host = runtimes.get(runtime).display\n',
                            encoding="utf-8")
            self.assertEqual(names_a_host_executable(path), [])


class ImportGraphTest(unittest.TestCase):
    """The probe above sees only what an import loads at once. A function-local import hides from it, which is
    how two cycles once survived it, so these read every import in the source instead."""

    def test_the_report_reader_reaches_neither_the_launcher_nor_an_http_client_by_any_import(self) -> None:
        self.assertEqual(forbidden_reach(SCRIPTS, "one_shot_result", *READER_BANS), [])

    def test_the_reach_check_sees_a_function_local_import_in_the_module_and_in_one_it_reaches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "a.py").write_text("import b\n\n\ndef main():\n    import backlog, http.client\n", encoding="utf-8")
            Path(tmp, "b.py").write_text("def f():\n    from urllib import request\n    from urllib.parse import quote\n", encoding="utf-8")
            Path(tmp, "backlog.py").write_text("", encoding="utf-8")
            self.assertEqual(forbidden_reach(Path(tmp), "a", *READER_BANS),
                             ["a imports backlog", "a imports http.client", "b imports urllib"])

    def test_the_scripts_import_each_other_in_no_cycle(self) -> None:
        cycle = cycle_in(import_graph(SCRIPTS))
        self.assertEqual(cycle, [], "import cycle: " + " -> ".join(cycle))

    def test_the_cycle_check_sees_a_function_local_package_style_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "a.py").write_text("def f():\n    from scripts.b import g\n", encoding="utf-8")
            Path(tmp, "b.py").write_text("from scripts import c\n", encoding="utf-8")
            Path(tmp, "c.py").write_text("import a\n", encoding="utf-8")
            self.assertEqual(cycle_in(import_graph(Path(tmp))), ["a", "b", "c", "a"])

    def test_a_module_loaded_as_scripts_x_imports_nothing_from_the_plugin(self) -> None:
        # The scripts load the same file by bare name with scripts/ on the path, and the package-style importers
        # load it as `scripts.X` without scripts/ on the path. No spelling of a sibling import works in both.
        stems = {path.stem for path in SCRIPTS.glob("*.py")}
        loaded = {script(name) for entry in PACKAGE_STYLE for name in imports(ROOT / entry)
                  if name.startswith("scripts.")} & stems
        self.assertTrue(loaded, "found no `scripts.X` import to check")
        graph = import_graph(SCRIPTS)
        for name in sorted(loaded):
            with self.subTest(module=name):
                self.assertEqual(sorted(graph[name]), [], f"{name} is loaded as scripts.{name}")


if __name__ == "__main__":
    unittest.main()
