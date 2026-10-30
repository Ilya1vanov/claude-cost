"""Web server: thin Flask adapter over the query service. Server-rendered pages."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Callable

from flask import Flask, abort, redirect, render_template, request, url_for

from .query import COMPARISON_MODES, PERIOD_MODES, QueryService

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

    @app.get("/session/<session_id>")
    @app.get("/session/<session_id>/agent/<agent_id>")
    def session_timeline(session_id: str, agent_id: str | None = None):
        timeline = service_factory().session_timeline(session_id, agent_id)
        if timeline is None:
            abort(404)
        chart = {"labels": [datetime.fromtimestamp(p.ts, timezone.utc).strftime("%H:%M:%S")
                            for p in timeline.points],
                 "context": [p.context_tokens for p in timeline.points],
                 "cost": [round(p.cost, 4) for p in timeline.points],
                 "cold": [i for i, p in enumerate(timeline.points) if p.cache_event == "cold"],
                 "idle": [i for i, p in enumerate(timeline.points) if p.idle]}
        return render_template("session.html", t=timeline, chart_data=chart,
                               when=lambda ts: datetime.fromtimestamp(ts, timezone.utc)
                               .strftime("%Y-%m-%d %H:%M:%S UTC"))

    return app
