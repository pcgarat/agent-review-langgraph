from __future__ import annotations

import logging

import httpx

from agent_review.errors import PermanentError, TransientError
from agent_review.ports.context import Ref, is_denied

logger = logging.getLogger(__name__)


class ContextUnavailable(TransientError):
    """El runner de lectura no responde (red, 5xx, 503 de clonado): reintentable."""


class ContextError(PermanentError):
    """Peticion de lectura rechazada (4xx que no es un `410` recuperable, respuesta ilegible)."""


class _WorkspaceGone(ContextError):
    """El workspace ya no existe (`410`): se reabre una vez y, si falla, se degrada.

    Hereda de `ContextError` a proposito: asi lo captura la degradacion del lector
    (`adapters/context_factory.py`) cuando la reapertura tampoco lo recupera.
    """


class WorkspaceContextReader:
    """`ContextReaderPort` sobre los endpoints `/workspaces` del runner (016 W5, W8).

    El clon *bare* vive en el runner, que es el unico proceso con credencial de
    clonado ([ADR-0011](../../docs/adr/0011-sandbox-runner-aislado.md)). Este
    adaptador abre el workspace de forma idempotente (el runner lo hace por
    `(repo, head_sha)`) y sirve las lecturas por HTTP.

    Un `410` es un workspace perdido (reinicio del runner, TTL o desalojo): se reabre
    **una vez**; si vuelve a fallar, la excepcion sube y quien llama degrada a
    `VcsContextReader`. Un `404` de `file` es un fichero ausente (`None`) y no gasta
    la reapertura.
    """

    def __init__(
        self,
        *,
        base_url: str,
        repo: str,
        head_sha: str,
        base_sha: str = "",
        token: str = "",
        denylist: tuple[str, ...] = (),
        timeout_s: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._repo = repo
        self._head = head_sha
        self._base = base_sha
        self._token = token.strip()
        self._denylist = denylist
        self._timeout = timeout_s
        self._client = client
        self._workspace_id: str | None = None
        self._reopened = False

    def read_file(
        self,
        path: str,
        *,
        ref: Ref = "head",
        start: int | None = None,
        end: int | None = None,
    ) -> str | None:
        if is_denied(path, self._denylist):
            return None
        params: dict[str, str | int] = {"path": path, "ref": ref}
        if start is not None:
            params["start"] = start
        if end is not None:
            params["end"] = end
        response = self._request("GET", "file", params=params)
        if response.status_code == 403:
            # Ruta vetada por el runner: no es un workspace perdido, es un veto.
            logger.info("lector workspace: ruta vetada %s", path)
            return None
        if response.status_code == 404:
            return None
        self._raise_for_status(response)
        return str(response.json().get("content", ""))

    def grep(self, pattern: str, *, glob: str | None = None) -> list[str]:
        if not pattern:
            return []
        payload: dict[str, object] = {"pattern": pattern}
        if glob:
            payload["glob"] = glob
        response = self._request("POST", "grep", json=payload)
        self._raise_for_status(response)
        matches = response.json().get("matches", [])
        return [str(match) for match in matches if isinstance(match, str)]

    def list_dir(self, path: str) -> list[str]:
        response = self._request("GET", "tree", params={"path": path})
        self._raise_for_status(response)
        entries = response.json().get("entries", [])
        return [str(entry) for entry in entries if isinstance(entry, str)]

    def _request(
        self,
        method: str,
        suffix: str,
        *,
        params: dict[str, str | int] | None = None,
        json: dict[str, object] | None = None,
        retry_workspace: bool = True,
    ) -> httpx.Response:
        workspace_id = self._ensure_workspace()
        url = f"{self._base_url}/workspaces/{workspace_id}/{suffix}"
        try:
            response = self._send(method, url, params=params, json=json)
        except httpx.HTTPError as exc:
            raise ContextUnavailable(f"el runner de lectura no respondio: {exc}") from exc
        if response.status_code == 410 and retry_workspace and not self._reopened:
            # El runner pudo reiniciarse, caducar o desalojar el clon: se reabre una
            # sola vez. Un `404` es un fichero ausente y no gasta la reapertura.
            self._reopened = True
            self._workspace_id = None
            return self._request(method, suffix, params=params, json=json, retry_workspace=False)
        return response

    def _send(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str | int] | None,
        json: dict[str, object] | None,
    ) -> httpx.Response:
        headers = self._headers()
        if self._client is not None:
            return self._client.request(
                method, url, params=params, json=json, headers=headers, timeout=self._timeout
            )
        return httpx.request(
            method, url, params=params, json=json, headers=headers, timeout=self._timeout
        )

    def _ensure_workspace(self) -> str:
        if self._workspace_id is not None:
            return self._workspace_id
        url = f"{self._base_url}/workspaces"
        payload = {"repo": self._repo, "head_sha": self._head, "base_sha": self._base}
        try:
            if self._client is not None:
                response = self._client.post(
                    url, json=payload, headers=self._headers(), timeout=self._timeout
                )
            else:
                response = httpx.post(
                    url, json=payload, headers=self._headers(), timeout=self._timeout
                )
        except httpx.HTTPError as exc:
            raise ContextUnavailable(f"no se pudo abrir el workspace: {exc}") from exc
        if response.status_code >= 400:
            self._raise_for_status(response)
        workspace_id = str(response.json().get("workspace_id", ""))
        if not workspace_id:
            raise ContextError("el runner no devolvio workspace_id")
        self._workspace_id = workspace_id
        return workspace_id

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        detail = _error_detail(response)
        if response.status_code >= 500 or response.status_code == 429:
            raise ContextUnavailable(
                f"el runner de lectura devolvio {response.status_code}: {detail}"
            )
        if response.status_code == 410:
            raise _WorkspaceGone(f"workspace no disponible: {detail}")
        raise ContextError(f"el runner rechazo la lectura ({response.status_code}): {detail}")


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text.strip()[:500] or "sin detalle"
    if isinstance(body, dict) and "detail" in body:
        return str(body["detail"])[:500]
    return str(body)[:500]
