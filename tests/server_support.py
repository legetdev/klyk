"""Load the real dispatcher with inert native adapters for portable contract tests.

The macOS entry point is deliberately not imported: it starts event taps and writes
user logs. Live tests separately exercise that entry point and the actual adapters.
"""
import ast
import asyncio
import base64
from collections import deque
import difflib
import json
import logging
import os
from pathlib import Path
import time
import traceback
import types as python_types
import unicodedata
import uuid
from unittest.mock import MagicMock

import jsonschema
from mcp import types

ROOT = Path(__file__).resolve().parents[1]


def load_server():
    """Compile unchanged source definitions while replacing only native boundaries."""
    tree = ast.parse((ROOT / 'klyk/mcp_server.py').read_text())
    module = python_types.ModuleType('isolated_klyk_server')
    module.__dict__.update({name: value for name, value in globals().items() if not name.startswith('__')})
    module._jsonschema = jsonschema
    module.log = logging.getLogger('klyk.tests')
    for name in ('computer', 'capture', 'skylight', 'ownership', 'registry', 'activity',
                 'window_labels', 'reporter_mod', 'ocr', 'matcher', '_ui'):
        setattr(module, name, MagicMock())
    module._BROWSER_INTERACTIVE_ROLES = set()
    module._INTERACTIVE_ROLES = set()
    module.is_browser = lambda app: False
    module.is_chromium_renderer_app = lambda *args: False
    module.CHROMIUM_BROWSERS = set()
    names = {'_HYPHEN_VARIANTS', '_SERVER_INSTRUCTIONS', '_APP_PARAM', '_APP_LAUNCH_PARAMS', '_CONFIRM_DESTRUCTIVE',
             '_WINDOW_ID_PARAM', '_VERIFY_PARAM', 'TOOLS', '_last_response_time', '_call_depth',
             '_HINT_HISTORY_CAP', '_call_history', '_BATCHABLE_ACTIONS', '_OBSERVATION_TOOLS',
             '_POST_ACTION_SETTLE_MS', '_PASSIVE_SETTLE_MS', '_last_action_mutated',
             '_OWNERSHIP_EXEMPT', '_chromium_based_cache'}
    nodes = [ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)]
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            node.decorator_list = []
            nodes.append(node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id in names for t in targets):
                nodes.append(node)
    compiled = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
    exec(compile(compiled, str(ROOT / 'klyk/mcp_server.py'), 'exec'), module.__dict__)
    module._TOOL_SCHEMAS = {t.name: module._tool_input_schema(t) for t in module.TOOLS}
    module._TOOL_VALIDATORS = {n: jsonschema.validators.validator_for(s)(s) for n,s in module._TOOL_SCHEMAS.items()}
    module._refresh_menubar = MagicMock()
    return module


def payload(result):
    """Return the last JSON text payload without confusing images with outcomes."""
    return json.loads(next(item.text for item in reversed(result) if isinstance(item, types.TextContent)))
