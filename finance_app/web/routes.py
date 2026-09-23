from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from finance_app.auth.routes import templates
from finance_app.auth.service import csrf_token, current_user
from finance_app.config import get_settings
from finance_app.db import get_db
from finance_app.web.viewmodels import dashboard, money

router = APIRouter()
templates.env.filters["money"] = money


@router.get("/", response_class=HTMLResponse)
@router.get("/holdings", response_class=HTMLResponse)
@router.get("/alerts", response_class=HTMLResponse)
def page(request: Request, db: Annotated[Session, Depends(get_db)]):
    user = current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    section = {"/": "dashboard", "/holdings": "holdings", "/alerts": "alerts"}[
        request.url.path
    ]
    return templates.TemplateResponse(
        request=request,
        name=f"{section}.html",
        context={
            "user": user,
            "csrf_token": csrf_token(request),
            "section": section,
            "demo": False,
            "vm": dashboard(db),
        },
    )


@router.get("/preview", response_class=HTMLResponse, include_in_schema=False)
def preview(request: Request):
    settings = get_settings()
    if not settings.demo_mode or settings.environment != "development":
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={"section": "dashboard", "demo": True, "vm": dashboard()},
    )
