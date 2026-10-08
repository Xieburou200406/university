# -*- coding: utf-8 -*-
from fastapi import APIRouter

from api.schemas import HealthOut
from db.models import Base


def router() -> APIRouter:
    r = APIRouter()

    @r.get("/health", response_model=HealthOut)
    def health():
        return HealthOut(status="ok", db="sqlite (data/vol.db, WAL)",
                         tables=len(Base.metadata.tables), advice_only=True)

    return r
