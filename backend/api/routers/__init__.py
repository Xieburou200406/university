# -*- coding: utf-8 -*-
from fastapi import APIRouter

from api.routers import advice, health, metrics, pipeline, positions, signals


def all_routers() -> list[APIRouter]:
    return [health.router(), metrics.router(), signals.router(), advice.router(),
            positions.router(), pipeline.reviews_router(), pipeline.pipeline_router()]
