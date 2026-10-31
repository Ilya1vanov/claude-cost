"""Web server: thin Flask adapter over the query service. Server-rendered pages."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Callable

from flask import Flask, Response, abort, g, jsonify, redirect, render_template, request, url_for
from markupsafe import Markup, escape

from . import explore
from .cache import COLD
from .explore import ExploreSpec
from .localtime import parse_since
from .findings import DRAIN, NOT_ADDITIVE_NOTE, OPPORTUNITY
from .query import COMPARISON_MODES, PERIOD_MODES, Period, QueryService
from .spotlights import SpotlightError
from .tooltips import TOOLTIPS

DASHBOARD_DAYS = 30
CHART_ROWS = 25  # Explore chart shows the top groups; the table and CSV show all
REQUEST_ROWS = 1000


def short_id(id: str, n: int = 8) -> str:
    """The leading characters of an id, as the `ui.sid` template macro shows it (minus the ellipsis)."""
    return id[:n]


def create_app(service_factory: Callable[[], QueryService],
               now: Callable[[], datetime],
               refresh: Callable[[], object] | None = None, top_n: int = 10) -> Flask:
    """`service_factory` is called per request. `refresh` runs ingest: on page load when the
    last ingest is older than the refresh_stale_seconds setting (60 s), and on POST /refresh (the Refresh button)."""
    app = Flask(__name__)
    app.jinja_env.globals["tooltip"] = TOOLTIPS.__getitem__  # a missing key raises KeyError
    make_service = service_factory

    def service_factory() -> QueryService:
        svc = make_service()
        g.setdefault("services", []).append(svc)
        return svc

    @app.teardown_appcontext
    def close_services(_exc):
        for svc in g.pop("services", []):
            svc.close()

    def helper_service() -> QueryService:
        """One service per request for the label helpers, so they share a single rollup."""
        if "helper_service" not in g:
            g.helper_service = service_factory()
        return g.helper_service

    @app.template_global()
    def session_label(session_id: str, link: bool = True, period: Period | None = None) -> Markup:
        """A Session by its label, the id on hover; linked to its Session page unless `link` is off.
        With `period`, the page also shows the Session's spend in that Period."""
        if "session_labels" not in g:
            g.session_labels = helper_service().session_labels()
        label, sid = escape(g.session_labels.get(session_id, session_id)), escape(session_id)
        if not link or session_id not in g.session_labels:  # unknown to the store: nothing to open
            return Markup(f'<span class="id" title="{sid}">{label}</span>')
        args = {"from": period.start.isoformat(), "to": period.end.isoformat()} if period else {}
        return Markup(f'<a href="{escape(url_for("session_timeline", session_id=session_id, **args))}" '
                      f'class="id" title="{sid}">{label}</a>')

    @app.template_global()
    def project_name(key: str) -> str:
        """A project by its display name, the key when it has none."""
        if "project_names" not in g:
            g.project_names = helper_service().project_names()
        return g.project_names.get(key, key)

    @app.post("/refresh")
    def force_refresh():
        if refresh:
            refresh()
        return redirect(url_for("dashboard"))

    @app.get("/")
    def dashboard():
        service = helper_service()
        mode = request.args.get("period", "last7")
        if mode not in PERIOD_MODES:
            abort(400, f"period must be one of {', '.join(PERIOD_MODES)}")
        # the form sends a hidden 0 before the checkbox's 1, so the last value wins
        include_today = request.args.getlist("include_today")[-1:] != ["0"]
        if refresh:
            last = service.last_ingest_epoch()
            if last is None or now().timestamp() - last > service.stale_after_seconds():
                refresh()
                g.pop("helper_service", None)
                g.pop("session_labels", None)
                g.pop("project_names", None)
                service = helper_service()
        today = service.today(now())
        days = service.daily_spend(today - timedelta(days=DASHBOARD_DAYS - 1), today)
        return render_template(
            "dashboard.html",
            days=days,
            total=sum(d.cost for d in days),
            comparison=service.period_comparison(mode, now(), include_today, top_n),
            cache=service.cache_card(mode, now(), include_today),
            modes=PERIOD_MODES,
            include_today=include_today,
            unpriced=service.unpriced_models(),
            spotlights=service.top_spotlights(mode, now(), include_today, top_n),
            top_findings=service.top_findings(mode, now(), include_today, top_n),
            chart_data=[{"day": d.day.isoformat(), "cost": round(d.cost, 4)} for d in days],
        )

    @app.get("/compact")
    def compact():
        return render_template("compact.html", a=service_factory().compact_thresholds())

    @app.get("/findings")
    def findings():
        service = service_factory()
        since = _since_arg()
        return render_template("findings.html", findings=service.findings(since), since=since,
                               note=NOT_ADDITIVE_NOTE, drain=DRAIN, opportunity=OPPORTUNITY)

    @app.get("/findings/<finding_id>")
    def finding_detail(finding_id: str):
        since = _since_arg()
        try:
            f = service_factory().finding(finding_id, since)
        except KeyError:
            abort(404)
        return render_template("finding.html", f=f, since=since, note=NOT_ADDITIVE_NOTE)

    def _since_arg():
        try:
            text = request.args.get("since")
            return parse_since(text, service_factory().today(now())) if text else None
        except ValueError:
            abort(400, "since must be a date like 2026-10-01 or days like 7d")

    @app.get("/compare")
    def compare():
        service = service_factory()
        mode = request.args.get("mode", "week")
        if mode not in COMPARISON_MODES:
            abort(400, f"mode must be one of {', '.join(COMPARISON_MODES)}")
        try:
            starts = [date.fromisoformat(v) for v in request.args.getlist("p")]
            baseline = (date.fromisoformat(request.args["baseline"])
                        if request.args.get("baseline") else None)
            result = service.comparison(mode, starts, now(), baseline) if len(set(starts)) >= 2 else None
        except ValueError as e:
            abort(400, str(e))
        chart = None
        if result:
            chart = {"labels": list(range(1, result.day_count + 1)),
                     "series": [{"label": f"{e.period.start}", "data": [round(c, 4) for c in e.daily]}
                                for e in result.entries]}
        return render_template(
            "compare.html", mode=mode, modes=COMPARISON_MODES, result=result, chart_data=chart,
            available=service.available_periods(mode, now()), selected=set(starts),
            baseline=result.baseline if result else None)

    @app.get("/spotlights")
    def spotlight_list():
        service = service_factory()
        return render_template("spotlights.html", spotlights=service.spotlights(),
                               auto_enabled=service.auto_spotlights_enabled())

    @app.post("/spotlights")
    def spotlight_create():
        name = request.form.get("name", "")
        try:
            service_factory().create_spotlight(
                name, request.form.getlist("session"), request.form.getlist("include"))
        except SpotlightError as e:
            abort(400, str(e))
        return redirect(url_for("spotlight_page", name=name.strip()))

    @app.get("/spotlights/compare")
    def spotlight_compare():
        service = service_factory()
        names = request.args.getlist("s")
        try:
            details = service.spotlight_comparison(names) if len(set(names)) >= 2 else None
        except KeyError as e:
            abort(404, f"no Spotlight named {e.args[0]!r}")
        chart = None
        if details:
            n = max(len(d.days) for d in details)
            chart = {"labels": list(range(1, n + 1)),
                     "series": [{"label": d.summary.name, "data": [round(x.cost, 4) for x in d.days]}
                                for d in details]}
        return render_template("spotlight_compare.html", details=details, chart_data=chart,
                               available=service.spotlights(), selected=set(names))

    @app.get("/spotlights/<path:name>")
    def spotlight_page(name):
        service = service_factory()
        try:
            detail = service.spotlight(name)
        except KeyError:
            abort(404, f"no Spotlight named {name!r}")
        chart = {"labels": [d.day.isoformat() for d in detail.days],
                 "series": [{"label": c, "data": [round(d.components[c], 4) for d in detail.days]}
                            for c in detail.components]}
        return render_template("spotlight.html", d=detail, chart_data=chart,
                               includable=service.includable_spotlights(name))

    @app.get("/api/session-search")
    def session_search():
        found = service_factory().search_sessions(
            request.args.get("q", ""), request.args.get("name", ""), request.args.getlist("exclude"))
        return jsonify(results=[
            {"id": c.session_id, "short_id": short_id(c.session_id), "label": c.label,
             "cost": round(c.cost, 2), "kind": c.kind, "suggested": c.suggested} for c in found])

    def _edit(name, action):
        service = service_factory()
        try:
            action(service)
        except SpotlightError as e:
            abort(400, str(e))
        return redirect(url_for("spotlight_page", name=name))

    @app.post("/spotlights/<path:name>/add")
    def spotlight_add(name):
        return _edit(name, lambda s: s.add_sessions(name, request.form.getlist("session")))

    @app.post("/spotlights/<path:name>/remove")
    def spotlight_remove(name):
        return _edit(name, lambda s: s.remove_session(name, request.form.get("session", "")))

    @app.post("/spotlights/<path:name>/include")
    def spotlight_include(name):
        def action(service):
            for other in request.form.getlist("spotlight"):
                service.include_spotlight(name, other)
        return _edit(name, action)

    @app.post("/spotlights/<path:name>/exclude")
    def spotlight_exclude(name):
        def action(service):
            for other in request.form.getlist("spotlight"):
                service.exclude_spotlight(name, other)
        return _edit(name, action)

    @app.post("/spotlights/<path:name>/delete")
    def spotlight_delete(name):
        try:
            service_factory().delete_spotlight(name)
        except SpotlightError as e:
            abort(400, str(e))
        return redirect(url_for("spotlight_list"))

    def explore_service() -> QueryService:
        service = service_factory()
        if refresh:
            last = service.last_ingest_epoch()
            if last is None or now().timestamp() - last > service.stale_after_seconds():
                refresh()
                service = service_factory()
        return service

    def explore_spec() -> ExploreSpec:
        try:
            return ExploreSpec.from_query(request.args.items(multi=True))
        except ValueError as e:
            abort(400, str(e))

    @app.template_global()
    def explore_url(endpoint: str, spec: ExploreSpec, **extra) -> str:
        """URL of an Explore page for `spec`: filters live in the query string."""
        params: dict[str, list[str]] = {}
        for key, value in spec.to_query():
            params.setdefault(key, []).append(value)
        return url_for(endpoint, **{**params, **extra})

    @app.get("/explore")
    def explore_page():
        spec = explore_spec()
        service = explore_service()
        result = service.explore(spec)
        return render_template(
            "explore.html", spec=spec, result=result, dims=explore.DIMENSIONS,
            options=service.explore_options(), fmt_cost=explore.fmt_cost,
            fmt_share=explore.fmt_share,
            chart_data={"label": result.label,
                        "labels": [project_name(r.value) if result.group == "project" else r.value
                                   for r in result.rows[:CHART_ROWS]],
                        "costs": [round(r.cost, 4) for r in result.rows[:CHART_ROWS]]})

    @app.get("/explore.csv")
    def explore_csv():
        spec = explore_spec()
        body = explore.to_csv(explore_service().explore(spec))
        return Response(body, mimetype="text/csv", headers={
            "Content-Disposition": f'attachment; filename="claude-cost-{spec.group}.csv"'})

    @app.get("/explore/requests")
    def explore_requests():
        spec = explore_spec()
        service = explore_service()
        reqs = service.explore_requests(spec)
        return render_template(
            "explore_requests.html", spec=spec, dims=explore.DIMENSIONS, total=len(reqs),
            requests=reqs[:REQUEST_ROWS], cost=sum(r.cost for r in reqs))

    @app.get("/session/<session_id>")
    @app.get("/session/<session_id>/agent/<agent_id>")
    def session_timeline(session_id: str, agent_id: str | None = None):
        service = service_factory()
        timeline = service.session_timeline(session_id, agent_id)
        if timeline is None:
            abort(404)
        period = in_period = None
        if "from" in request.args or "to" in request.args:
            try:
                period = Period(date.fromisoformat(request.args["from"]),
                                date.fromisoformat(request.args["to"]))
            except (KeyError, ValueError):
                abort(400, "from and to must both be dates like 2026-10-01")
            if agent_id is None:
                in_period = service.session_spend(session_id, period)
        chart = {"labels": [service.local_time(p.ts).strftime("%Y-%m-%d %H:%M:%S")
                            for p in timeline.points],
                 "context": [p.context_tokens for p in timeline.points],
                 "cost": [round(p.cost, 4) for p in timeline.points],
                 "cold": [i for i, p in enumerate(timeline.points) if p.cache_event == COLD],
                 "idle": [i for i, p in enumerate(timeline.points) if p.idle],
                 "threshold": [p.threshold for p in timeline.points],
                 "over": [i for i, p in enumerate(timeline.points)
                          if p.threshold is not None and p.context_tokens > p.threshold],
                 "compactions": [{"index": c.before_index, "trigger": c.trigger,
                                  "pre": c.pre_tokens, "post": c.post_tokens, "late": c.late}
                                 for c in timeline.compactions],
                 "late": [{"index": c.before_index, "pre": c.pre_tokens}
                          for c in timeline.compactions if c.late]}
        return render_template("session.html", t=timeline, chart_data=chart,
                               period=period, in_period=in_period,
                               when=lambda ts: service.local_time(ts).strftime("%Y-%m-%d %H:%M:%S %Z"))

    return app
