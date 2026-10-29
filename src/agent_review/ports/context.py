from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from agent_review.globs import matches_any

# Lado del diff al que se lee un fichero. `head` es el commit revisado; `base`, el
# destino del PR (para comparar lo que ya estaba antes del cambio).
Ref = Literal["head", "base"]

# Modos de las tres operaciones (023 E10): parámetros del puerto, nunca operaciones
# nuevas. `literal` y `full` son los defaults de 016; el resto los enciende el
# modelo o el flag, no la disponibilidad (E11).
GrepMode = Literal["literal", "regex"]
ReadMode = Literal["full", "outline"]

# Campos nuevos que un runner puede o no saber atender. El runner los anuncia al
# abrir el workspace y el adaptador **rechaza** una peticion que los use si no
# estan: un runner viejo que los ignorara en silencio haria creer al modelo que
# busco en regex y recibio literal (E0g, R3c).
CAP_GREP_PATH = "grep.path"
CAP_GREP_REF = "grep.ref"
CAP_GREP_MODE = "grep.mode"
CAP_GREP_FILES_ONLY = "grep.files_only"
CAP_READ_MODE = "read.mode"
CAP_DIR_GLOB = "dir.glob"
CAP_DIR_DEPTH = "dir.depth"
CAP_RESULT_META = "result.meta"

# Lo que sabe hacer el runner de este repositorio: los campos nuevos de 023 que
# implementa (C0b, G1, R1). Un campo que no este aqui se rechaza al pedirlo.
READER_CAPABILITIES: tuple[str, ...] = (
    CAP_GREP_PATH,
    CAP_GREP_REF,
    CAP_GREP_MODE,
    CAP_GREP_FILES_ONLY,
    CAP_READ_MODE,
    CAP_DIR_GLOB,
    CAP_DIR_DEPTH,
    CAP_RESULT_META,
)


class ToolError(Exception):
    """Peticion que el lector no puede servir, con un mensaje para el modelo.

    No es un fallo de infraestructura: es una peticion mal formada o no soportada
    por esta superficie, y se sirve como `ERROR` dentro del `<tool_result>` para que
    el modelo corrija. El bucle **no** la confunde con un bug del adaptador (C0b).
    """


# Rutas que un lector de contexto nunca sirve, ni por `read_file` ni en un `grep`.
# Es el *default*: el runner lo aplica sobre su `context_denylist` de perfil, de
# modo que el modelo no puede saltarselo pidiendo otra ruta (W7 de 016).
DEFAULT_CONTEXT_DENYLIST: tuple[str, ...] = (
    ".env*",
    "*.pem",
    "*.key",
    "**/secrets/**",
)


def is_denied(path: str, patterns: Sequence[str]) -> bool:
    """¿La ruta esta vetada? Casa por ruta completa o por nombre de fichero.

    Se comprueba tambien el basename para que un `config/.env` lo cace el patron
    `.env*`, que por si solo solo casaria el fichero en la raiz del arbol. Un patron
    `**/x/**` casa tambien `x/...` en la raiz: `**/` admite cero directorios.
    """
    normalized = _normalize(path)
    name = normalized.rsplit("/", 1)[-1]
    expanded = list(patterns) + [p[3:] for p in patterns if p.startswith("**/")]
    return matches_any(normalized, expanded) or matches_any(name, expanded)


def unsafe_repo_path(path: str) -> bool:
    """Ruta que no es relativa al repo: `..`, absoluta, o con query/fragmento.

    `..` y un `%2e%2e` decodificado salen del arbol. `?` y `#` cambian la URL
    cuando la ruta se interpola en una peticion HTTP.
    """
    from urllib.parse import unquote

    candidate = unquote(path or "")
    if not candidate or candidate.startswith(("/", "\\")):
        return True
    if "\\" in candidate or "\x00" in candidate or "?" in candidate or "#" in candidate:
        return True
    return any(segment == ".." for segment in candidate.split("/"))


def _normalize(path: str) -> str:
    """Quita los prefijos `./` y `/` de una ruta, nunca el punto de `.env`.

    `str.lstrip("./")` quitaria cada `.` y `/` inicial, de modo que `.env` pasaria
    a `env` y escaparia del veto `.env*`.
    """
    normalized = path
    while normalized.startswith(("./", "/")):
        normalized = normalized[2:] if normalized.startswith("./") else normalized[1:]
    return normalized


