"""Explore: slice spend by any dimension (filters) and group it by one (group-by).

A **dimension** (`Dimension`, registered in `DIMENSIONS`) says which value(s) each Request has
on some axis. To add one (Drain/Opportunity, ...): subclass `Dimension` with a `key`, `label`
and either an `sql` ("SELECT message_id, value FROM ...") or a `load(conn)` returning
`{message_id: [values]}`, then call `register(MyDimension())`. It is then a filter and a
group-by with no further wiring (web form, URL and CSV are generated from the registry).

Semantics:
- Filters on different dimensions AND together; several values of one dimension OR.
- A request may have several values on a dimension (tool, skill): it then counts in each
  group (`overlapping`), while the total counts it once.
- Cost component and Cache event split a request's cost across their values; filtering
  by them also restricts the cost counted to the selected parts.
"""
from __future__ import annotations

import csv
import functools
import io
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Callable

from .findings import all_findings
from .sql import COMPONENTS, SESSION_ID_SQL, SESSION_JOIN

NONE = "(none)"
COMPONENT_KEYS = COMPONENTS
_KEYS = tuple(k for _, k in COMPONENT_KEYS)
DEFAULT_GROUP = "date"
ROW_CAP = 200  # rows a page shows; the rest is reached by narrowing the filters
SESSION_SORTS = ("session", "project", "last_activity", "cost", "requests", "share")
BREAKDOWN_SORTS = ("value", "cost", "requests", "share")


def _resolve_sort(sort: str | None, allowed: tuple[str, ...], default: str) -> tuple[str, str]:
    """(key, effective sort): `sort` is a key, with `-` in front for descending."""
    sort = sort or default
    key = sort.lstrip("-") if sort.startswith("-") else sort
    if key not in allowed:
        raise ValueError(f"sort must be one of {', '.join(allowed)} (with - in front for descending)")
    return key, sort


def _sorted(rows: list, key_of: Callable, tiebreak: Callable, descending: bool) -> list:
    rows.sort(key=tiebreak)  # stable: ties keep a fixed order whichever way the key goes
    rows.sort(key=key_of, reverse=descending)
    return rows


@dataclass
class Item:
    """One Request in the slice; `components` are its costs by Cost component column."""
    message_id: str
    ts_epoch: float
    day: date
    components: dict[str, float]

    @property
    def cost(self) -> float:
        return sum(self.components.values())


# (value, cost-component columns it covers, None = the whole request)
Part = tuple[str, "tuple[str, ...] | None"]


class Dimension:
    key = ""
    label = ""
    sql: str | None = None  # SELECT message_id, value ... (several rows per message id = several values)
    missing = NONE  # value of a request the loader has nothing for
    overlapping = False  # a request can have several values
    chronological = False  # order groups by value, not by cost

    def load(self, conn: sqlite3.Connection) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        if self.sql:
            for mid, value in conn.execute(self.sql):
                out.setdefault(mid, []).append(value)
        return out

    def load_with(self, conn: sqlite3.Connection,
                  findings: Callable[[], list] | None) -> dict[str, list[str]]:
        """`load`, for dimensions that also need the findings (`findings()` -> list[Finding])."""
        return self.load(conn)

    def parts(self, item: Item, data: dict[str, list[str]]) -> list[Part]:
        return [(v, None) for v in data.get(item.message_id) or [self.missing]]

    def matches(self, value: str, selected: tuple[str, ...]) -> bool:
        return value in selected

    def restrict(self, item: Item, data: dict[str, list[str]],
                 selected: tuple[str, ...]) -> Item | None:
        """The item as the filter leaves it (cost possibly narrowed), or None when excluded."""
        hit = [keys for v, keys in self.parts(item, data) if self.matches(v, selected)]
        if not hit:
            return None
        if any(k is None for k in hit):
            return item
        kept = {c for keys in hit for c in keys}  # type: ignore[union-attr]
        comps = {c: (v if c in kept else 0.0) for c, v in item.components.items()}
        return Item(item.message_id, item.ts_epoch, item.day, comps) if sum(comps.values()) else None

    def cost_of(self, item: Item, keys: tuple[str, ...] | None) -> float:
        return item.cost if keys is None else sum(item.components[k] for k in keys)


