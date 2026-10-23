"""Cliente HTTP del worker hacia el servicio `claude-runner` (025 R3).

El worker no ejecuta el CLI: pide stdout y valida. Los códigos distinguen
tiempo agotado, cupo y proceso caído.
"""

from __future__ import annotations

import httpx

from agent_review.ports.claude import (
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

    def run(self, *, repo: str, pr_id: int, head_sha: str) -> str:
        headers: dict[str, str] = {}
        if self._token and not is_missing(self._token):
            headers["Authorization"] = f"Bearer {self._token}"
        own = self._client is None
        client = self._client or httpx.Client()
        try:
            try:
                response = client.post(
                    f"{self._url}/run",
                    json={"repo": repo, "pr_id": pr_id, "head_sha": head_sha},
                    headers=headers,
                    timeout=self._timeout + 30,
                )
            except httpx.TimeoutException as exc:
                raise ClaudeTimeout("el servicio claude-runner no respondio") from exc
            except httpx.RequestError as exc:
                raise ClaudeProcessCrashed("el servicio claude-runner no esta alcanzable") from exc
            if response.status_code == 429:
                raise ClaudeQuotaExceeded("cupo de claude agotado")
            if response.status_code in {408, 504}:
                raise ClaudeTimeout("claude excedio el tiempo de pared")
            if response.status_code != 200:
                raise ClaudeProcessCrashed(f"claude-runner respondio {response.status_code}")
            payload = response.json()
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
            payload = response.json()
            return bool(payload.get("active")) if isinstance(payload, dict) else False
        finally:
            if own:
                client.close()
