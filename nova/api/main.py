"""FastAPI gateway exposing endpoints for memory ingestion, QA, verification, and BYOK setup."""

import logging
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictBool
from starlette.exceptions import HTTPException as StarletteHTTPException

from nova.api.bff import build_bff_router
from nova.api.errors import PublicError, map_exception
from nova.api.limits import MAX_MESSAGES, Answer, Identifier, Question
from nova.api.middleware import BodyLimitMiddleware, RequestContextMiddleware, current_request_id
from nova.api.runtime import Runtime, resolve_tenant
from nova.audit.logger import AuditError, AuditRecord
from nova.core.config import configure_logging, get_settings
from nova.core.schemas import Decision, IngestReport, Message
from nova.extraction.extractor import ExtractionError
from nova.identity.config import IdentitySettings
from nova.identity.errors import IdentityError
from nova.pipeline import Anchor
from nova.providers.config import ConnectionTestResult, ProviderConfigRequest, ProviderStatus
from nova.providers.errors import ProviderError
from nova.retrieval.embedder import EmbeddingError
from nova.retrieval.hybrid import MAX_K, RetrievalError
from nova.security.secrets import SecretStoreError
from nova.store.base import StorageUnavailableError, StoreError
from nova.store.postgres import PostgresConfigRequest
from nova.store.service import StorageStatus
from nova.verification.claim_splitter import VerificationError

logger = logging.getLogger("nova.api")
DELETE_CONFIRMATION = "delete-all-data"


class IngestRequest(BaseModel):
    """Payload for memory ingestion."""

    session_id: Identifier
    messages: list[Message] = Field(min_length=1, max_length=MAX_MESSAGES)


class AskRequest(BaseModel):
    """Payload for question answering."""

    question: Question
    draft_answer: Answer | None = None
    k: int = Field(default=5, ge=1, le=MAX_K)
    include_history: StrictBool = False


class VerifyRequest(BaseModel):
    """Payload for answer verification."""

    question: Question
    answer: Answer
    k: int = Field(default=5, ge=1, le=MAX_K)
    include_history: StrictBool = False


_state: dict[str, Runtime] = {}
_runtime_lock = threading.RLock()


def get_runtime() -> Runtime:
    """Return the shared Runtime, rebuilding it if the environment configuration changed."""
    settings = get_settings()
    settings.validate_supported()
    identity_settings = IdentitySettings.from_env()
    with _runtime_lock:
        current = _state.get("runtime")
        if current is not None and (
            current.settings != settings or current.identity_settings != identity_settings
        ):
            current.close()
            current = None
        if current is None:
            current = Runtime(settings, identity_settings=identity_settings)
            _state["runtime"] = current
        return current


def close_runtime() -> None:
    """Close and forget the shared Runtime."""
    with _runtime_lock:
        runtime = _state.pop("runtime", None)
        if runtime is not None:
            runtime.close()


def get_tenant(
    x_api_key: Annotated[str | None, Header()] = None,
    x_tenant_id: Annotated[str | None, Header()] = None,
) -> str:
    """Authenticate the request and return its tenant id."""
    return resolve_tenant(get_settings(), x_api_key, x_tenant_id)


def get_anchor(tenant: Annotated[str, Depends(get_tenant)]) -> Iterator[Anchor]:
    """Yield a tenant-scoped Anchor and release its connections after the response."""
    with get_runtime().anchor_for(tenant) as anchor:
        yield anchor


def _error_response(status: int, error: str, detail: str, **extra: Any) -> JSONResponse:
    body = {"error": error, "detail": detail, "request_id": current_request_id(), **extra}
    return JSONResponse(status_code=status, content=body)


def _handle(request: Request, exc: Exception) -> JSONResponse:
    mapped = map_exception(exc)
    rid = current_request_id()
    if mapped.status >= 500:
        logger.error("request failed [%s] %s %s", rid, mapped.error, request.url.path, exc_info=exc)
    else:
        logger.info("request rejected [%s] %s %s", rid, mapped.error, type(exc).__name__)
    return _error_response(mapped.status, mapped.error, mapped.detail)