DATE_PRESETS = {"last-7": "Last 7 days", "last-30": "Last 30 days", "last-90": "Last 90 days",
                "this-week": "This week", "last-week": "Last week",
                "this-month": "This month", "last-month": "Last month"}


def resolve_date(value: str, today: date) -> str:
    """A Date filter value as a day (YYYY-MM-DD) or an inclusive range (A..B): a preset token
    (`last-7`, `this-week`, ...) is resolved against `today` (weeks start on Monday)."""
    if value not in DATE_PRESETS:
        return value
    if value.startswith("last-") and value[5:].isdigit():
        lo, hi = today - timedelta(days=int(value[5:]) - 1), today
    elif value == "this-week":
        lo, hi = today - timedelta(days=today.weekday()), today
    elif value == "last-week":
        hi = today - timedelta(days=today.weekday() + 1)
        lo = hi - timedelta(days=6)
    elif value == "this-month":
        lo, hi = today.replace(day=1), today
    else:  # last-month
        hi = today.replace(day=1) - timedelta(days=1)
        lo = hi.replace(day=1)
    return f"{lo.isoformat()}..{hi.isoformat()}"


class DateDimension(Dimension):
    key, label, chronological = "date", "Date", True

    def parts(self, item, data):
        return [(item.day.isoformat(), None)]

    def matches(self, value, selected):
        """A selected entry is a day (YYYY-MM-DD) or an inclusive range (A..B)."""
        for sel in selected:
            lo, _, hi = sel.partition("..")
            if lo <= value <= (hi or lo):
                return True
        return False


class SplitDimension(Dimension):
    """Values that divide a request's cost between them (Cost component, Cache event)."""

    def parts(self, item, data):
        return [(v, keys) for v, keys in self.split(item, data)
                if sum(item.components[k] for k in keys) > 0]

    def split(self, item: Item, data: dict[str, list[str]]) -> list[tuple[str, tuple[str, ...]]]:
        raise NotImplementedError


class ComponentDimension(SplitDimension):
    key, label = "component", "Cost component"

    def split(self, item, data):
        return [(name, (col,)) for name, col in COMPONENT_KEYS]


class CacheEventDimension(SplitDimension):
    """Cold/Warm write (from `request_cache`), cache read, and everything else as uncached."""
    key, label = "cache_event", "Cache event"
    sql = "SELECT message_id, event FROM request_cache"

    def split(self, item, data):
        event = (data.get(item.message_id) or ["none"])[0]
        out = [("cache read", ("cost_cache_read",)), ("uncached", ("cost_input", "cost_output"))]
        if event in ("cold", "warm"):
            out.append((f"{event} write", ("cost_cache_write_5m", "cost_cache_write_1h")))
        return out


class FindingDimension(Dimension):
    """Requests a Drain or Opportunity covers; the value is the finding's title."""
    overlapping = True

    def __init__(self, key: str, label: str, kind: str):
        self.key, self.label, self.kind = key, label, kind

    def titles(self) -> list[str]:
        return [d.title for d in all_findings() if d.kind == self.kind]

    def load_with(self, conn, findings):
        out: dict[str, list[str]] = {}
        for f in findings() if findings else []:
            if f.kind == self.kind:
                for r in f.requests:
                    out.setdefault(r.message_id, []).append(f.title)
        return out


def _scalar(key: str, label: str, sql: str, **attrs) -> Dimension:
    return type(f"{key}_dimension", (Dimension,), {"key": key, "label": label, "sql": sql, **attrs})()


# Subagent type: the transcript's agent type, "(main)" for requests made by the Session itself
_SUBAGENT_SQL = (
    "SELECT a.message_id, COALESCE(s.agent_type, '(unknown)') FROM request_surface a "
    "JOIN requests r ON r.message_id = a.message_id "
    "LEFT JOIN (SELECT f.file_stem AS stem, sf.agent_id AS agent_id, MAX(sf.agent_type) AS agent_type "
    "FROM subagent_files sf JOIN ingest_files f ON f.path = sf.path "
    "GROUP BY f.file_stem, sf.agent_id) s ON s.stem = r.session_id AND s.agent_id = a.agent_id "
    "WHERE a.agent_id IS NOT NULL")

DIMENSIONS: dict[str, Dimension] = {}


def register(dim: Dimension) -> Dimension:
    DIMENSIONS[dim.key] = dim
    return dim


