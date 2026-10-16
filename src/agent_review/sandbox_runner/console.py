from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from agent_review.errors import PermanentError

# Tope de lineas por verificacion: la consola es best-effort, no un archivo (019 W8).
DEFAULT_MAX_LINES = 2000
# Verificaciones terminadas que se retienen para servir su consola a quien llega
# tarde. Un reinicio del runner las pierde todas (efimero, como ADR-0018).
DEFAULT_MAX_RUNS = 8
# Cadencia con la que un suscriptor sin lineas nuevas vuelve a comprobar el buffer.
# Una linea o el cierre lo despiertan al instante, asi que este valor solo acota la
# espera maxima cuando la verificacion sigue muda.
DEFAULT_POLL_S = 1.0


class ConsoleNotFound(PermanentError):
    """No hay consola para ese `run_key` en la memoria del proceso (`404`)."""


@dataclass(frozen=True)
class ConsoleLine:
    seq: int
    stream: str  # "stdout" | "stderr"
    text: str


@dataclass
class _Run:
    """Estado de una verificacion. Todo el estado mutable lo guarda su `cond`."""

    lines: deque[ConsoleLine]
    cond: threading.Condition = field(default_factory=threading.Condition)
    closed: bool = False
    subscribers: int = 0
    next_seq: int = 1


class ConsoleBuffer:
    """Buffer circular en memoria del runner, indexado por `run_key` (019 W5).

    Acumula las lineas que emite `ContainerRuntime.start` y despierta a los
    suscriptores vivos. Es **efimero**: no se persiste, no viaja al `VerifyReport`
    y un reinicio del runner lo pierde (la consola es best-effort, W8).
    """

    def __init__(
        self,
        *,
        max_lines: int = DEFAULT_MAX_LINES,
        max_runs: int = DEFAULT_MAX_RUNS,
        poll_s: float = DEFAULT_POLL_S,
    ) -> None:
        self._max_lines = max(1, max_lines)
        self._max_runs = max(1, max_runs)
        self._poll_s = poll_s
        self._runs: dict[str, _Run] = {}
        self._lock = threading.Lock()

    def open(self, run_key: str) -> None:
        """Da de alta una verificacion; reinicia su consola si ya existia."""
        with self._lock:
            self._runs.pop(run_key, None)
            self._evict_locked()
            self._runs[run_key] = _Run(lines=deque(maxlen=self._max_lines))

    def append(self, run_key: str, stream: str, text: str) -> None:
        """Anade una linea y despierta a los suscriptores.

        Un `run_key` desalojado se ignora: perder la consola es aceptable, tumbar la
        verificacion que sigue emitiendo no (W8).
        """
        run = self._get_or_none(run_key)
        if run is None:
            return
        with run.cond:
            run.lines.append(ConsoleLine(seq=run.next_seq, stream=stream, text=text))
            run.next_seq += 1
            run.cond.notify_all()

    def close(self, run_key: str) -> None:
        """Marca la verificacion como terminada; la consola queda servible."""
        run = self._get_or_none(run_key)
        if run is None:
            return
        with run.cond:
            run.closed = True
            run.cond.notify_all()

    def has(self, run_key: str) -> bool:
        with self._lock:
            return run_key in self._runs

    def subscribe(self, run_key: str) -> bool:
        """Alta de un suscriptor vivo. `False` si el run no existe (desalojado o ausente)."""
        run = self._get_or_none(run_key)
        if run is None:
            return False
        with run.cond:
            run.subscribers += 1
        return True

    def unsubscribe(self, run_key: str) -> None:
        run = self._get_or_none(run_key)
        if run is None:
            return
        with run.cond:
            run.subscribers = max(0, run.subscribers - 1)

    def wait_for_lines(
        self, run_key: str, after_seq: int, *, timeout_s: float | None = None
    ) -> tuple[list[ConsoleLine], bool]:
        """Lineas con `seq > after_seq` y si el run ya termino.

        Si no hay nada nuevo y el run sigue abierto, espera cambios **bajo el mismo
        lock** que `append`/`close`: esperar fuera perderia el aviso de una linea que
        llegase entre la lectura y la espera.
        """
        run = self._get(run_key)
        with run.cond:
            pending = _after(run, after_seq)
            if pending or run.closed:
                return pending, run.closed
            run.cond.wait(self._poll_s if timeout_s is None else timeout_s)
            return _after(run, after_seq), run.closed

    def line_sink(self, run_key: str) -> Callable[[str, str], None]:
        """`on_line` ligado a un `run_key`, para el `ContainerRuntime`."""
        return lambda stream, text: self.append(run_key, stream, text)

    def _get(self, run_key: str) -> _Run:
        run = self._get_or_none(run_key)
        if run is None:
            raise ConsoleNotFound(f"no hay consola para {run_key}")
        return run

    def _get_or_none(self, run_key: str) -> _Run | None:
        with self._lock:
            return self._runs.get(run_key)

    def _evict_locked(self) -> None:
        """Desaloja la verificacion terminada mas antigua si se supera el tope.

        Lee `closed` y `subscribers` **sin** su `cond` a proposito: tomar el lock de
        la run invertiria el orden que siguen `append`/`subscribe`. Una lectura
        obsoleta solo retrasa un desalojo, que es inofensivo.
        """
        while len(self._runs) >= self._max_runs:
            victim = next(
                (key for key, run in self._runs.items() if run.closed and run.subscribers == 0),
                None,
            )
            if victim is None:
                return  # todo abierto o con lector vivo: no se tira nada
            del self._runs[victim]


def _after(run: _Run, after_seq: int) -> list[ConsoleLine]:
    return [line for line in run.lines if line.seq > after_seq]


__all__ = [
    "ConsoleBuffer",
    "ConsoleLine",
    "ConsoleNotFound",
    "DEFAULT_MAX_LINES",
    "DEFAULT_MAX_RUNS",
    "DEFAULT_POLL_S",
]
