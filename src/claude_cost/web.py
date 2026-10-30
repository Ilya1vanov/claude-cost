"""Web server: thin Flask adapter over the query service. Server-rendered pages."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

from flask import Flask, render_template

from .query import QueryService

DASHBOARD_DAYS = 30


def create_app(service_factory: Callable[[], QueryService],
               now: Callable[[], datetime]) -> Flask:
    """`service_factory` is called per request (after refreshing ingest, in later tickets)."""
    app = Flask(__name__)

    @app.get("/")
    def dashboard():
        service = service_factory()
        today = now().date()
        days = service.daily_spend(today - timedelta(days=DASHBOARD_DAYS - 1), today)
        return render_template(
            "dashboard.html",
            days=days,
            total=sum(d.cost for d in days),
            unpriced=service.unpriced_models(),
            chart_data=[{"day": d.day.isoformat(), "cost": round(d.cost, 4)} for d in days],
        )

    return app
