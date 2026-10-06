"""CarFinder v4 FastAPI service.

This API runs alongside the existing v3 Streamlit application during the
v4.0.1 foundation release.  It does not expose the v3 database.
"""
from __future__ import annotations

import os
from typing import Annotated

from fastapi import Cookie, Depends, FastAPI, HTTPException, Response, status
from pydantic import BaseModel, Field

from app.sources import available_makes
from app.users import authenticate, get_user
from app.v4_db import (
    create_session,
    delete_session,
    ensure_db,
    get_user_settings,
    list_targets,
    save_user_settings,
    session_username,
)

VERSION = "4.0.1"
SESSION_COOKIE = "carfinder_session"
COOKIE_SECURE = os.getenv("CARFINDER_COOKIE_SECURE", "0").strip().lower() in {"1", "true", "yes", "on"}

app = FastAPI(
    title="CarFinder API",
    version=VERSION,
    docs_url="/api/docs",
    redoc_url=None,
    openapi_url="/api/openapi.json",
)


class LoginRequest(BaseModel):
    username: str
    password: str


class SettingsRequest(BaseModel):
    postcode: str = ""
    radius_miles: int = Field(default=50, ge=1, le=500)


def current_username(
    carfinder_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> str:
    username = session_username(carfinder_session)
    if not username:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return username


@app.on_event("startup")
def startup() -> None:
    ensure_db()


@app.get("/api/v1/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "CarFinder", "version": VERSION}


@app.post("/api/v1/auth/login")
def login(payload: LoginRequest, response: Response) -> dict:
    user = authenticate(payload.username, payload.password)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")
    token = create_session(user["username"])
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        max_age=14 * 24 * 60 * 60,
        path="/",
    )
    return {"user": user}


@app.post("/api/v1/auth/logout")
def logout(
    response: Response,
    carfinder_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> dict[str, bool]:
    delete_session(carfinder_session)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/v1/auth/me")
def me(username: Annotated[str, Depends(current_username)]) -> dict:
    user = get_user(username)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown user")
    return {"user": user}


@app.get("/api/v1/reference/manufacturers")
def manufacturers(_: Annotated[str, Depends(current_username)]) -> dict:
    return {"manufacturers": sorted(available_makes())}


@app.get("/api/v1/settings")
def settings(username: Annotated[str, Depends(current_username)]) -> dict:
    return get_user_settings(username)


@app.put("/api/v1/settings")
def update_settings(
    payload: SettingsRequest,
    username: Annotated[str, Depends(current_username)],
) -> dict:
    return save_user_settings(username, payload.postcode, payload.radius_miles)


@app.get("/api/v1/targets")
def targets(username: Annotated[str, Depends(current_username)]) -> dict:
    return {"targets": list_targets(username)}
