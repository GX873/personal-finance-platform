from fastapi import FastAPI

VERSION = "0.1.0"


def create_app() -> FastAPI:
    app = FastAPI(title="Personal Finance", version=VERSION)

    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok", "version": VERSION}

    return app


app = create_app()
