from __future__ import annotations

import base64
import logging
from collections.abc import Mapping
from typing import Any

import httpx

from agent_review.adapters.diff_parser import parse_unified_diff, resolve_added_line
from agent_review.errors import PermanentError, TransientError
from agent_review.ports.vcs import InlineAnchor, PullRequestInfo, ReviewMarker

logger = logging.getLogger(__name__)

_DEFAULT_API_URL = "https://api.github.com"
# Cabeceras que pide la API de GitHub. `diff` es la unica que devuelve el diff
# unificado directamente (y ademas con los bytes correctos del fichero), asi que
# no se pide el JSON+patch, que resecala cada linea por caracteres no ASCII.
_DIFF_ACCEPT = "application/vnd.github.v3.diff"
_JSON_ACCEPT = "application/vnd.github+json"
# Un PR con mas de 3000 ficheros no se puede paginar entero (limite de la API);
# `get_diff` tampoco lo devolveria. Es una capacidad de GitHub, no una eleccion.
_MAX_PAGES = 30
_PER_PAGE = 100


class GithubError(PermanentError):
    """Fallo no recuperable de la API de GitHub: peticion invalida o credencial."""


class GithubUnavailable(TransientError):
    """La API no responde, responde 5xx/429 o el transporte falla: reintentable."""


