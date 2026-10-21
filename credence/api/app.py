"""Credence API.

Tech: Python, FastAPI

Responsibility (System architecture table, build plan):
Thin HTTP and WebSocket layer over the core; streams live events to the UI

Owner: API/UI (no decision logic here).

Product P0: a read-only, offline HTTP adapter for the Credence Markets UI (contracts/API-CONTRACT.md).
Every route maps to one client operation. Decisions come from Core (the procurement dry run) or
from recorded offline Integration runs; this layer only validates input and serializes. It holds
no agent key, accepts no signing secret, makes no purchase and builds no XRPL transaction.

I2a (provisional): when a `Marketplace` is supplied, the `/v0/live/...` routes serve the
IntegrationService embedded in this process (one process owns the database) with OFFLINE MOCK
providers. Agent sessions authenticate with a bearer token; purchases need the agent's own
Ed25519 signature over the prepared request, made outside the browser and the API. Still no
agent key is held or accepted, no live provider is called and no XRPL transaction is built.

Run (P0 only):  uv run uvicorn --factory credence.api.app:create_app --port 8000
Run (I2a):      uv run python -m credence.api.offline_demo serve <demo-dir>
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import FastAPI, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from credence.api.marketplace import Marketplace, MarketplaceError, PrepareRequest, Session
from credence.api.product import ProcurementForm, ProductError, ProductReadModel

API_PREFIX = "/v0"
# The local Vite dev server and preview, on loopback only.
LOCAL_ORIGIN_REGEX = r"^http://(localhost|127\.0\.0\.1)(:\d+)?$"


class ProcurementBody(BaseModel):
    """POST body for evaluateProcurement and submitPurchase. Amounts are integer micro-tRLUSD."""

    model_config = ConfigDict(extra="forbid", strict=True)

    model_family: str = Field(min_length=1)
    prompt: str = Field(min_length=1, max_length=4096)
    max_total_cost_micro_trlusd: int | None = Field(default=None, ge=0)
    minimum_evidence_tier: int = Field(ge=0, le=3)
    allowed_providers: list[str]
    allowed_precisions: list[str] | None = None
    allowed_quantizations: list[str] | None = None
    allowed_jurisdictions: list[str] = Field(min_length=1)
    allowed_data_residencies: list[str] = Field(min_length=1)
    facility_limit_micro_trlusd: int = Field(ge=0)
    facility_drawn_micro_trlusd: int = Field(ge=0)

    def form(self) -> ProcurementForm:
        return ProcurementForm(
            model_family=self.model_family,
            prompt=self.prompt,
            max_total_cost_micro_trlusd=self.max_total_cost_micro_trlusd,
            minimum_evidence_tier=self.minimum_evidence_tier,
            allowed_providers=tuple(self.allowed_providers),
            allowed_precisions=None
            if self.allowed_precisions is None
            else tuple(self.allowed_precisions),
            allowed_quantizations=None
            if self.allowed_quantizations is None
            else tuple(self.allowed_quantizations),
            allowed_jurisdictions=tuple(self.allowed_jurisdictions),
            allowed_data_residencies=tuple(self.allowed_data_residencies),
            facility_limit_micro_trlusd=self.facility_limit_micro_trlusd,
            facility_drawn_micro_trlusd=self.facility_drawn_micro_trlusd,
        )


class PrepareBody(BaseModel):
    """POST body for preparePurchase (I2a). The prompt itself is not sent, only its digest."""

    model_config = ConfigDict(extra="forbid", strict=True)

    facility_id: str | None = Field(default=None, min_length=1, max_length=128)
    idempotency_key: str | None = None
    prompt_digest: str
    model_family: str = Field(min_length=1)
    max_total_cost_micro_trlusd: int | None = Field(default=None, ge=0)
    minimum_evidence_tier: int = Field(ge=0, le=3)
    allowed_providers: list[str] = Field(min_length=1)
    allowed_precisions: list[str] | None = None
    allowed_quantizations: list[str] | None = None
    allowed_jurisdictions: list[str] = Field(min_length=1)
    allowed_data_residencies: list[str] = Field(min_length=1)

    def request(self) -> PrepareRequest:
        return PrepareRequest(
            form=ProcurementForm(
                model_family=self.model_family,
                prompt="",
                max_total_cost_micro_trlusd=self.max_total_cost_micro_trlusd,
                minimum_evidence_tier=self.minimum_evidence_tier,
                allowed_providers=tuple(self.allowed_providers),
                allowed_precisions=None
                if self.allowed_precisions is None
                else tuple(self.allowed_precisions),
                allowed_quantizations=None
                if self.allowed_quantizations is None
                else tuple(self.allowed_quantizations),
                allowed_jurisdictions=tuple(self.allowed_jurisdictions),
                allowed_data_residencies=tuple(self.allowed_data_residencies),
                facility_limit_micro_trlusd=0,
                facility_drawn_micro_trlusd=0,
            ),
            prompt_digest=self.prompt_digest,
            facility_id=self.facility_id,
            idempotency_key=self.idempotency_key,
        )


class SignedObjectBody(BaseModel):
    """A C3A SignedObject, exactly as `credence.core.signatures.sign` produces it."""

    model_config = ConfigDict(extra="forbid", strict=True)

    object_type: str
    schema_version: str
    object_digest: str
    key_id: str
    signature: str


class SubmitBody(BaseModel):
    """POST body for submitSignedPurchase (I2a): the prompt and the agent's signature."""

    model_config = ConfigDict(extra="forbid", strict=True)

    preparation_id: str = Field(min_length=1, max_length=64)
    prompt: str = Field(min_length=1, max_length=4096)
    signature: SignedObjectBody


