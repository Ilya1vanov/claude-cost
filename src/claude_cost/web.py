"""Web server: thin Flask adapter over the query service. Server-rendered pages."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

from flask import Flask, abort, render_template, request

from .query import PERIOD_MODES, QueryService

DASHBOARD_DAYS = 30


def create_app(service_factory: Callable[[], QueryService],
               now: Callable[[], datetime], top_n: int = 10) -> Flask:
    """`service_factory` is called per request (after refreshing ingest, in later tickets)."""
    app = Flask(__name__)

    @app.get("/")
    def dashboard():
        service = service_factory()
        mode = request.args.get("period", "last7")
        if mode not in PERIOD_MODES:
            abort(400, f"period must be one of {', '.join(PERIOD_MODES)}")
        # the form sends a hidden 0 before the checkbox's 1, so the last value wins
        include_today = request.args.getlist("include_today")[-1:] != ["0"]
        today = now().date()
        days = service.daily_spend(today - timedelta(days=DASHBOARD_DAYS - 1), today)
        return render_template(
            "dashboard.html",
            days=days,
            total=sum(d.cost for d in days),
            comparison=service.period_comparison(mode, now(), include_today, top_n),
            modes=PERIOD_MODES,
            include_today=include_today,
            unpriced=service.unpriced_models(),
            chart_data=[{"day": d.day.isoformat(), "cost": round(d.cost, 4)} for d in days],
        )

    return app