def create_app() -> FastAPI:
    """Create and configure FastAPI application instance with error handlers."""

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        settings = get_settings()
        settings.validate_supported()
        configure_logging(settings)
        if not settings.api_key and not settings.tenant_api_keys:
            logger.warning(
                "Nova API is running without authentication in %s mode", settings.environment
            )
        runtime = get_runtime()
        try:
            runtime.purge_expired_audit()
        except (StoreError, AuditError):
            logger.warning("audit retention purge failed at startup", exc_info=True)
        try:
            runtime.purge_expired_sessions()
        except StoreError:
            logger.warning("expired session purge failed at startup", exc_info=True)
        try:
            yield
        finally:
            close_runtime()

    application = FastAPI(title="Nova API Gateway", lifespan=lifespan)
    settings = get_settings()
    identity_settings = IdentitySettings.from_env()
    if identity_settings.browser_enabled:
        identity_settings.validate_for_environment(settings.environment)
        application.include_router(build_bff_router(), prefix="/api")
    application.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_body_bytes)
    application.add_middleware(RequestContextMiddleware)

    for exc_type in (
        PublicError,
        ExtractionError,
        VerificationError,
        ProviderError,
        SecretStoreError,
        StorageUnavailableError,
        StoreError,
        AuditError,
        EmbeddingError,
        RetrievalError,
        IdentityError,
        NotImplementedError,
    ):
        application.add_exception_handler(exc_type, _handle)

    @application.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [
            {"loc": [str(p) for p in e.get("loc", ())], "msg": str(e.get("msg", "invalid"))}
            for e in exc.errors()
        ]
        return _error_response(422, "validation_error", "The request is invalid.", fields=fields)

    @application.exception_handler(StarletteHTTPException)
    async def http_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _error_response(exc.status_code, "http_error", str(exc.detail))

    @application.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        return _handle(request, exc)

    Tenant = Annotated[str, Depends(get_tenant)]
    AnchorDep = Annotated[Anchor, Depends(get_anchor)]

    @application.post("/ingest", response_model=IngestReport)
    def ingest_endpoint(payload: IngestRequest, anchor: AnchorDep, _: Tenant) -> IngestReport:
        return anchor.ingest(payload.session_id, payload.messages)

    @application.post("/ask", response_model=Decision)
    def ask_endpoint(payload: AskRequest, anchor: AnchorDep, _: Tenant) -> Decision:
        if payload.k == 5 and not payload.include_history:
            return anchor.ask(payload.question, payload.draft_answer)
        return anchor.ask(
            payload.question,
            payload.draft_answer,
            k=payload.k,
            include_history=payload.include_history,
        )

    @application.post("/verify", response_model=Decision)
    def verify_endpoint(payload: VerifyRequest, anchor: AnchorDep, _: Tenant) -> Decision:
        if payload.k == 5 and not payload.include_history:
            return anchor.verify(payload.question, payload.answer)
        return anchor.verify(
            payload.question,
            payload.answer,
            k=payload.k,
            include_history=payload.include_history,
        )

    @application.get("/audit/{id}", response_model=AuditRecord)
    def audit_endpoint(id: str, anchor: AnchorDep, _: Tenant) -> AuditRecord:
        record = anchor.get_audit(id)
        if record is None:
            raise HTTPException(status_code=404, detail="Audit record not found")
        return record

    # ---- provider (BYOK) ----------------------------------------------------------------
    @application.get("/provider", response_model=ProviderStatus)
    def provider_get(tenant: Tenant) -> ProviderStatus:
        return get_runtime().providers.status(tenant)

    @application.put("/provider", response_model=ProviderStatus)
    def provider_put(payload: ProviderConfigRequest, tenant: Tenant) -> ProviderStatus:
        return get_runtime().providers.configure(tenant, payload)

    @application.delete("/provider", status_code=204)
    def provider_delete(tenant: Tenant) -> None:
        get_runtime().providers.disconnect(tenant)

    @application.post("/provider/test", response_model=ConnectionTestResult)
    def provider_test(
        tenant: Tenant, payload: ProviderConfigRequest | None = None
    ) -> ConnectionTestResult:
        return get_runtime().providers.test(tenant, payload)

    # ---- storage ------------------------------------------------------------------------
    @application.get("/storage", response_model=StorageStatus)
    def storage_get(tenant: Tenant) -> StorageStatus:
        return get_runtime().storage.status(tenant)

    @application.put("/storage", response_model=StorageStatus)
    def storage_put(payload: PostgresConfigRequest, tenant: Tenant) -> StorageStatus:
        return get_runtime().storage.configure(tenant, payload)

    @application.delete("/storage", status_code=204)
    def storage_delete(tenant: Tenant) -> None:
        get_runtime().storage.disconnect(tenant)

    @application.post("/storage/test", response_model=ConnectionTestResult)
    def storage_test(
        tenant: Tenant, payload: PostgresConfigRequest | None = None
    ) -> ConnectionTestResult:
        return get_runtime().storage.test(tenant, payload)

    @application.post("/storage/initialize")
    def storage_initialize(tenant: Tenant) -> dict[str, int]:
        return {"schema_version": get_runtime().storage.initialize(tenant)}

    @application.get("/data/export")
    def data_export(
        tenant: Tenant, limit: Annotated[int, Query(ge=1, le=10_000)] = 10_000
    ) -> dict[str, object]:
        return get_runtime().storage.export(tenant, limit)

    @application.delete("/data")
    def data_delete(tenant: Tenant, confirm: str = "") -> dict[str, int]:
        if confirm != DELETE_CONFIRMATION:
            raise PublicError(
                400, "confirmation_required", f"Pass confirm={DELETE_CONFIRMATION} to delete data."
            )
        return get_runtime().storage.delete_data(tenant)

    # ---- health -------------------------------------------------------------------------
    @application.get("/healthz")
    def healthz_endpoint() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/readyz")
    def readyz_endpoint() -> JSONResponse:
        checks = get_runtime().ready()
        ok = all(v == "ok" for v in checks.values())
        return JSONResponse(
            status_code=200 if ok else 503,
            content={"status": "ready" if ok else "not_ready", "checks": checks},
        )

    return application


app = create_app()