class AuthorizationBody(BaseModel):
    """An agent-signed ResponseAuthorization, unchanged (I1e)."""

    model_config = ConfigDict(extra="forbid", strict=True)

    operation_id: str
    action: str
    agent_key_id: str
    nonce: str
    expires_at: str
    signature: str


def create_app(
    model: ProductReadModel | None = None, marketplace: Marketplace | None = None
) -> FastAPI:
    product = model or ProductReadModel()
    app = FastAPI(
        title="Credence API (Product P0, offline)",
        description="Read-only offline adapter. Amounts are integer micro-tRLUSD; tRLUSD is a "
        "test token. Accounting settlement is not XRPL settlement.",
        version="0.0.0",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=LOCAL_ORIGIN_REGEX,
        allow_methods=["GET", "POST"],
        allow_headers=["content-type", "authorization"],
    )

    @app.exception_handler(ProductError)
    async def product_error(_: Request, exc: ProductError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"error": exc.code, "detail": exc.detail}
        )

    @app.exception_handler(MarketplaceError)
    async def marketplace_error(_: Request, exc: MarketplaceError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"error": exc.code, "detail": exc.detail}
        )

    @app.get(f"{API_PREFIX}/health")
    def health() -> dict[str, Any]:
        record = product.health()
        record["integration"] = (
            {"integrated_service": False} if marketplace is None else marketplace.health()
        )
        return record

    @app.get(f"{API_PREFIX}/providers")
    def list_providers() -> list[dict[str, Any]]:
        return product.list_providers()

    @app.get(f"{API_PREFIX}/procurement/presets")
    def procurement_presets() -> list[dict[str, Any]]:
        return product.procurement_presets()

    @app.post(f"{API_PREFIX}/procurement/evaluate")
    def evaluate_procurement(body: ProcurementBody) -> dict[str, Any]:
        return product.evaluate_procurement(body.form())

    @app.post(f"{API_PREFIX}/purchases")
    def submit_purchase(body: ProcurementBody) -> dict[str, Any]:
        return product.submit_purchase(body.form())

    @app.get(f"{API_PREFIX}/operations")
    def list_operations() -> list[dict[str, Any]]:
        return product.list_operations()

    @app.get(f"{API_PREFIX}/operations/{{operation_id}}")
    def get_operation(operation_id: str) -> dict[str, Any]:
        return product.get_operation(operation_id)

    @app.get(f"{API_PREFIX}/operations/{{operation_id}}/exposure")
    def get_accounting_exposure(operation_id: str) -> dict[str, Any]:
        return product.get_accounting_exposure(operation_id)

    @app.get(f"{API_PREFIX}/operations/{{operation_id}}/payment")
    def get_payment_status(operation_id: str) -> dict[str, Any]:
        return product.get_payment_status(operation_id)

    @app.post(f"{API_PREFIX}/operations/{{operation_id}}/response")
    def retrieve_authorized_response(operation_id: str) -> JSONResponse:
        product.get_operation(operation_id)  # 404 for an unknown operation
        return JSONResponse(
            status_code=501,
            content={
                "error": "RESPONSE_RETRIEVAL_NOT_CONNECTED",
                "detail": "Retrieval needs an agent-signed ResponseAuthorization verified by the "
                "accepted integration service (I1g/I1h), which is not connected. The API holds "
                "no agent key and does not accept one.",
            },
        )

    @app.get(f"{API_PREFIX}/receipts")
    def list_receipts() -> list[dict[str, Any]]:
        return product.list_receipts()

    @app.get(f"{API_PREFIX}/receipts/{{receipt_digest}}")
    def get_receipt_evidence(receipt_digest: str) -> dict[str, Any]:
        return product.get_receipt_evidence(receipt_digest)

    @app.get(f"{API_PREFIX}/xrpl/settlement-fixtures")
    def xrpl_settlement_fixtures() -> dict[str, Any]:
        return product.xrpl_settlement_fixtures()

    _live_routes(app, marketplace)
    return app