class ContextLimits(BaseModel):
    """Topes de un bucle de herramientas, por lote (**016** W2 / **014** H5).

    Un turno reenvía toda la conversación, así que el coste crece más rápido que el
    número de turnos: los topes son por lote y el bucle cierra con un turno final
    forzado al agotar cualquiera de ellos.
    """

    max_turns: int = Field(default=4, ge=1)
    max_calls: int = Field(default=8, ge=0)
    max_bytes: int = Field(default=60_000, ge=1)
    # Peticiones que se atienden en un mismo turno (023 F1/E14). Se sube junto con
    # `max_calls`: con 5 por turno y 8 totales el tope del lote se agota en dos
    # turnos y la palanca mide otra cosa.
    calls_per_turn: int = Field(default=3, ge=1)


@runtime_checkable
class ContextReaderPort(Protocol):
    """Lectura acotada del repositorio para un modelo que razona (016 W1).

    Un solo puerto con tres adaptadores (fixture, VCS y workspace del runner): el
    bucle de herramientas no sabe de dónde salen los bytes. Todas las operaciones
    son de **solo lectura** y ninguna ejecuta código del PR.

    Desde 023 C0 devuelve **value objects**, no `str`/`list` (ADR-0026): así el
    bucle puede anunciar el tamaño real, el corte y el rango servido sin mentir con
    `len(content)`. `total_bytes` cuenta bytes UTF-8 del fichero completo; si una
    superficie no lo conoce, se declara `None` y el consumidor lo dice, no lo
    inventa.

    Los modos (`mode`, `path`, `files_only`, `glob`, `depth`) son parámetros, no
    operaciones nuevas: el puerto conserva las tres de 016. Un adaptador que no
    puede servir un modo **lanza `ToolError`** con un mensaje accionable; nunca lo
    ignora ni degrada en silencio (E0g de 023).
    """

    def read_file(
        self,
        path: str,
        *,
        ref: Ref = "head",
        start: int | None = None,
        end: int | None = None,
        mode: ReadMode = "full",
    ) -> ReadResult | None:
        """Contenido del fichero (entero, o el rango `start..end`, 1-based inclusivo).

        `mode="outline"` sirve un índice de símbolos con sus rangos en vez del
        cuerpo. `None` si el fichero no existe, la ruta está vetada o no se puede
        leer.
        """
        ...

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
        """Coincidencias como `path:line: texto`, o solo las rutas si `files_only`.

        `literal` es el default: es más barato y predecible que una expresión regular
        y cierra el riesgo de un patrón caro (Q1 de 016). `glob` acota los ficheros,
        `path` acota a un subárbol y `ref` elige el lado (`context_vcs` mezclaba
        `head` y `base`).
        """
        ...

    def list_dir(
        self,
        path: str,
        *,
        glob: str | None = None,
        depth: int = 1,
    ) -> ToolResult:
        """Nombres del directorio `path`, ordenados; `depth` recursiona acotado.

        Un directorio **vacio** devuelve un `ToolResult` vacio; una ruta que no
        existe (o que es un fichero) lanza `ToolError`: el bucle no puede
        confundir «no hay nada» con «no esta» (L1). Un lector parcial solo puede
        hablar de la parte que ve.
        """
        ...


@dataclass(frozen=True)
class ReadResult:
    """Una lectura servida con lo necesario para no mentir sobre su tamaño.

    `content` es lo que se sirve (posiblemente recortado); `total_bytes` el tamaño
    del fichero **completo** en bytes UTF-8, antes de cualquier recorte (`None` si
    la superficie no lo conoce); `truncated` dice si el adaptador cortó; `served_range`
    es el rango de líneas 1-based inclusivo que se sirvió, si se pidió un rango.
    """

    content: str
    total_bytes: int | None = None
    truncated: bool = False
    served_range: tuple[int, int] | None = None


@dataclass(frozen=True)
class ToolResult:
    """Salida de `grep`/`list_dir`: los items servidos y cuántos había en total.

    `total` es el número de coincidencias o entradas **antes** del tope; `truncated`
    dice si se devolvió solo una parte. Si la superficie no puede contar el total,
    `total` es `None` («no medido»), nunca el tamaño de `items` por suposición.
    """

    items: tuple[str, ...] = ()
    total: int | None = None
    truncated: bool = False


# Qué se sirvió de un fichero (S1 de 023). `outline` es un índice de símbolos y
# `grep`, coincidencias: ninguno de los dos prueba una cita de cuerpo.
ReadingKind = Literal["full", "range", "outline", "grep"]