for _d in (
    DateDimension(),
    _scalar("data_root", "Data root", "SELECT message_id, data_root FROM requests"),
    _scalar("surface", "Surface", "SELECT message_id, surface FROM request_surface"),
    _scalar("project", "Project", "SELECT message_id, project FROM requests"),
    _scalar("git_branch", "Git branch",
            "SELECT message_id, git_branch FROM request_dims WHERE git_branch != ''"),
    _scalar("session", "Session",
            f"SELECT r.message_id, {SESSION_ID_SQL} FROM requests r {SESSION_JOIN}"),
    _scalar("subagent_type", "Subagent type", _SUBAGENT_SQL, missing="(main)"),
    _scalar("model", "Model", "SELECT message_id, model FROM requests"),
    _scalar("effort", "Effort", "SELECT message_id, effort FROM request_dims WHERE effort != ''"),
    _scalar("speed", "Speed", "SELECT message_id, speed FROM request_attrs"),
    ComponentDimension(),
    CacheEventDimension(),
    FindingDimension("drain", "Drain", "drain"),
    FindingDimension("opportunity", "Opportunity", "opportunity"),
    _scalar("tool", "Tool", "SELECT message_id, tool FROM request_tools", overlapping=True),
    _scalar("skill", "Skill", "SELECT message_id, skill FROM request_skills", overlapping=True),
):
    register(_d)


def breakdowns() -> dict[str, Dimension]:
    """The dimensions a Breakdown can split by: all but Session, which the Sessions page lists."""
    return {k: d for k, d in DIMENSIONS.items() if k != "session"}


@dataclass(frozen=True)
class ExploreSpec:
    """A view: the group-by and the filters. `from_query`/`to_query` are the URL encoding
    (`?group=model&project=a&project=b`: one query parameter per dimension key)."""
    group: str = DEFAULT_GROUP
    filters: dict[str, tuple[str, ...]] = field(default_factory=dict)
    sort: str | None = None  # a column key, `-` in front for descending; None = the page default

    @classmethod
    def from_query(cls, pairs) -> "ExploreSpec":
        group, filters, sort = DEFAULT_GROUP, {}, None
        for key, value in pairs:
            if key == "group":
                if value not in breakdowns():
                    raise ValueError(f"group must be one of {', '.join(breakdowns())}")
                group = value
            elif key == "sort":
                sort = value or None  # validated against the page's columns when it runs
            elif key in DIMENSIONS and value != "":
                if value not in filters.setdefault(key, ()):
                    filters[key] = filters[key] + (value,)
        return cls(group, filters, sort)

    def to_query(self) -> list[tuple[str, str]]:
        return ([("group", self.group)] + [(k, v) for k in DIMENSIONS for v in self.filters.get(k, ())]
                + ([("sort", self.sort)] if self.sort else []))

    def resolved(self, today: date) -> "ExploreSpec":
        """The spec with Date presets turned into ranges, for running; the original keeps the tokens."""
        if "date" not in self.filters:
            return self
        dates = tuple(resolve_date(v, today) for v in self.filters["date"])
        return replace(self, filters={**self.filters, "date": dates})

    def with_group(self, group: str) -> "ExploreSpec":
        return ExploreSpec(group, self.filters)  # the sort keys differ per group: back to default

    def drill(self, dim: str, value: str) -> "ExploreSpec":
        """The filters plus `dim = value`: the slice behind one Breakdown row, listed on Sessions."""
        return ExploreSpec(DEFAULT_GROUP, {**self.filters, dim: (value,)})

    def without_dim(self, dim: str) -> "ExploreSpec":
        return ExploreSpec(self.group, {k: v for k, v in self.filters.items() if k != dim})

    def without(self, dim: str, value: str) -> "ExploreSpec":
        rest = tuple(v for v in self.filters.get(dim, ()) if v != value)
        filters = {k: v for k, v in self.filters.items() if k != dim}
        if rest:
            filters[dim] = rest
        return ExploreSpec(self.group, filters, self.sort)


@dataclass(frozen=True)
class ExploreRow:
    value: str
    cost: float
    requests: int
    share: float  # of the slice's total cost, 0..1


