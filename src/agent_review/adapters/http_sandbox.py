from __future__ import annotations

import logging

import httpx
from pydantic import ValidationError

from agent_review.errors import PermanentError, TransientError
from agent_review.ports.sandbox import VerifyReport, VerifyRequest

logger = logging.getLogger(__name__)

# Holgura sobre el presupuesto del runner: el `POST /verify` es bloqueante y debe
# esperar a que el runner termine y responda. Si el cliente cortara justo en
# `sandbox.timeout_s`, el runner seguiria trabajando sin nadie leyendo el informe
# y un fix correcto se marcaria como `NOT_VERIFIABLE` por una carrera.
_CLIENT_SLACK_S = 30.0


class SandboxError(PermanentError):
    """Fallo no recuperable del runner: peticion invalida (4xx), respuesta ilegible."""


class SandboxUnavailable(TransientError):
    """El runner no responde o responde 5xx/429: reintentar puede resolverlo."""


class HttpSandboxAdapter:
    """`SandboxPort` sobre el servicio `sandbox-runner` (E3, HTTP interno).

    Una unica operacion de alto nivel: `POST /verify` bloqueante. El ciclo de vida
    del workspace (clonar -> aplicar -> ejecutar -> destruir) vive entero en el
    runner; el worker solo envia el parche y los nombres de recipe. El `run_key`
    es idempotente: un `POST` repetido tras una caida del worker devuelve el run
    ya echo en vez de lanzar una segunda ejecucion (R8).

    Se eligio bloqueante sobre `POST /runs` + sondeo porque el worker es
    secuencial: el sondeo no desacoplaria la cola, solo anadiria estado de runs.
    """

    def __init__(
        self,
        *,
        base_url: str,
        token: str = "",
        timeout_s: float = 1200.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token.strip()
        self._timeout = timeout_s + _CLIENT_SLACK_S
        self._client = client

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _post(self, payload: dict[str, object]) -> httpx.Response:
        url = f"{self._base_url}/verify"
        try:
            if self._client is not None:
                return self._client.post(
                    url, json=payload, headers=self._headers(), timeout=self._timeout
                )
            return httpx.post(url, json=payload, headers=self._headers(), timeout=self._timeout)
        except httpx.HTTPError as exc:
            # Red, DNS o timeout de transporte: el runner no contesto. Reintentable.
            raise SandboxUnavailable(f"El sandbox-runner no respondio: {exc}") from exc

    def verify(self, request: VerifyRequest) -> VerifyReport:
        response = self._post(request.model_dump(mode="json"))
        if response.status_code >= 400:
            detail = _error_detail(response)
            # 5xx y 429 cambian entre intentos; el resto (4xx) es una peticion mal
            # formada que reintentar solo repetiria. Misma politica que `is_transient`.
            transient = response.status_code >= 500 or response.status_code == 429
            error = SandboxUnavailable if transient else SandboxError
            raise error(
                f"El sandbox-runner rechazo la verificacion ({response.status_code}): {detail}"
            )
        try:
            return VerifyReport.model_validate(response.json())
        except (ValidationError, ValueError) as exc:
            raise SandboxError(f"Informe del sandbox ilegible: {exc}") from exc


def _error_detail(response: httpx.Response) -> str:
    """Mensaje compacto del runner; nunca el cuerpo entero en un log."""
    try:
        body = response.json()
    except ValueError:
        return response.text.strip()[:500] or "sin detalle"
    if isinstance(body, dict) and "detail" in body:
        return str(body["detail"])[:500]
    return str(body)[:500]
