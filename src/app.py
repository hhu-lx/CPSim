from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from src.api.frontend_routes import FrontendSession, router as frontend_router
from src.api.task_routes import router


def create_app(engine=None, *, synthetic_seed: int = 42, engine_factory=None) -> FastAPI:
    app = FastAPI(title="Computing-Power Synergy Platform")

    @app.middleware("http")
    async def disable_dashboard_cache(request, call_next):
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.state.engine = engine
    app.state.synthetic_seed = synthetic_seed
    app.state.frontend_session = FrontendSession(engine=engine, engine_factory=engine_factory)
    app.include_router(router)
    app.include_router(frontend_router)
    web_dir = Path(__file__).resolve().parent / "web"
    if web_dir.exists():
        app.mount("/static", StaticFiles(directory=web_dir), name="static")

    @app.get("/", include_in_schema=False)
    def dashboard():
        return FileResponse(web_dir / "index.html")

    return app


app = create_app()
