# -*- coding: utf-8 -*-
from fastapi import APIRouter

from api.routers import advice, forecast, health, metrics, pipeline, positions, riskbudget, signals


def all_routers() -> list[APIRouter]:
    return [health.router(), metrics.router(), signals.router(), advice.router(),
            positions.router(), pipeline.reviews_router(), pipeline.pipeline_router(),
            forecast.router(), riskbudget.router()]
