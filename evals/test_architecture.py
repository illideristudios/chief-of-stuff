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
                   "flow_chart", "gantt", "panels", "columns", "fragment", "module_graph", "page_reload"})

# The leaf page modules: each imports at most fragment (the shared escaping), so a chart or the reload check lifts out with it.
LEAVES = ("fragment", "gantt", "columns", "panels", "page_reload")

# Every script module that draws nothing. A new one belongs here unless it renders a page.
DRAWS_NOTHING = (
    "md", "clock", "workspace", "tracker", "settings", "backlog_ref", "backlog", "ownership", "forge_review",
    "board_guard", "board_sources", "tracker_write", "make_worktree", "dispatch_prompt", "kanban", "audit_tasks",
    "one_shot", "spawn_session", "session_exec", "shell_setup", "process_status", "one_shot_result", "adopt_one_shots", "merge_approved", "merge_ready",
    "review_threads", "notify", "init_workspace", "install_model_guidance", "migrate_backlog", "inbox",
    "probe_health", "runtimes", "estimate", "task_forge", "tracker_log", "findings", "tree_state", "orphans",
    "git_trees", "git_view", "tree_claims", "notify_service", "tracker_read",
    "one_shot_report", "flow", "sync_page_theme", "store", "store_access",
    "model_port",
    "claude_adapter",
    "policy",
    "forge",
    "forge_port",
    "engine_merge",
    "issue_gate",
    "trio_gate",
    "performance",
    "performance_report",
    "supervisor",
    "slots",
    "reconcile",
    "cleanup",
    "engine",
    "work",
)

PROBE = "import sys; sys.path.insert(0, sys.argv[1]); import {name}; print(' '.join(sorted(set(sys.modules) & set(sys.argv[2:]))))"

# The forge client's HTTP stack. Reading a `Backlog:` line needs none of it, and the hook runs before every tool call.
HTTP = ("urllib.request", "http.client", "ssl")

# The files that load script modules package-style, as `scripts.X`, with the repo root on the path and not scripts/.
PACKAGE_STYLE = ("start_coordinator.py", "chief_of_stuff.py", "evals/run.py")


def imports(path: Path) -> set[str]:
    """Every module a file imports, at the top or inside a function. `from scripts import x` counts as `scripts.x`."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module == "scripts":
            names.update(f"scripts.{alias.name}" for alias in node.names)
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

# Design 7a: only the model adapter launches a host CLI, and the registry is where it learns the executable.
ADAPTER = ("scripts/claude_adapter.py", "scripts/runtimes.py")
# The modules that named a host executable before the adapter. The list may only shrink; each entry must still name one.
# session_exec execs the worker CLI for spawn_session, and process_status matches the executable to check liveness.
# probe_health finds each rotation's executable for the health line.
LEGACY_LAUNCHERS = ("scripts/one_shot.py", "scripts/spawn_session.py", "start_coordinator.py", "evals/run.py",
                    "scripts/session_exec.py", "scripts/process_status.py", "scripts/probe_health.py")
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


class DependencyRuleTest(unittest.TestCase):
    def test_reconcile_reaches_no_adapter_or_io_module(self) -> None:
        self.assertEqual(forbidden_reach(SCRIPTS, "reconcile",
                         {"store", "forge", "claude_adapter", "settings", "trio_gate"},
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
