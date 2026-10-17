from __future__ import annotations

import logging

import httpx

from agent_review.errors import PermanentError, TransientError
from agent_review.ports.context import (
    CAP_DIR_DEPTH,
    CAP_DIR_GLOB,
    CAP_GREP_FILES_ONLY,
    CAP_GREP_MODE,
    CAP_GREP_PATH,
    CAP_GREP_REF,
    CAP_READ_MODE,
    GrepMode,
    ReadMode,
    ReadResult,
    Ref,
    ToolError,
    ToolResult,
    is_denied,
    validate_range,
)

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
        self._caps: frozenset[str] = frozenset()
        self._reopened = False

    def read_file(
        self,
        path: str,
        *,
        ref: Ref = "head",
        start: int | None = None,
        end: int | None = None,
        mode: ReadMode = "full",
    ) -> ReadResult | None:
        if is_denied(path, self._denylist):
            return None
        # Un rango imposible se rechaza antes de gastar una peticion HTTP (y no es
        # un fallo del runner: no debe degradar el lector al VCS).
        validate_range(start, end)
        if mode != "full":
            self._require(CAP_READ_MODE, "mode")
        params: dict[str, str | int] = {"path": path, "ref": ref}
        if mode != "full":
            params["mode"] = mode
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
        payload = response.json()
        content = str(payload.get("content", ""))
        return ReadResult(
            content=content,
            total_bytes=_as_optional_int(payload.get("total_bytes")),
            truncated=payload.get("truncated") is True,
            served_range=_as_range(payload.get("served_range")),
        )

    def grep(
        self,
        pattern: str,
        *,
        glob: str | None = None,
        path: str | None = None,
        ref: Ref = "head",
        mode: GrepMode = "literal",
        files_only: bool = False,
    ) -> ToolResult:
        if not pattern:
            return ToolResult()
        payload: dict[str, object] = {"pattern": pattern, "ref": ref}
        if glob:
            payload["glob"] = glob
        if path:
            self._require(CAP_GREP_PATH, "path")
            payload["path"] = path
        if ref != "head":
            self._require(CAP_GREP_REF, "ref")
        if mode != "literal":
            self._require(CAP_GREP_MODE, "mode")
            payload["mode"] = mode
        if files_only:
            self._require(CAP_GREP_FILES_ONLY, "files_only")
            payload["files_only"] = True
        response = self._request("POST", "grep", json=payload)
        self._raise_for_status(response)
        body = response.json()
        matches = body.get("matches", [])
        items = tuple(str(match) for match in matches if isinstance(match, str))
        return ToolResult(
            items=items,
            total=_as_optional_int(body.get("total")),
            truncated=body.get("truncated") is True,
        )

    def list_dir(
        self,
        path: str,
        *,
        glob: str | None = None,
        depth: int = 1,
    ) -> ToolResult:
        if depth < 1:
            raise ToolError(f"`depth` debe ser >= 1 (recibido {depth})")
        params: dict[str, str | int] = {"path": path}
        if glob:
            self._require(CAP_DIR_GLOB, "glob")
            params["glob"] = glob
        if depth != 1:
            self._require(CAP_DIR_DEPTH, "depth")
            params["depth"] = depth
        response = self._request("GET", "tree", params=params)
        self._raise_for_status(response)
        body = response.json()
        entries = body.get("entries", [])
        items = tuple(str(entry) for entry in entries if isinstance(entry, str))
        return ToolResult(
            items=items,
            total=_as_optional_int(body.get("total")),
            truncated=body.get("truncated") is True,
        )

    def _require(self, capability: str, field: str) -> None:
        """Falla si el runner no anuncia la capacidad; nunca la ignora en silencio.

        Un runner viejo no manda `capabilities`: su conjunto es vacio y cualquier
        campo nuevo se rechaza con un error que el modelo puede corregir (R3c).
        """
        if capability in self._capabilities():
            return
        raise ToolError(
            f"el runner de lectura no soporta `{field}`; "
            "actualizalo o repite la peticion sin ese parametro"
        )

    def _capabilities(self) -> frozenset[str]:
        self._ensure_workspace()
        return self._caps

    def _lock_capabilities(self, payload: dict[str, object]) -> None:
        raw = payload.get("capabilities")
        if isinstance(raw, list):
            self._caps = frozenset(str(item) for item in raw if isinstance(item, str))
        else:
            # Runner anterior a 023: no declara nada, asi que no se le atribuye
            # ninguna capacidad nueva.
            self._caps = frozenset()

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
        body = response.json()
        workspace_id = str(body.get("workspace_id", ""))
        if not workspace_id:
            raise ContextError("el runner no devolvio workspace_id")
        self._lock_capabilities(body)
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
        if response.status_code == 400:
            # Peticion mal formada (patron no portable, rango imposible): el modelo
            # puede corregirla y no es motivo para degradar el lector al VCS (C0b).
            raise ToolError(detail)
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


def _as_optional_int(value: object) -> int | None:
    """`int` del JSON, o `None` si falta o no es un entero (nunca un `bool`)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _as_range(value: object) -> tuple[int, int] | None:
    if not isinstance(value, list) or len(value) != 2:
        return None
    low, high = value
    if isinstance(low, bool) or isinstance(high, bool):
        return None
    if not isinstance(low, int) or not isinstance(high, int):
        return None
    return (low, high)
