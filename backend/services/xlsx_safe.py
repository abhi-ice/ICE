"""Keep user text from turning into live formulas in our spreadsheet exports.

Excel, LibreOffice and Google Sheets treat a cell that starts with = + - @
as a formula. A task code or note typed as '=HYPERLINK("http://evil",..)'
would otherwise run when someone opens the export ("formula injection").

Two helpers:
  - escape_formula(value): for CSV. Puts a single quote in front of a
    dangerous string so the spreadsheet shows it as plain text.
  - set_text_cell(cell, value): for openpyxl. Writes the value unchanged
    but forces the cell to be a text cell, so nothing is shown differently.

Numbers, dates, booleans and None are never touched. Ordinary text that
merely starts with '-' or '+' (like "-5", "- see note", "+1 555 0100")
stays as it is; only text that looks like a formula after the sign is
escaped.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

# Characters that always start a formula (after any leading spaces).
_ALWAYS = ("=", "@")
# Signs that start a formula only when what follows looks like one.
_SIGNS = ("-", "+")
# Control characters that some spreadsheet apps strip before parsing.
_LEADING_CONTROL = ("\t", "\r")

# After a leading - or +, these patterns mean "this is a formula":
#   - a function call:      SUM(  HYPERLINK (  cmd|
#   - a cell reference:     A1  $B$2  Sheet1!C3
#   - DDE / external links: |  !
#   - another operator chain that includes '=' or a quote
_FORMULA_HINT = re.compile(
    r"""
      [A-Za-z_][A-Za-z0-9_.]*\s*\(      # function call
    | (?<![A-Za-z0-9])\$?[A-Za-z]{1,3}\$?[0-9]+(?![A-Za-z0-9])  # cell ref
    | (?<![A-Za-z0-9])R[0-9]+C[0-9]+(?![A-Za-z0-9])            # R1C1 ref
    | [|!="]                            # DDE, sheet refs, embedded = or quotes
    """,
    re.VERBOSE,
)


def is_formula_like(value: Any) -> bool:
    """True when `value` is text a spreadsheet would treat as a formula."""
    if not isinstance(value, str) or not value:
        return False
    if value.startswith(_LEADING_CONTROL):
        return True
    # Any leading whitespace (spaces, tabs, line breaks, non-breaking
    # spaces) can be dropped by the spreadsheet before it parses the cell.
    stripped = value.lstrip()
    if not stripped:
        return False
    first = stripped[0]
    if first in _ALWAYS:
        return True
    if first in _SIGNS:
        return bool(_FORMULA_HINT.search(stripped[1:]))
    return False


def escape_formula(value: Any) -> Any:
    """CSV-safe version of `value`: dangerous text gets a leading single
    quote; everything else is returned unchanged."""
    if is_formula_like(value):
        return "'" + value
    return value


def escape_row(values: Iterable[Any]) -> list:
    """escape_formula applied to every value of a CSV row."""
    return [escape_formula(v) for v in values]


_ILLEGAL_XLSX_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def set_text_cell(cell, value: Any) -> None:
    """Assign `value` to an openpyxl cell. If it is formula-like text, the
    cell is forced to a plain text cell so it is shown, never run.
    Control characters Excel cannot store (a stray vertical tab or form
    feed pasted into a description) are removed instead of failing the
    whole file."""
    if isinstance(value, str):
        value = _ILLEGAL_XLSX_CHARS.sub("", value)
    cell.value = value
    if is_formula_like(value):
        # openpyxl marks "=..." strings as formulas ('f'); 's' stores the
        # text as an ordinary string. quotePrefix makes Excel keep it as
        # text even if someone later edits the cell.
        cell.data_type = "s"
        try:
            cell.quotePrefix = True
        except Exception:
            pass
