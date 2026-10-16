"""Consola del sandbox vista desde el panel (019 OP24).

El panel no habla con el buffer del runner directamente: reenvía su SSE
(`GET /verify/{run_key}/console`) al navegador con el bearer compartido, que el
navegador no tiene ni debe tener. Es un *proxy* de solo lectura: si el runner no
responde o el `run_key` ya no está en su buffer efímero, el panel degrada a `404`
sin tumbar nada (W8).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import httpx

logger = logging.getLogger(__name__)

# Tiempo de lectura amplio: la consola puede estar muda mientras el comando del PR
# corre (una imagen tardía, un `git apply` lento) y el SSE no debe cortarse por eso.
_STREAM_TIMEOUT_S = 1800.0


class RunnerConsoleClient:
    """Puerto mínimo para pedir la consola de una verificación al runner."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str = "",
        timeout_s: float = _STREAM_TIMEOUT_S,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token.strip()
        self._timeout_s = timeout_s
        # Solo los tests lo inyectan (`httpx.MockTransport`); en produccion, red real.
        self._transport = transport

    def available(self) -> bool:
        return bool(self._base_url)

    def open(self, run_key: str) -> Iterator[str] | None:
        """Abre la consola de `run_key` en el runner; `None` si no está disponible.

        La petición y su código se resuelven **antes** de devolver nada, para que el
        endpoint pueda responder `404` en vez de un `200` vacío: un `EventSource`
        que recibe un `200` sin eventos reconecta sin fin, y con un `404` se rinde.
        El iterador devuelto emite solo el **cuerpo** de la respuesta (las líneas
        `event:`/`data:`), que el panel vuelve a envolver en su propio
        `text/event-stream`, y cierra la conexión al agotarse o al cortarse.
        """
        headers = {"Accept": "text/event-stream"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        url = f"{self._base_url}/verify/{run_key}/console"
        client = httpx.Client(timeout=self._timeout_s, transport=self._transport)
        try:
            response = client.send(client.build_request("GET", url, headers=headers), stream=True)
        except httpx.HTTPError as exc:
            client.close()
            logger.info("Consola de %s: el runner no respondio (%s)", run_key, exc)
            return None
        if response.status_code != 200:
            response.close()
            client.close()
            logger.info(
                "Consola de %s no disponible en el runner (%d)", run_key, response.status_code
            )
            return None
        return _relay(run_key, client, response)


def _relay(run_key: str, client: httpx.Client, response: httpx.Response) -> Iterator[str]:
    """Reenvía las líneas del SSE del runner y libera la conexión al terminar."""
    try:
        yield from response.iter_lines()
    except httpx.HTTPError as exc:
        logger.info("Consola de %s: el runner corto el stream (%s)", run_key, exc)
    finally:
        response.close()
        client.close()


def build_runner_client(settings: object) -> RunnerConsoleClient:
    """Cliente del runner desde la configuración del panel.

    El runner solo publica el bearer compartido; el panel no recibe credenciales de
    VCS ni del LLM (spec §5.8), así que este cliente es lo único que necesita.
    """
    return RunnerConsoleClient(
        base_url=str(getattr(settings, "sandbox_runner_url", "") or ""),
        token=str(getattr(settings, "sandbox_runner_token", "") or ""),
    )