class GithubVcsAdapter:
    """`VcsPort` sobre la API REST de GitHub (v3).

    Simetrico a `TwgVcsAdapter` pero sin CLI: el proyecto ya habla HTTP
    (`httpx`) con el sandbox y la API. La credencial es un PAT *fine-grained*
    con `pull_requests: write` (comentar el PR) y `contents: read` (leer el
    fichero en Fase 3).

    La API no tiene un endpoint "resuelveme esta linea del diff": `resolve_inline_anchor`
    se calcula en cliente con el `diff_parser` compartido. Comentar el PR es
    siempre publico (nunca un review pending): el agente no aprueba PRs.
    """

    def __init__(
        self,
        *,
        owner: str,
        repo: str,
        token: str,
        api_url: str = _DEFAULT_API_URL,
        timeout_s: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._owner = owner
        self._repo = repo
        self._token = token.strip()
        self._api_url = (api_url or _DEFAULT_API_URL).rstrip("/")
        self._timeout = timeout_s
        self._client = client
        # El diff se pide una vez por PR y se reusa entre ancla y validacion:
        # sin cache, cada hallazgo descargaria el PR entero otra vez.
        self._diff_cache: dict[int, str] = {}

    def _headers(self, accept: str = _JSON_ACCEPT) -> dict[str, str]:
        headers = {
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "agent-review-langgraph",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Peticion cruda al API. `kwargs` es de httpx (`params`, `json`, `headers`);

        `accept` viaja aparte porque cada llamada elige su representacion (JSON o
        diff) y no se debe poder pisar por accidente desde los kwargs.
        """
        url = f"{self._api_url}{path}"
        headers = self._headers(kwargs.pop("accept", _JSON_ACCEPT))
        try:
            if self._client is not None:
                return self._client.request(
                    method, url, headers=headers, timeout=self._timeout, **kwargs
                )
            return httpx.request(method, url, headers=headers, timeout=self._timeout, **kwargs)
        except httpx.HTTPError as exc:
            # Red, DNS o timeout de transporte: no hubo respuesta que clasificar.
            raise GithubUnavailable(f"La API de GitHub no respondio: {exc}") from exc

    def _check(self, response: httpx.Response, action: str) -> None:
        if response.status_code < 400:
            return
        detail = _error_detail(response)
        # 5xx/429 cambian entre intentos; un 4xx (auth, not found, validacion) no.
        transient = response.status_code >= 500 or response.status_code == 429
        error = GithubUnavailable if transient else GithubError
        raise error(f"GitHub rechazo {action} ({response.status_code}): {detail}")

    def _get_json(self, path: str, *, params: Mapping[str, Any] | None = None) -> object:
        response = self._request("GET", path, params=params)
        self._check(response, f"GET {path}")
        try:
            return response.json()
        except ValueError as exc:
            raise GithubError(f"Respuesta ilegible de GitHub en {path}: {exc}") from exc

    def _paginate(
        self, path: str, *, params: Mapping[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Todas las paginas de una lista, con un tope de seguridad.

        `list_review_markers` no puede quedarse con la primera pagina: una marca
        antigua (o el resumen) puede estar mas alla de la 100 y entonces se
        re-publicaria la review.
        """
        items: list[dict[str, Any]] = []
        query: dict[str, Any] = {"per_page": _PER_PAGE, **(params or {})}
        for page in range(1, _MAX_PAGES + 1):
            data = self._get_json(path, params={**query, "page": page})
            if not isinstance(data, list):
                break
            items.extend(item for item in data if isinstance(item, dict))
            if len(data) < _PER_PAGE:
                break
        return items

    def _repo_path(self, suffix: str = "") -> str:
        return f"/repos/{self._owner}/{self._repo}{suffix}"

    def get_pull_request(self, repo: str, pr_id: int) -> PullRequestInfo:
        del repo
        item = self._get_json(self._repo_path(f"/pulls/{pr_id}"))
        if not isinstance(item, dict):
            raise GithubError(f"PR {pr_id} con formato inesperado")
        head = item.get("head") or {}
        base = item.get("base") or {}
        user = item.get("user") or {}
        return PullRequestInfo(
            repo=f"{self._owner}/{self._repo}",
            pr_id=pr_id,
            title=str(item.get("title", "")),
            description=str(item.get("body") or ""),
            source_branch=str((head or {}).get("ref", "")),
            destination_branch=str((base or {}).get("ref", "")),
            author=str((user or {}).get("login", "")),
            head_sha=str((head or {}).get("sha", "")),
            state=str(item.get("state", "open")).upper(),
            url=str(item.get("html_url", "")),
        )

    def get_diff(self, repo: str, pr_id: int) -> str:
        del repo
        cached = self._diff_cache.get(pr_id)
        if cached is not None:
            return cached
        response = self._request("GET", self._repo_path(f"/pulls/{pr_id}"), accept=_DIFF_ACCEPT)
        self._check(response, f"diff del PR {pr_id}")
        text = response.text
        self._diff_cache[pr_id] = text
        return text

    def get_diffstat(self, repo: str, pr_id: int) -> list[tuple[str, int, int]]:
        del repo
        result: list[tuple[str, int, int]] = []
        for entry in self._paginate(self._repo_path(f"/pulls/{pr_id}/files")):
            path = str(entry.get("filename", ""))
            if not path:
                continue
            result.append(
                (
                    path,
                    int(entry.get("additions", 0) or 0),
                    int(entry.get("deletions", 0) or 0),
                )
            )
        return result

    def get_file(self, repo: str, path: str, *, ref: str) -> str:
        del repo
        data = self._get_json(self._repo_path(f"/contents/{path}"), params={"ref": ref})
        if not isinstance(data, dict):
            raise GithubError(f"Contenido de {path}@{ref} con formato inesperado")
        content = data.get("content")
        if not isinstance(content, str):
            # Fichero binario, submodulo, symlink o directorio: no hay texto que
            # leer. El fix lo trata como "no se pudo leer el fichero".
            raise GithubError(f"{path}@{ref} no es un fichero de texto")
        try:
            # `content` viene en base64 con saltos de linea.
            return base64.b64decode(content).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise GithubError(f"Contenido de {path}@{ref} ilegible: {exc}") from exc

    def file_url(self, repo: str, path: str, *, ref: str, line: int) -> str | None:
        """Permalink de GitHub: `{web}/{owner}/{repo}/blob/{ref}/{path}#L{line}`.

        El host web se deriva de `GITHUB_API_URL` (GHES tiene otro dominio); en
        `api.github.com` se usa `github.com` para no exponer el subdominio de API.
        """
        if not path or not ref:
            return None
        web = self._api_url
        if web == _DEFAULT_API_URL:
            web = "https://github.com"
        elif web.endswith("/api/v3"):
            web = web[: -len("/api/v3")]
        return f"{web}/{self._owner}/{self._repo}/blob/{ref}/{path.lstrip('/')}#L{line}"

    def resolve_inline_anchor(
        self, repo: str, pr_id: int, *, path: str, text: str
    ) -> InlineAnchor | None:
        del repo
        if not path or not text.strip():
            return None
        files = parse_unified_diff(self.get_diff("", pr_id))
        file = files.get(path)
        if file is None:
            return None
        line = resolve_added_line(file.added, text)
        if line is None:
            return None
        return InlineAnchor(path=path, line=line)

    def post_inline_comment(
        self,
        repo: str,
        pr_id: int,
        *,
        path: str,
        line: int,
        body: str,
        from_line: int | None = None,
    ) -> None:
        del repo
        payload: dict[str, object] = {
            "body": body,
            "path": path,
            "line": line,
            "side": "RIGHT",
        }
        if from_line is not None and from_line < line:
            # Contrato de GitHub: un rango va en `start_line` (menor) + `line`
            # (mayor). Bitbucket lo expresa como `from_line`.
            payload["start_line"] = from_line
            payload["start_side"] = "RIGHT"
        response = self._request("POST", self._repo_path(f"/pulls/{pr_id}/comments"), json=payload)
        self._check(response, f"comentario inline en {path}:{line}")

    def post_comment(self, repo: str, pr_id: int, body: str) -> None:
        del repo
        response = self._request(
            "POST", self._repo_path(f"/issues/{pr_id}/comments"), json={"body": body}
        )
        self._check(response, f"comentario en el PR {pr_id}")

    def list_review_markers(self, repo: str, pr_id: int) -> list[ReviewMarker]:
        """Comentarios ya publicados en el PR: generales e inline.

        El resumen va a `/issues/{n}/comments` y los inline a
        `/pulls/{n}/comments`; el publish filtra por su token, asi que hay que
        mirar en los dos sitios. Sin paginar, una marca (o el resumen) mas alla
        de la primera pagina se perderia y la review se re-publicaria.
        """
        del repo
        markers: list[ReviewMarker] = []
        for path in (
            self._repo_path(f"/issues/{pr_id}/comments"),
            self._repo_path(f"/pulls/{pr_id}/comments"),
        ):
            for item in self._paginate(path):
                markers.append(
                    ReviewMarker(id=str(item.get("id", "")), body=str(item.get("body") or ""))
                )
        return markers

    def post_task(self, repo: str, pr_id: int, body: str) -> None:
        """GitHub no tiene "tasks" de PR.

        Se conserva como no-op explicito: un PR comentado ya deja el hallazgo
        accionable (`post_comment`), y devolver el hallazgo a la superficie de
        tareas requeriria escribir en un issue aparte. El grafo hoy no lo llama.
        """
        del repo, pr_id, body


def _error_detail(response: httpx.Response) -> str:
    """Mensaje compacto de la API; nunca el cuerpo entero en un log."""
    try:
        body = response.json()
    except ValueError:
        return response.text.strip()[:500] or "sin detalle"
    if isinstance(body, dict) and "message" in body:
        return str(body["message"])[:500]
    return str(body)[:500]
