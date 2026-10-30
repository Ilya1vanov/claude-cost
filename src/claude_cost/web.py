"""Web server: thin Flask adapter over the query service. Server-rendered pages."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Callable

from flask import Flask, abort, redirect, render_template, request, url_for

from .query import COMPARISON_MODES, PERIOD_MODES, QueryService
from .spotlights import SpotlightError

DASHBOARD_DAYS = 30
STALE_AFTER_SECONDS = 60


def create_app(service_factory: Callable[[], QueryService],
               now: Callable[[], datetime],
               refresh: Callable[[], object] | None = None, top_n: int = 10) -> Flask:
    """`service_factory` is called per request. `refresh` runs ingest: on page load when the
    last ingest is older than 60 s, and on POST /refresh (the Refresh button)."""
    app = Flask(__name__)

    @app.post("/refresh")
    def force_refresh():
        if refresh:
            refresh()
        return redirect(url_for("dashboard"))

    @app.get("/")
    def dashboard():
        service = service_factory()
        mode = request.args.get("period", "last7")
        if mode not in PERIOD_MODES:
            abort(400, f"period must be one of {', '.join(PERIOD_MODES)}")
        # the form sends a hidden 0 before the checkbox's 1, so the last value wins
        include_today = request.args.getlist("include_today")[-1:] != ["0"]
        if refresh:
            last = service.last_ingest_epoch()
            if last is None or now().timestamp() - last > STALE_AFTER_SECONDS:
                refresh()
                service = service_factory()
        today = now().date()
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
            chart_data=[{"day": d.day.isoformat(), "cost": round(d.cost, 4)} for d in days],
        )

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
        return render_template("spotlights.html", spotlights=service.spotlights())

    @app.post("/spotlights")
    def spotlight_create():
        name = request.form.get("name", "")
        try:
            service_factory().save_spotlight(name, request.form.get("rule", ""))
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
        return render_template(
            "spotlight.html", d=detail, chart_data=chart,
            choices=[c for c in service.session_choices() if c[0] not in detail.pins])

    def _edit(name, action):
        service = service_factory()
        try:
            action(service)
        except SpotlightError as e:
            abort(400, str(e))
        return redirect(url_for("spotlight_page", name=name))

    @app.post("/spotlights/<path:name>/rule")
    def spotlight_rule(name):
        return _edit(name, lambda s: s.save_spotlight(name, request.form.get("rule", "")))

    @app.post("/spotlights/<path:name>/pin")
    def spotlight_pin(name):
        return _edit(name, lambda s: s.pin_session(name, request.form.get("session", "")))

    @app.post("/spotlights/<path:name>/unpin")
    def spotlight_unpin(name):
        return _edit(name, lambda s: s.unpin_session(name, request.form.get("session", "")))

    @app.post("/spotlights/<path:name>/delete")
    def spotlight_delete(name):
        service_factory().delete_spotlight(name)
        return redirect(url_for("spotlight_list"))

    return app
