import os
import time
import asyncio
import logging
import traceback
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from server.db import pool
from server import cache as cache_mod
from server import embeddings as emb_mod
from server import genie as genie_mod


logging.basicConfig(level=logging.INFO)
log = logging.getLogger("genie-cache")

DEFAULT_SPACE_ID = os.environ["GENIE_SPACE_ID"]
CLEANUP_INTERVAL_SECONDS = int(os.environ.get("CACHE_CLEANUP_INTERVAL_SECONDS", "0"))


async def _cleanup_loop():
    """Periodically delete expired cache rows. Runs until cancelled."""
    log.info(
        "cleanup loop started: interval=%ds ttl=%ds",
        CLEANUP_INTERVAL_SECONDS,
        cache_mod.CACHE_TTL_SECONDS,
    )
    try:
        while True:
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
            try:
                result = await asyncio.to_thread(cache_mod.cleanup_expired)
                if result["exact_deleted"] or result["semantic_deleted"]:
                    log.info("cleanup: %s", result)
            except Exception:
                log.exception("cleanup iteration failed")
    except asyncio.CancelledError:
        log.info("cleanup loop cancelled")
        raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool.open(wait=True, timeout=30.0)
    log.info("Connection pool opened")

    try:
        cache_mod.bootstrap_schema()
        log.info("Schema bootstrap OK (expires_at columns + indexes)")
    except Exception:
        log.exception("Schema bootstrap failed -- app will continue")

    cleanup_task: asyncio.Task | None = None
    if cache_mod.CACHE_TTL_SECONDS > 0 and CLEANUP_INTERVAL_SECONDS > 0:
        cleanup_task = asyncio.create_task(_cleanup_loop())

    try:
        yield
    finally:
        if cleanup_task is not None:
            cleanup_task.cancel()
            try:
                await cleanup_task
            except (asyncio.CancelledError, Exception):
                pass
        pool.close()


app = FastAPI(
    title="Genie Cache Proxy",
    description=(
        "Caches Genie responses using exact-match + semantic (pg_vector) caches "
        "in Lakebase. Falls back to Genie Conversations API on miss."
    ),
    version="0.1.0",
    lifespan=lifespan,
)


@app.exception_handler(Exception)
async def unhandled_exc(request: Request, exc: Exception):
    log.exception("unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "detail": f"{type(exc).__name__}: {exc}",
            "trace": traceback.format_exception(exc)[-10:],
        },
    )


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    space_id: str | None = None
    bypass_cache: bool = False


class AskResponse(BaseModel):
    question: str
    source: str  # "exact_cache" | "semantic_cache" | "genie"
    response: dict
    latency_ms: int
    matched_question: str | None = None
    similarity: float | None = None


STATIC_DIR = Path(__file__).parent / "static"


@app.get("/")
def root():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/info")
def api_info():
    return {
        "name": "genie-cache-proxy",
        "description": (
            "Semantic caching proxy for a Databricks Genie space. "
            "Checks exact_cache -> semantic_cache (pg_vector) -> Genie API."
        ),
        "genie_space_id": DEFAULT_SPACE_ID,
        "endpoints": {
            "GET /": "browser UI",
            "GET /api/info": "this JSON",
            "GET /health": "health + configured space_id",
            "GET /stats": "cache row + hit counters",
            "GET /docs": "interactive OpenAPI docs",
            "POST /ask": {
                "body": {
                    "question": "string (required)",
                    "space_id": "string (optional; defaults to configured space)",
                    "bypass_cache": "bool (optional; skip both caches)",
                },
                "returns": {
                    "source": "exact_cache | semantic_cache | genie",
                    "response": "genie response payload",
                    "latency_ms": "int",
                    "matched_question": "string (semantic hits only)",
                    "similarity": "float (semantic hits only)",
                },
            },
        },
    }


@app.get("/health")
def health():
    return {"status": "ok", "space_id": DEFAULT_SPACE_ID}


@app.get("/stats")
def stats():
    return cache_mod.get_cache_stats()


@app.post("/admin/cleanup")
def admin_cleanup():
    """Manually trigger expired-row cleanup. Returns deletion counts."""
    return cache_mod.cleanup_expired()


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest):
    space_id = req.space_id or DEFAULT_SPACE_ID
    t0 = time.time()

    # Step 1: exact cache
    if not req.bypass_cache:
        hit = cache_mod.check_exact_cache(space_id, req.question)
        if hit:
            log.info("exact cache hit")
            return AskResponse(
                question=req.question,
                source="exact_cache",
                response=hit["response"],
                latency_ms=int((time.time() - t0) * 1000),
            )

    # Step 2: embed + semantic cache
    embedding = emb_mod.embed_text(req.question)

    if not req.bypass_cache:
        sem = cache_mod.check_semantic_cache(space_id, embedding)
        if sem:
            log.info(
                "semantic cache hit (similarity=%.3f)", sem["similarity"]
            )
            return AskResponse(
                question=req.question,
                source="semantic_cache",
                response=sem["response"],
                matched_question=sem["matched_question"],
                similarity=sem["similarity"],
                latency_ms=int((time.time() - t0) * 1000),
            )

    # Step 3: Genie fallback
    log.info("cache miss -> calling Genie")
    try:
        genie_resp = genie_mod.ask_genie(space_id, req.question)
    except genie_mod.GenieError as e:
        raise HTTPException(status_code=502, detail=f"Genie error: {e}") from e

    # Step 4: write-through to both caches
    try:
        cache_mod.write_caches(space_id, req.question, embedding, genie_resp)
    except Exception as e:  # noqa: BLE001
        log.exception("failed to write caches: %s", e)

    return AskResponse(
        question=req.question,
        source="genie",
        response=genie_resp,
        latency_ms=int((time.time() - t0) * 1000),
    )
