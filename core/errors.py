"""Error taxonomy for every OneNess TOR surface.

Every failure crosses the API boundary as the Section 7.1 envelope:

    {"ok": false, "data": null,
     "error": {"code": ..., "http_status": ..., "message": ..., "details": ...}}

Rules: ``message`` is always user-presentable (no tracebacks, no secrets);
``code`` is stable and machine-readable so the Ollama brain can react to it
without parsing English; ``details`` is optional structured context.
"""
from __future__ import annotations


class AppError(Exception):
    """Base class for every user-presentable failure."""

    code = "INTERNAL"
    http_status = 500

    def __init__(self, message: str, *, code: str | None = None,
                 http_status: int | None = None,
                 details: dict | None = None) -> None:
        super().__init__(message)
        self.message = str(message)
        if code:
            self.code = code
        if http_status:
            self.http_status = http_status
        self.details = details or {}

    def to_error(self) -> dict:
        out = {"code": self.code, "http_status": self.http_status,
               "message": self.message}
        if self.details:
            out["details"] = self.details
        return out


class ValidationError(AppError):
    code = "VALIDATION_ERROR"
    http_status = 400


class NotFound(AppError):
    code = "NOT_FOUND"
    http_status = 404


class Unauthorized(AppError):
    code = "UNAUTHORIZED"
    http_status = 401


class Forbidden(AppError):
    code = "FORBIDDEN"
    http_status = 403


class FeeError(AppError):
    """A fee could not be charged — nothing was delivered."""
    code = "INSUFFICIENT_FEE"
    http_status = 402


class ChainUnavailable(AppError):
    """The node RPC is down/unreachable — nothing was charged."""
    code = "CHAIN_UNAVAILABLE"
    http_status = 503


class DexNotDeployed(AppError):
    """Placeholder seam: the on-chain DEX contract does not exist yet.

    Every DEX-dependent route answers with this so the UI and the AI can say
    "not yet" honestly instead of inventing prices or liquidity.
    """
    code = "DEX_NOT_DEPLOYED"
    http_status = 501


class NftNotFound(AppError):
    """No NFT / no metadata exists for the given address + token id."""

    code = "NFT_NOT_FOUND"
    http_status = 404


class NftUnavailable(AppError):
    """The Solana RPC or the metadata host did not answer — nothing is invented."""

    code = "NFT_RPC_UNAVAILABLE"
    http_status = 503


class EngineNotDeployed(AppError):
    """Placeholder seam for a tool whose execution engine is not built yet.

    The record (a paid session, a queued job) is always stored first and says so;
    this error is what a *run* call returns so the UI and the AI cannot pretend
    work happened.
    """

    code = "ENGINE_NOT_DEPLOYED"
    http_status = 501


class TorUnavailable(AppError):
    code = "TOR_DAEMON_UNAVAILABLE"
    http_status = 503


class OllamaUnavailable(AppError):
    code = "OLLAMA_UNAVAILABLE"
    http_status = 503