@dataclass(frozen=True)
class ExploreResult:
    group: str
    label: str
    rows: tuple[ExploreRow, ...]
    total_cost: float
    total_requests: int
    overlapping: bool  # a request can be in several rows, so rows may add up to more than the total
    sort: str = ""  # the effective sort, e.g. "-cost"

    @property
    def shown(self) -> tuple[ExploreRow, ...]:
        return self.rows[:ROW_CAP]


@dataclass(frozen=True)
class ExploreRequest:
    message_id: str
    ts_epoch: float
    day: date  # local day
    session_id: str
    project: str
    model: str
    cost: float


@dataclass(frozen=True)
class SessionRow:
    """One logical Session of the Sessions page."""
    session_id: str
    name: str
    project: str
    last_activity: datetime  # latest request, local time
    cost: float
    requests: int
    share: float  # of the slice's total cost, 0..1


@dataclass(frozen=True)
class SessionsResult:
    rows: tuple[SessionRow, ...]  # in the requested sort
    total_cost: float
    total_requests: int
    sort: str = ""  # the effective sort, e.g. "-last_activity"

    @property
    def shown(self) -> tuple[SessionRow, ...]:
        return self.rows[:ROW_CAP]


def _items(conn: sqlite3.Connection, local_day: Callable[[float], date]) -> list[Item]:
    cols = ", ".join(_KEYS)
    return [Item(r["message_id"], r["ts_epoch"], local_day(r["ts_epoch"]),
                 {k: r[k] for k in _KEYS})
            for r in conn.execute(f"SELECT message_id, ts_epoch, {cols} FROM requests "
                                  "ORDER BY ts_epoch, message_id")]


def _slice(conn, local_day, spec: ExploreSpec, extra: tuple[str, ...] = (), findings=None):
    """Items left by the filters, and the loaded data of every dimension used."""
    used = {spec.group, *spec.filters, *extra}
    if findings is not None:  # several finding dimensions share one pipeline run
        findings = functools.cache(findings)
    data = {k: DIMENSIONS[k].load_with(conn, findings) for k in used}
    items = []
    for item in _items(conn, local_day):
        for key, selected in spec.filters.items():
            item = DIMENSIONS[key].restrict(item, data[key], selected)  # type: ignore[assignment]
            if item is None:
                break
        else:
            items.append(item)
    return items, data


def run(conn, local_day, spec: ExploreSpec, findings=None) -> ExploreResult:
    dim = DIMENSIONS[spec.group]
    key, sort = _resolve_sort(spec.sort, BREAKDOWN_SORTS, "-value" if dim.chronological else "-cost")
    items, data = _slice(conn, local_day, spec, findings=findings)
    groups: dict[str, list] = {}
    for item in items:
        for value, keys in dim.parts(item, data[spec.group]):
            g = groups.setdefault(value, [0.0, 0])
            g[0] += dim.cost_of(item, keys)
            g[1] += 1
    total = sum(i.cost for i in items)
    rows = [ExploreRow(v, c, n, c / total if total else 0.0) for v, (c, n) in groups.items()]
    _sorted(rows, lambda r: getattr(r, key), lambda r: r.value, sort.startswith("-"))
    return ExploreResult(spec.group, dim.label, tuple(rows), total, len(items), dim.overlapping, sort)


def requests(conn, local_day, spec: ExploreSpec, findings=None) -> list[ExploreRequest]:
    items, data = _slice(conn, local_day, spec, extra=("session", "project", "model"),
                         findings=findings)
    one = lambda key, item: (data[key].get(item.message_id) or [NONE])[0]  # noqa: E731
    return [ExploreRequest(i.message_id, i.ts_epoch, i.day, one("session", i), one("project", i),
                           one("model", i), i.cost) for i in items]


@dataclass(frozen=True)
class FilterOption:
    """One value a filter can take: `count` is the Sessions that have it in the narrowed slice."""
    value: str
    label: str
    count: int


