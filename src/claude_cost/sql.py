"""SQL fragments shared by the query service, Compact analysis, cache classification and
the findings, so each formula is written once. All assume `requests` aliased `r`."""
from __future__ import annotations

# Cost components: (display name, `requests` column). The one list everything derives from.
COMPONENTS = (("input", "cost_input"), ("output", "cost_output"),
              ("cache write (5m)", "cost_cache_write_5m"),
              ("cache write (1h)", "cost_cache_write_1h"), ("cache read", "cost_cache_read"))

COST_SQL = " + ".join(col for _, col in COMPONENTS)

# Prompt size of a request: everything the model read or wrote to the cache.
CONTEXT_SQL = ("r.input_tokens + r.cache_read_tokens + r.cache_write_5m_tokens "
               "+ r.cache_write_1h_tokens")

# Stream readers (cache events, Stream facts, Compact analysis, Setting changes, Concurrency,
# findings) skip Advisor calls: they have no prompt cache and do not grow the context (ADR-0009).
NOT_ADVISOR_SQL = "r.is_advisor = 0"

# Resolve a request's transcript stem to its logical Session (Desktop-store links).
SESSION_JOIN = "LEFT JOIN session_links l ON l.stem = r.session_id"
SESSION_ID_SQL = "COALESCE(l.logical_session_id, r.session_id)"


def context_tokens(input: int, cache_read: int, write_5m: int, write_1h: int) -> int:
    """Python twin of CONTEXT_SQL."""
    return input + cache_read + write_5m + write_1h
