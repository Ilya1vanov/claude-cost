"""Web server: thin Flask adapter over the query service. Server-rendered pages."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

from flask import Flask, abort, redirect, render_template, request, url_for

from .query import PERIOD_MODES, QueryService

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

    return app