LIVE = f"{API_PREFIX}/live"
# The agent session token, from the Authorization header only (module level so FastAPI can
# resolve it under postponed annotations).
Auth = Annotated[str | None, Header()]


def _live_routes(app: FastAPI, marketplace: Marketplace | None) -> None:
    """The I2a routes. Every one needs an agent session; none returns another agent's data."""

    def session(authorization: str | None) -> tuple[Marketplace, Session]:
        if marketplace is None:
            raise MarketplaceError(
                "INTEGRATED_SERVICE_NOT_RUNNING",
                "this API process has no embedded IntegrationService (run offline_demo serve)",
                503,
            )
        return marketplace, marketplace.authenticate(authorization)

    @app.get(f"{LIVE}/session")
    def get_session(authorization: Auth = None) -> dict[str, Any]:
        m, s = session(authorization)
        return m.session(s)

    @app.get(f"{LIVE}/facilities")
    def list_facilities(authorization: Auth = None) -> list[dict[str, Any]]:
        m, s = session(authorization)
        return m.facilities(s)

    @app.post(f"{LIVE}/purchases/prepare")
    def prepare_purchase(body: PrepareBody, authorization: Auth = None) -> dict[str, Any]:
        m, s = session(authorization)
        return m.prepare(s, body.request())

    @app.get(f"{LIVE}/purchases/preparations/{{preparation_id}}")
    def get_preparation(preparation_id: str, authorization: Auth = None) -> dict[str, Any]:
        m, s = session(authorization)
        return m.preparation(s, preparation_id)

    @app.post(f"{LIVE}/purchases/submit")
    def submit_signed_purchase(body: SubmitBody, authorization: Auth = None) -> dict[str, Any]:
        m, s = session(authorization)
        return m.submit(s, body.preparation_id, body.prompt, body.signature.model_dump())

    @app.get(f"{LIVE}/operations")
    def list_live_operations(
        authorization: Auth = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        after: str | None = None,
    ) -> dict[str, Any]:
        m, s = session(authorization)
        return m.operations(s, limit=limit, after=after)

    @app.get(f"{LIVE}/operations/{{operation_id}}")
    def get_live_operation(operation_id: str, authorization: Auth = None) -> dict[str, Any]:
        m, s = session(authorization)
        return m.operation(s, operation_id)

    @app.get(f"{LIVE}/operations/{{operation_id}}/exposure")
    def get_live_exposure(operation_id: str, authorization: Auth = None) -> dict[str, Any]:
        m, s = session(authorization)
        return m.exposure(s, operation_id)

    @app.get(f"{LIVE}/operations/{{operation_id}}/payment")
    def get_live_payment(operation_id: str, authorization: Auth = None) -> dict[str, Any]:
        m, s = session(authorization)
        return m.payment(s, operation_id)

    @app.post(f"{LIVE}/operations/{{operation_id}}/response")
    def live_response(
        operation_id: str, body: AuthorizationBody, authorization: Auth = None
    ) -> Response:
        m, s = session(authorization)
        action, data = m.respond(s, operation_id, body.model_dump())
        if action == "acknowledge":
            return Response(status_code=204)
        assert data is not None
        return Response(content=data, media_type="application/octet-stream")

    @app.get(f"{LIVE}/receipts/{{receipt_digest}}")
    def get_live_receipt(receipt_digest: str, authorization: Auth = None) -> dict[str, Any]:
        m, s = session(authorization)
        return m.receipt(s, receipt_digest)
