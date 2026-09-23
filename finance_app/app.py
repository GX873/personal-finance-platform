from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from finance_app.auth.routes import router as auth_router
from finance_app.config import get_settings

VERSION = "0.1.0"


def create_app() -> FastAPI:
    app = FastAPI(title="Personal Finance", version=VERSION)
    settings = get_settings()
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        max_age=8 * 60 * 60,
        same_site="lax",
        https_only=settings.session_https_only,
    )
    app.include_router(auth_router)

    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok", "version": VERSION}

    return app


app = create_app()
