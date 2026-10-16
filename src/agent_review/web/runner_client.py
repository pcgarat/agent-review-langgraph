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
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token.strip()
        self._timeout_s = timeout_s

    def available(self) -> bool:
        return bool(self._base_url)

    def stream(self, run_key: str) -> Iterator[str]:
        """Reenvía los eventos SSE del runner para `run_key`, tal cual.

        Solo emite el **cuerpo** de la respuesta (las líneas `event:`/`data:`): el
        panel vuelve a envolverlas en su propio `text/event-stream`. Un fallo de
        conexión se registra y cierra el stream; la consola es best-effort.
        """
        headers = {"Accept": "text/event-stream"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        url = f"{self._base_url}/verify/{run_key}/console"
        try:
            with httpx.stream("GET", url, headers=headers, timeout=self._timeout_s) as response:
                if response.status_code != 200:
                    logger.info(
                        "Consola de %s no disponible en el runner (%d)",
                        run_key,
                        response.status_code,
                    )
                    return
                yield from response.iter_lines()
        except httpx.HTTPError as exc:
            logger.info("Consola de %s: el runner no respondio (%s)", run_key, exc)


def build_runner_client(settings: object) -> RunnerConsoleClient:
    """Cliente del runner desde la configuración del panel.

    El runner solo publica el bearer compartido; el panel no recibe credenciales de
    VCS ni del LLM (spec §5.8), así que este cliente es lo único que necesita.
    """
    return RunnerConsoleClient(
        base_url=str(getattr(settings, "sandbox_runner_url", "") or ""),
        token=str(getattr(settings, "sandbox_runner_token", "") or ""),
    )
