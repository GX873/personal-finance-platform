from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from finance_app.auth.service import (
    authenticate,
    csrf_token,
    current_user,
    require_csrf,
    session_fingerprint,
)
from finance_app.config import get_settings
from finance_app.db import get_db

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parents[1] / "templates")


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "csrf_token": csrf_token(request),
            "development": get_settings().environment == "development",
        },
    )


@router.post("/login", dependencies=[Depends(require_csrf)])
async def login(request: Request, db: Annotated[Session, Depends(get_db)]):
    form = await request.form()
    username, password = form.get("username"), form.get("password")
    user = (
        await run_in_threadpool(authenticate, db, username, password)
        if isinstance(username, str) and isinstance(password, str)
        else None
    )
    if user is None:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            status_code=401,
            context={
                "csrf_token": csrf_token(request),
                "error": "Invalid username or password.",
                "development": get_settings().environment == "development",
            },
        )
    request.session.clear()
    request.session["user_id"] = user.id
    request.session["auth_fingerprint"] = session_fingerprint(user)
    csrf_token(request)
    return RedirectResponse("/", status_code=303)


@router.get("/", response_class=HTMLResponse)
def home(request: Request, db: Annotated[Session, Depends(get_db)]):
    user = current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="home.html",
        context={"user": user, "csrf_token": csrf_token(request)},
    )


@router.post("/logout", dependencies=[Depends(require_csrf)])
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