@dataclass(frozen=True)
class Reading:
    """Una lectura servida, con lo necesario para citarla (S1 de 023).

    `ranges` son tramos 1-based inclusivos del fichero; vacío en `full`, `outline`
    y `grep`. El registro las usa para decidir si una cita `path:line` está probada.
    """

    path: str
    ref: Ref = "head"
    kind: ReadingKind = "full"
    ranges: tuple[tuple[int, int], ...] = ()


class ReadingRegistry:
    """Lecturas de **una review**, compartidas entre lotes (S1 de 023).

    El bucle la recibe y anota cada operación servida. Vive en el `LlmReviewer`
    porque la instancia se reutiliza entre reviews y una cita a un fichero leído en
    el lote 1 sigue siendo válida en el lote 3; el revisor la vacía al empezar cada
    review para no validar citas de la anterior.

    Solo `full` y `range` de la **misma** `ref` prueban una cita de cuerpo: un
    índice (`outline`) o unas coincidencias (`grep`) no son el fichero, y leer
    `base` no prueba lo que dice `head`.
    """

    def __init__(self, readings: Sequence[Reading] = ()) -> None:
        self._readings: list[Reading] = list(readings)

    def record(self, reading: Reading) -> None:
        if reading.kind == "full" and any(
            other.kind == "full" and other.path == reading.path and other.ref == reading.ref
            for other in self._readings
        ):
            return
        if reading not in self._readings:
            self._readings.append(reading)

    def clear(self) -> None:
        self._readings.clear()

    @property
    def readings(self) -> tuple[Reading, ...]:
        return tuple(self._readings)

    def __len__(self) -> int:
        return len(self._readings)

    def __iter__(self) -> Iterator[Reading]:
        return iter(self._readings)

    def serves(self, path: str, *, ref: Ref = "head", line: int | None = None) -> bool:
        """¿Una lectura de cuerpo de `path@ref` cubre `line`? (009 V4, 023 S1)."""
        for reading in self._readings:
            if reading.path != path or reading.ref != ref:
                continue
            if reading.kind == "full":
                return True
            if reading.kind == "range" and (
                line is None or any(low <= line <= high for low, high in reading.ranges)
            ):
                return True
        return False

    def files_read(self) -> list[str]:
        """Rutas con **cuerpo** servido; `outline` y `grep` no cuentan como leídas."""
        return sorted({r.path for r in self._readings if r.kind in ("full", "range")})


def validate_range(start: int | None, end: int | None) -> None:
    """Valida un rango 1-based inclusivo; `ValueError` si no tiene sentido.

    Un `start` < 1, un `end` < 1 o un `end` < `start` son **error** del consumidor:
    devolver «sin coincidencias» o el fichero entero convertiria una peticion mal
    formada en una media verdad.
    """
    if start is not None and start < 1:
        raise ValueError(f"`start` debe ser >= 1 (recibido {start})")
    if end is not None and end < 1:
        raise ValueError(f"`end` debe ser >= 1 (recibido {end})")
    if start is not None and end is not None and end < start:
        raise ValueError(f"`end` ({end}) no puede ser menor que `start` ({start})")


def slice_lines(
    text: str, start: int | None, end: int | None
) -> tuple[str, tuple[int, int] | None]:
    """Rango `start..end` (1-based inclusivo) de un texto, validado.

    Sin rango devuelve el texto entero y `None`. Los limites se recortan al tamano
    del texto: pedir mas alla del final no es error, es servir lo que hay.
    """
    validate_range(start, end)
    if start is None and end is None:
        return text, None
    lines = text.splitlines()
    first = (start or 1) - 1
    last = min(end if end is not None else len(lines), len(lines))
    served = (start or 1, max(start or 1, last))
    return "\n".join(lines[first:last]), served


def truncate_bytes(text: str, max_bytes: int) -> tuple[str, bool]:
    """Recorta a `max_bytes` UTF-8 sin partir una línea; `(texto, truncado)`.

    El corte es en bytes, no en caracteres (un fichero con acentos ocupa más de lo
    que dice `len`). Si el corte cae a mitad de línea, se descarta la línea
    incompleta para que el modelo no lea una línea cortada como si fuera entera.
    """
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text, False
    cut = data[:max_bytes].decode("utf-8", errors="ignore")
    newline = cut.rfind("\n")
    if newline >= 0:
        cut = cut[:newline]
    return cut, True


# --- Dialecto portable de `grep` (023 G1, ADR-0027) ---------------------------------
#
# `git grep -E` y `re` no comparten dialecto: `[[:space:]]` en `re` es una clase con
# los caracteres `[ : s p a c e`, y `\b`/`\<` son extensiones GNU que `re` lee de
# otra forma. En vez de traducir a dos emisores, se acepta un subconjunto cuya
# semantica coincide y se emite **una** cadena canonica que ambos entienden.

