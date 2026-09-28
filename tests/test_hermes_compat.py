"""Every Hermes import this plugin makes must still resolve on hermes-agent main.

Hermes closed its plugin-compat window on 2026-09-28 (NousResearch/hermes-agent#126164): an old
import path no longer resolves, and a plugin that uses one fails to load with an ImportError on
someone's gateway. This walks our source with `ast` and checks each `from <hermes module> import
<name>` against the Hermes tree by reading files, not importing them, so it runs even where
Hermes' own dependencies are not installed. Nightly CI runs it against hermes-agent main.
"""
import ast
import os
import sys
from pathlib import Path

import pytest

HERMES_ROOT = Path(os.environ.get("HERMES_AGENT_ROOT") or Path.home() / ".hermes" / "hermes-agent")
PACKAGE = Path(__file__).resolve().parent.parent / "keryx_stream"
HERMES_TOP = {"agent", "gateway", "hermes_cli", "hermes_constants", "hermes_state", "model_tools",
              "run_agent", "toolsets", "tools", "tui_gateway"}

if not (HERMES_ROOT / "hermes_cli").is_dir():
    pytest.skip(f"no hermes-agent tree at {HERMES_ROOT}", allow_module_level=True)


def _module_file(dotted: str) -> Path | None:
    base = HERMES_ROOT.joinpath(*dotted.split("."))
    for cand in (base.with_suffix(".py"), base / "__init__.py"):
        if cand.is_file():
            return cand
    return None


def _top_level_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        names.add(n.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, (ast.If, ast.Try)):   # names defined under a guard still count
            for sub in ast.walk(node):
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names.add(sub.name)
                elif isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                    names.add(sub.id)
                elif isinstance(sub, (ast.Import, ast.ImportFrom)):
                    names.update((a.asname or a.name).split(".")[0] for a in sub.names)
    return names


def _hermes_imports():
    for src in sorted(PACKAGE.glob("*.py")):
        for node in ast.walk(ast.parse(src.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module \
                    and node.module.split(".")[0] in HERMES_TOP:
                yield src.name, node.lineno, node.module, [a.name for a in node.names]
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.split(".")[0] in HERMES_TOP:
                        yield src.name, node.lineno, a.name, []


def test_every_hermes_import_resolves():
    found = list(_hermes_imports())
    assert found, "no Hermes imports found: the scan is looking in the wrong place"
    broken = []
    for fname, line, module, names in found:
        mfile = _module_file(module)
        if mfile is None:
            broken.append(f"{fname}:{line} `{module}` no longer exists")
            continue
        defined = _top_level_names(mfile)
        for name in names:
            if name != "*" and name not in defined and _module_file(f"{module}.{name}") is None:
                broken.append(f"{fname}:{line} `from {module} import {name}`: {module} has no `{name}`")
    assert not broken, "Hermes imports that no longer resolve:\n" + "\n".join(broken)


def test_the_hooks_we_register_still_exist():
    if str(HERMES_ROOT) not in sys.path:
        sys.path.insert(0, str(HERMES_ROOT))
    plugins = pytest.importorskip("hermes_cli.plugins")
    from keryx_stream import PluginConfig, _make_hook_callbacks

    ours = set(_make_hook_callbacks(PluginConfig(), lambda *a: None))
    assert ours <= set(plugins.VALID_HOOKS), f"unknown to Hermes: {sorted(ours - set(plugins.VALID_HOOKS))}"
