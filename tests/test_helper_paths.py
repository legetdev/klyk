"""Prevent inherited PATH from replacing Apple helpers without operating the desktop."""
import ast
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_input_cleanup import load_functions

ROOT = Path(__file__).resolve().parents[1]
COMMANDS = {"open", "log", "osascript", "pbcopy", "pbpaste", "screencapture", "sips", "system_profiler"}


class HelperPathTests(unittest.TestCase):
    """Exercise production clipboard helpers against owned, harmless PATH substitutes."""

    def test_clipboard_helpers_ignore_workspace_path(self):
        """A PATH substitute must never receive clipboard text or supply its result."""
        tree = ast.parse((ROOT / "klyk/computer.py").read_text())
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name in {"get_clipboard", "set_clipboard"}]
        self.assertEqual(len(functions), 2)
        calls = []
        real_run = subprocess.run
        with tempfile.TemporaryDirectory(prefix="klyk-helper-test-") as directory:
            for name in ("pbcopy", "pbpaste"):
                helper = Path(directory) / name
                helper.write_text("#!/bin/sh\nprintf 'WORKSPACE_HELPER_SELECTED\\n'\n")
                helper.chmod(0o700)

            def run(command, **arguments):
                """Only execute our harmless substitute; intercept every real Apple operation."""
                calls.append((command, arguments))
                if command[0] in {"pbcopy", "pbpaste"}:
                    return real_run(command, **arguments)
                if command[0] not in {"/usr/bin/pbcopy", "/usr/bin/pbpaste"}:
                    raise AssertionError("Unexpected native helper")
                return subprocess.CompletedProcess(command, 0, stdout=b"SAFE_NATIVE_RESULT", stderr=b"")

            namespace = {"subprocess": SimpleNamespace(run=run), "_check_stop": lambda: None}
            load_functions("computer.py", {"get_clipboard", "set_clipboard"}, namespace)
            with patch.dict(os.environ, {"PATH": directory}):
                self.assertEqual(namespace["get_clipboard"](), "SAFE_NATIVE_RESULT")
                namespace["set_clipboard"]("synthetic clipboard sentinel")
        self.assertEqual([call[0][0] for call in calls], ["/usr/bin/pbpaste", "/usr/bin/pbcopy"])
        self.assertEqual(calls[1][1]["input"], b"synthetic clipboard sentinel")

    def test_all_apple_helper_argv_use_system_paths(self):
        """Catch a new literal Apple-helper argv that would restore workspace lookup."""
        observed = set()
        for path in (ROOT / "klyk").glob("*.py"):
            tree = ast.parse(path.read_text())
            parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
            for node in ast.walk(tree):
                if not isinstance(node, (ast.List, ast.Tuple)) or not node.elts:
                    continue
                parent = parents.get(node)
                direct = (isinstance(parent, ast.Call) and parent.args and parent.args[0] is node
                          and ((isinstance(parent.func, ast.Attribute)
                                and isinstance(parent.func.value, ast.Name) and parent.func.value.id == "subprocess")
                               or (isinstance(parent.func, ast.Name) and parent.func.id == "_run_process")))
                assigned = (isinstance(parent, ast.Assign) and any(isinstance(target, ast.Name)
                            and target.id in {"cmd", "command", "argv"} for target in parent.targets))
                if not direct and not assigned:
                    continue
                first = node.elts[0]
                if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
                    continue
                name = Path(first.value).name
                if name not in COMMANDS:
                    continue
                with self.subTest(file=path.name, command=name):
                    self.assertTrue(first.value.startswith(("/usr/bin/", "/usr/sbin/")))
                observed.add(name)
        self.assertEqual(observed, COMMANDS)
