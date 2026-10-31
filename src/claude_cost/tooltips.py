"""Hover-tooltip wording for dashboard metrics, keyed by metric and worded from GLOSSARY.md.

One source for every page, so a metric is explained the same way everywhere. Keys are the
Cost component names, plus the Cache metric names (lower case).
"""
from __future__ import annotations

_5M = ("Subagents and other background requests (forks, compaction) always use the 5-minute "
       "cache by default. The main conversation also uses it on an API key, or once a "
       "subscription runs past its included usage.")
_1H = ("Your main conversation, on a Claude subscription within its included usage. The cache "
       "survives breaks of up to an hour, so a short break doesn't trigger a Cold write.")

TOOLTIPS = {
    "sessions": ("Sessions that made at least one request in the period. A Session is one logical "
                 "conversation, which may span several transcripts through resumes and clears."),
    "turns": ("Prompts you typed into a session, including slash commands and messages queued "
              "while Claude was working. Tool results, compaction summaries, hook output and "
              "Subagent messages are not Turns."),
    "requests": "Billed API calls in the period, each counted once however many transcripts hold a copy.",
    "per turn": ("The component's cost divided by the Turns in the period. A flat cost per Turn "
                 "with a falling total means less usage, not savings."),
    "input": "The uncached input tokens of requests, at the list price.",
    "output": "The output tokens of requests, at the list price.",
    "cache write (5m)": _5M,
    "cache write (1h)": _1H,
    "cache read": "Context read back from the prompt cache, the cheapest kind of input.",
    "hit ratio": "Share of the prompt that was read from the cache instead of written or sent uncached.",
    "cold writes": ("A cache write that rebuilds context the cache already held before it expired. "
                    "Shown as its cost and the number of requests that made one."),
    "warm writes": "A cache write of context that is new to the session, as the conversation grows.",
    "5m writes": _5M,
    "1h writes": _1H,
}
