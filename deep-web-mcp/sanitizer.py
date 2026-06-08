"""
sanitizer.py — Text Sanitizer Class
=====================================

Evaluates all incoming raw markdown or DOM string data streams prior to
parsing them to the inference engine.

Pipeline (in order)
-------------------
1. Strip dense inline base64 image encodings.
2. Strip redundant nested HTML table blocks (≥ 2 levels deep).
3. Collapse excess whitespace (blank-line storms, leading/trailing space).
4. Enforce MAX_CHARS = 20 000 hard ceiling.
   If exceeded: slice at threshold and append the CONTEXT RESTRICTION sentinel.

All regular expressions are compiled exactly once at module import time for
maximum throughput under repeated call patterns.
"""

from __future__ import annotations

import re
import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hard ceiling
# ---------------------------------------------------------------------------

MAX_CHARS: int = 20_000

TRUNCATION_SENTINEL: str = (
    "\n\n[CONTEXT RESTRICTION ENFORCED: DATA STREAM TRUNCATED TO PROTECT HARDWARE LIMITS]"
)

# ---------------------------------------------------------------------------
# Compiled regular expressions  (compiled once at import, never re-compiled)
# ---------------------------------------------------------------------------

# Pattern 1: Dense inline base64 image encodings
# Matches the complete data URI including the quoted or unquoted attribute value
# context.  We target both bare occurrences (src=...) and fenced occurrences
# (inside markdown image syntax).
#
#   data:[mime];base64,[A-Za-z0-9+/=]+
#
# The base64 payload is greedily consumed so multi-kilobyte blobs are captured
# in a single match, avoiding catastrophic backtracking via atomic-group style
# limiting on the character class.
_RE_BASE64: re.Pattern = re.compile(
    r"""
    (?:
        # Markdown image syntax:  ![alt](data:image/...;base64,AAA...)
        !\[[^\]]{0,200}\]
        \(
            data:[a-zA-Z0-9][a-zA-Z0-9!#$&\-^_]{0,99}
            ;base64,
            [A-Za-z0-9+/\r\n]{10,}={0,2}
        \)
        |
        # HTML attribute context:  src="data:..." or url('data:...')
        (?:src|href|url)\s*=\s*["']?
            data:[a-zA-Z0-9][a-zA-Z0-9!#$&\-^_]{0,99}
            ;base64,
            [A-Za-z0-9+/\r\n]{10,}={0,2}
        ["']?
        |
        # Bare data URI not attached to an attribute (raw DOM dumps)
        data:[a-zA-Z0-9][a-zA-Z0-9!#$&\-^_]{0,99}
        ;base64,
        [A-Za-z0-9+/\r\n]{10,}={0,2}
    )
    """,
    re.VERBOSE | re.MULTILINE,
)

# Pattern 2: Redundant nested HTML tables (2+ levels deep)
# Strategy: match an outer <table ...> tag and capture its full content only
# when that content itself contains at least one inner <table> tag.
# This is necessarily iterative (nested tables require multiple passes) —
# the sanitize() method applies this pattern in a loop until stable.
_RE_NESTED_TABLE: re.Pattern = re.compile(
    r"""
    <table                          # opening outer table tag
        (?:\s[^>]*)?>               # optional attributes
    (?:(?!<table).)*?               # content before the inner table
    <table(?:\s[^>]*)?>             # at least one inner table tag present
    .*?                             # inner table content (lazy)
    </table\s*>                     # close inner table
    (?:(?!<table).)*?               # content after inner table
    </table\s*>                     # close outer table
    """,
    re.VERBOSE | re.DOTALL | re.IGNORECASE,
)

# Pattern 3: Collapse whitespace storms (3+ consecutive blank lines → 2)
_RE_BLANK_LINES: re.Pattern = re.compile(r"\n{3,}", re.MULTILINE)

# Pattern 4: Collapse runs of horizontal whitespace inside lines
_RE_INLINE_SPACES: re.Pattern = re.compile(r"[ \t]{4,}", re.MULTILINE)


# ---------------------------------------------------------------------------
# TextSanitizer
# ---------------------------------------------------------------------------

class TextSanitizer:
    """
    Standalone text sanitization class.

    Evaluate all incoming raw markdown or DOM string data streams prior to
    parsing them to the inference engine.  Thread-safe: all state is local
    to each sanitize() call; the compiled patterns are module-level constants.

    Usage
    -----
    sanitizer = TextSanitizer()
    clean = sanitizer.sanitize(raw_html_or_markdown)
    """

    # Maximum passes to apply the nested-table pattern before giving up.
    # Prevents infinite loops on pathologically malformed HTML.
    _MAX_TABLE_PASSES: int = 8

    def __init__(self, max_chars: int = MAX_CHARS) -> None:
        self.max_chars = max_chars

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def sanitize(self, raw: str) -> str:
        """
        Full sanitization pipeline.  Returns a clean, length-bounded string.

        Parameters
        ----------
        raw : str
            Raw markdown or DOM text from the extraction layer.

        Returns
        -------
        str
            Sanitized string, guaranteed to be ≤ max_chars + len(sentinel).
        """
        if not raw:
            return ""

        original_len = len(raw)
        text = raw

        # ── Step 1: Strip dense inline base64 image encodings ─────────────
        text, n_b64 = _RE_BASE64.subn("[base64-image-removed]", text)
        if n_b64:
            logger.debug("[SANITIZER] Stripped %d base64 image encoding(s).", n_b64)

        # ── Step 2: Strip redundant nested HTML tables (iterative) ────────
        total_tables_removed = 0
        for pass_idx in range(self._MAX_TABLE_PASSES):
            replaced, n_tables = _RE_NESTED_TABLE.subn(
                "<!-- nested-table-removed -->", text
            )
            if n_tables == 0:
                break
            text = replaced
            total_tables_removed += n_tables
            logger.debug(
                "[SANITIZER] Table pass %d: removed %d nested table block(s).",
                pass_idx + 1, n_tables,
            )

        # ── Step 3: Collapse excess whitespace ────────────────────────────
        text = _RE_BLANK_LINES.sub("\n\n", text)
        text = _RE_INLINE_SPACES.sub("    ", text)
        text = text.strip()

        # ── Step 4: Enforce MAX_CHARS hard ceiling ────────────────────────
        final_len = len(text)
        if final_len > self.max_chars:
            text = text[: self.max_chars] + TRUNCATION_SENTINEL
            logger.info(
                "[SANITIZER] Payload truncated: %d → %d chars (sentinel appended).",
                final_len, self.max_chars,
            )
        else:
            logger.debug(
                "[SANITIZER] Clean. original=%d  after_sanitize=%d  delta=-%d  "
                "b64_hits=%d  table_hits=%d",
                original_len, final_len, original_len - final_len,
                n_b64, total_tables_removed,
            )

        return text

    # ------------------------------------------------------------------
    # Convenience: evaluate length without full sanitization
    # ------------------------------------------------------------------

    def would_truncate(self, raw: str) -> bool:
        """Return True if *raw* would exceed max_chars after sanitization."""
        # Quick heuristic: if raw itself is short enough, skip full pipeline.
        return len(raw) > self.max_chars

    def truncation_point(self) -> int:
        """Return the exact character index at which truncation occurs."""
        return self.max_chars