# `\s` no se emite como `[ \t\n\r\f\v]`: dentro de una clase, GNU grep lee `\t` como
# la letra `t` (POSIX no reconoce escapes ahi) y una linea de `grep` nunca contiene
# `\n`. Van los caracteres de control **literales**, que los dos motores leen igual.
_WHITESPACE = " \t\r\f\v"
_CLASSES = {
    "d": "[0-9]",
    "D": "[^0-9]",
    "w": "[A-Za-z0-9_]",
    "W": "[^A-Za-z0-9_]",
    "s": f"[{_WHITESPACE}]",
    "S": f"[^{_WHITESPACE}]",
}

# Puntuacion que `\X` escapa igual en los dos motores: el caracter sigue siendo el
# mismo literal. Cualquier otro escape no es portable y se rechaza.
_PUNCTUATION = frozenset(".*+?|()[]{}^$\\/-")

# `\b`, `\<` y `\>` son fronteras de palabra: GNU las soporta, `re` no las lee igual.
_BOUNDARIES = {"b": "frontera de palabra", "<": "inicio de palabra", ">": "fin de palabra"}


def portable_pattern(pattern: str) -> str:
    """Cadena canonica valida a la vez en `re` y en `git grep -E` (ADR-0027).

    Expande `\\d`/`\\w`/`\\s` (y sus negaciones) y rechaza con `ToolError` lo que
    cada motor interpretaria distinto: clases POSIX, fronteras de palabra,
    *lookaround*, grupos sin captura, retro-referencias y escapes de letras no
    conocidos. Un patron invalido tambien es error, nunca «sin coincidencias».
    """
    out: list[str] = []
    in_class = False
    class_chars = 0
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "\\":
            nxt = pattern[index + 1] if index + 1 < len(pattern) else ""
            if not nxt:
                raise ToolError("patron regex invalido: termina en `\\`")
            if in_class:
                raise ToolError(
                    "no portable: `\\` dentro de una clase ([...]); "
                    "pon el caracter fuera de la clase"
                )
            if nxt in _CLASSES:
                out.append(_CLASSES[nxt])
            elif nxt in _BOUNDARIES:
                raise ToolError(
                    f"no portable: `\\{nxt}` ({_BOUNDARIES[nxt]}); "
                    "acota con `path`/`glob` o usa `[^A-Za-z0-9_]`"
                )
            elif nxt.isdigit():
                raise ToolError(
                    "no portable: retro-referencias (`\\1`); repite el grupo en el patron"
                )
            elif nxt in _PUNCTUATION:
                out.append(f"\\{nxt}")
            else:
                raise ToolError(
                    f"escape no portable `\\{nxt}`: usa literales, `[...]` o `\\d`/`\\w`/`\\s`"
                )
            index += 2
            continue
        if in_class:
            if char == "[":
                raise ToolError(
                    "no portable: `[` dentro de una clase ([...]); no hay clases anidadas"
                )
            if char == "]" and class_chars:
                in_class = False
            else:
                class_chars += 1
            out.append(char)
            index += 1
            continue
        if pattern.startswith("[[:", index):
            raise ToolError("no portable: clases POSIX `[[:...:]]`; usa `[0-9]`, `[A-Za-z]`, `[_]`")
        if pattern.startswith("(?", index):
            raise ToolError(
                "no portable: `(?...)` (lookaround, grupos sin captura o flags); usa `(...)`"
            )
        if char == "[":
            in_class = True
            class_chars = 0
        out.append(char)
        index += 1
    if in_class:
        raise ToolError("patron regex invalido: clase `[` sin cerrar")
    canonical = "".join(out)
    try:
        re.compile(canonical)
    except re.error as exc:
        raise ToolError(f"patron regex invalido: {exc}") from exc
    return canonical


def line_matcher(pattern: str, mode: GrepMode = "literal") -> Callable[[str], bool]:
    """Predicado «esta linea casa» en el dialecto del puerto.

    Lo comparten el lector fixture y el VCS: la semantica de `regex` no puede vivir
    en dos sitios o las superficies dejan de dar el mismo resultado (K1). El runner
    no la usa: delega en `git grep -E` con la **misma** cadena canonica.
    """
    if mode not in ("literal", "regex"):
        raise ToolError(f"`mode={mode!r}` no existe; usa `literal` o `regex`")
    if mode == "literal":
        return lambda line: pattern in line
    compiled = re.compile(portable_pattern(pattern))
    return lambda line: compiled.search(line) is not None
