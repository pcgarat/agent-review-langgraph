"""Cliente HTTP del worker hacia el servicio `claude-runner` (025 R3).

El worker no ejecuta el CLI: pide stdout y valida. Los códigos distinguen
tiempo agotado, cupo y proceso caído.
"""

from __future__ import annotations

import httpx

from agent_review.ports.claude import (
    ClaudeCeiling,
    ClaudeProcessCrashed,
    ClaudeQuotaExceeded,
    ClaudeTimeout,
)
from agent_review.settings import is_missing


class HttpClaudeRunner:
    """`POST /run` del servicio. El cuerpo de respuesta es stdout sin validar."""

    def __init__(
        self,
        *,
        url: str,
        token: str,
        timeout_s: float,
        client: httpx.Client | None = None,
    ) -> None:
        self._url = url.rstrip("/")
        self._token = token.strip()
        self._timeout = timeout_s
        self._client = client

    def run(
        self,
        *,
        repo: str,
        pr_id: int,
        head_sha: str,
        base_sha: str,
        max_turns: int | None = None,
        timeout_s: float | None = None,
    ) -> str:
        headers: dict[str, str] = {}
        if self._token and not is_missing(self._token):
            headers["Authorization"] = f"Bearer {self._token}"
        own = self._client is None
        client = self._client or httpx.Client()
        body: dict[str, object] = {
            "repo": repo,
            "pr_id": pr_id,
            "head_sha": head_sha,
            "base_sha": base_sha,
        }
        # Solo lo publicado. Omitirlo deja al runner con su entorno.
        if max_turns is not None:
            body["max_turns"] = max_turns
        if timeout_s is not None:
            body["timeout_s"] = timeout_s
        wait = self._timeout if timeout_s is None else timeout_s
        try:
            try:
                response = client.post(
                    f"{self._url}/run",
                    json=body,
                    headers=headers,
                    timeout=wait + 30,
                )
            except httpx.TimeoutException as exc:
                raise ClaudeTimeout("el servicio claude-runner no respondio") from exc
            except httpx.RequestError as exc:
                raise ClaudeProcessCrashed("el servicio claude-runner no esta alcanzable") from exc
            if response.status_code == 429:
                raise ClaudeQuotaExceeded(_with_detail("cupo de claude agotado", response))
            if response.status_code == 504:
                raise ClaudeCeiling(_with_detail("claude llego al techo", response))
            if response.status_code == 408:
                raise ClaudeTimeout(_with_detail("claude excedio el tiempo de pared", response))
            if response.status_code != 200:
                raise ClaudeProcessCrashed(
                    _with_detail(f"claude-runner respondio {response.status_code}", response)
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise ClaudeProcessCrashed("el servicio no devolvio JSON") from exc
            stdout = payload.get("stdout") if isinstance(payload, dict) else None
            if not isinstance(stdout, str):
                raise ClaudeProcessCrashed("el servicio no devolvio stdout")
            return stdout
        finally:
            if own:
                client.close()

    def active(self) -> bool:
        """`GET /session`: sí o no. Un fallo de red cuenta como no."""
        headers: dict[str, str] = {}
        if self._token and not is_missing(self._token):
            headers["Authorization"] = f"Bearer {self._token}"
        own = self._client is None
        client = self._client or httpx.Client()
        try:
            try:
                response = client.get(f"{self._url}/session", headers=headers, timeout=5)
            except httpx.HTTPError:
                return False
            if response.status_code != 200:
                return False
            try:
                payload = response.json()
            except ValueError:
                return False
            return bool(payload.get("active")) if isinstance(payload, dict) else False
        finally:
            if own:
                client.close()


def _with_detail(message: str, response: httpx.Response) -> str:
    """Añade el `detail` del runner. Sin esto un 502 solo dice el código."""
    detail = _response_detail(response)
    if not detail:
        return message
    return f"{message}: {detail}"


def _response_detail(response: httpx.Response) -> str:
    raw: object = None
    try:
        raw = response.json()
    except ValueError:
        raw = None
    if isinstance(raw, dict):
        detail = raw.get("detail")
        if isinstance(detail, str):
            text = detail
        elif detail is None:
            text = ""
        else:
            text = str(detail)
    elif isinstance(raw, str):
        text = raw
    else:
        text = response.text
    return text.replace("\x00", "").strip()[-500:]