def filter_options(conn, local_day, spec: ExploreSpec, key: str, q: str = "",
                   labels: dict[str, str] | None = None, findings=None,
                   limit: int = 100) -> list[FilterOption]:
    """The values of dimension `key` for the filter popover: the top `limit` by Session count
    among the requests the *other* filters keep, narrowed by `q` (case-insensitive, on the
    readable label or the value). Values `spec` already selects for `key` are always listed,
    at 0 when the other filters leave them nothing."""
    labels = labels or {}
    selected = spec.filters.get(key, ())
    narrowed = ExploreSpec(DEFAULT_GROUP, {k: v for k, v in spec.filters.items() if k != key})
    items, data = _slice(conn, local_day, narrowed, extra=(key, "session"), findings=findings)
    sessions: dict[str, set[str]] = {}
    for item in items:
        sid = (data["session"].get(item.message_id) or [NONE])[0]
        for value, _ in DIMENSIONS[key].parts(item, data[key]):
            sessions.setdefault(value, set()).add(sid)
    needle = q.strip().lower()
    options = [FilterOption(v, labels.get(v, v), len(s)) for v, s in sessions.items()
               if not needle or needle in labels.get(v, v).lower() or needle in v.lower()]
    options.sort(key=lambda o: (-o.count, o.label, o.value))
    options = options[:limit]
    have = {o.value for o in options}
    options += [FilterOption(v, labels.get(v, v), 0) for v in selected if v not in have]
    return options


def chip_summary(names: list[str]) -> str:
    """A chip's value text: `A, B`, or `A +N` once it has more than 2 values or 40 characters
    (a lone value is never cut)."""
    joined = ", ".join(names)
    return joined if len(names) == 1 or (len(names) == 2 and len(joined) <= 40) else f"{names[0]} +{len(names) - 1}"


@dataclass(frozen=True)
class Chip:
    key: str
    label: str  # the dimension's label
    text: str  # chip_summary of the readable values
    full: str  # `Label: A, B, C`, for the hover title


def chips(spec: ExploreSpec, names: dict[str, dict[str, str]]) -> list[Chip]:
    """One chip per applied filter, in registry order; `names[key]` maps values to readable names."""
    out = []
    for key, dim in DIMENSIONS.items():
        values = spec.filters.get(key)
        if values:
            shown = [names.get(key, {}).get(v, v) for v in values]
            if key == "date":
                shown = [DATE_PRESETS.get(v) or v.replace("..", " – ") for v in values]
            out.append(Chip(key, dim.label, chip_summary(shown), f"{dim.label}: {', '.join(shown)}"))
    return out


def fmt_cost(cost: float) -> str:
    return f"{cost:.2f}"


def fmt_share(share: float) -> str:
    return f"{share * 100:.1f}"


def to_csv(result: ExploreResult) -> str:
    """The Explore table as CSV: same rows, same rounding (cost in $, share in %)."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow([result.label, "Cost", "Requests", "Share %"])
    for r in result.rows:
        w.writerow([r.value, fmt_cost(r.cost), r.requests, fmt_share(r.share)])
    return buf.getvalue()


def sessions(conn, local_day, local_time: Callable[[float], datetime], labels: dict[str, str],
             spec: ExploreSpec, findings=None) -> SessionsResult:
    """One row per logical Session over the requests the filters keep, newest activity first by default."""
    key, sort = _resolve_sort(spec.sort, SESSION_SORTS, "-last_activity")
    reqs = requests(conn, local_day, spec, findings)
    groups: dict[str, list] = {}
    for r in reqs:
        g = groups.setdefault(r.session_id, [r.project, r.ts_epoch, 0.0, 0])
        g[1] = max(g[1], r.ts_epoch)
        g[2] += r.cost
        g[3] += 1
    total = sum(r.cost for r in reqs)
    rows = [SessionRow(sid, labels.get(sid, sid), project, local_time(last), cost, n,
                       cost / total if total else 0.0)
            for sid, (project, last, cost, n) in groups.items()]
    _sorted(rows, lambda r: r.name if key == "session" else getattr(r, key),
            lambda r: r.session_id, sort.startswith("-"))
    return SessionsResult(tuple(rows), total, len(reqs), sort)


def sessions_to_csv(result: SessionsResult) -> str:
    """The Sessions table as CSV: same rows, same rounding (cost in $, share in %)."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["Session", "Session id", "Project", "Last activity", "Cost", "Requests", "Share %"])
    for r in result.rows:
        w.writerow([r.name, r.session_id, r.project, f"{r.last_activity:%Y-%m-%d %H:%M}",
                    fmt_cost(r.cost), r.requests, fmt_share(r.share)])
    return buf.getvalue()
