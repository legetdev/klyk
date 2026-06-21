"""
AX role catalogs — single source of truth for "what counts as interactive UI."

Two related sets live here so they can't drift independently:

  INTERACTIVE_ROLES         — the broad set surfaced in ax_snapshot. Includes
                              structural elements (tables, headings, static
                              text) so callers can inspect layout context.
  BROWSER_INTERACTIVE_ROLES — the narrower set used when filtering a browser
                              AX tree (which is dominated by structural noise).
                              Derived from INTERACTIVE_ROLES minus the
                              structural roles so adding a new interactive
                              role automatically reaches both call sites.

Resolves Consideration #8 (two sources of truth) — previously these were two
separate frozen sets in computer.py and mcp_server.py.
"""

from __future__ import annotations

# All AX roles the snapshot collector considers "interesting." Used by
# computer.py's element walker to decide which elements to emit.
INTERACTIVE_ROLES: frozenset[str] = frozenset({
    "AXButton", "AXTextField", "AXTextArea", "AXCheckBox", "AXRadioButton",
    "AXPopUpButton", "AXComboBox", "AXSlider", "AXMenuItem", "AXMenuBarItem",
    "AXLink", "AXStaticText", "AXHeading", "AXTab", "AXTabGroup",
    "AXTable", "AXRow", "AXCell", "AXToolbar", "AXSearchField",
    "AXDisclosureTriangle", "AXOutline", "AXBrowser", "AXSplitter",
    "AXSegmentedControl", "AXMenuButton",
})

# Structural roles — present in INTERACTIVE_ROLES but NOT clickable/typable.
# Subtracted from the interactive set when filtering browser AX trees, where
# these roles dominate the output and bury the real click targets.
_STRUCTURAL_ROLES: frozenset[str] = frozenset({
    "AXStaticText", "AXHeading", "AXTabGroup",
    "AXTable", "AXRow", "AXCell",
    "AXDisclosureTriangle", "AXOutline", "AXBrowser", "AXSplitter",
})

# What survives the browser filter — true click/type/pick targets only.
BROWSER_INTERACTIVE_ROLES: frozenset[str] = INTERACTIVE_ROLES - _STRUCTURAL_ROLES
