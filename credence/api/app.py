"""Credence API.

Tech: Python, FastAPI

Responsibility (System architecture table, build plan):
Thin HTTP and WebSocket layer over the core; streams live events to the UI

Owner: API/UI (no decision logic here).

Product P0: a read-only, offline HTTP adapter for the Credence Markets UI (contracts/API-CONTRACT.md).
Every route maps to one client operation. Decisions come from Core (the procurement dry run) or
from recorded offline Integration runs; this layer only validates input and serializes. It holds
no agent key, accepts no signing secret, makes no purchase and builds no XRPL transaction.

Run:  uv run uvicorn --factory credence.api.app:create_app --port 8000
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

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


def create_app(model: ProductReadModel | None = None) -> FastAPI:
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
        allow_headers=["content-type"],
    )

    @app.exception_handler(ProductError)
    async def product_error(_: Request, exc: ProductError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"error": exc.code, "detail": exc.detail}
        )

    @app.get(f"{API_PREFIX}/health")
    def health() -> dict[str, Any]:
        return product.health()

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

    return app
