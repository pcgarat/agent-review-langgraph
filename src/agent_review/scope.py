from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence

from agent_review.adapters.diff_parser import parse_unified_diff
from agent_review.domain.models import FileClass, ReviewContext, ReviewCoverage, SkipReason
from agent_review.globs import matches_any, path_matches
from agent_review.ports.vcs import VcsPort

logger = logging.getLogger(__name__)

_HEADING = re.compile(r"^(#{1,6})\s+(\S.*?)\s*$", re.MULTILINE)

# Ficheros que no aportan senal a una review: generados, lockfiles, vendor y
# artefactos de build. Filtrarlos antes de las personas evita gastar tokens (o
# inlines) en ruido que nadie leeria. Es un conjunto minimo y revisable; se puede
# ampliar por deploy con `SCOPE_IGNORE`.
DEFAULT_IGNORE_PATTERNS = (
    "composer.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "uv.lock",
    "vendor/**",
    "node_modules/**",
    "var/**",
    "public/build/**",
    "**/*.min.js",
    "**/*.min.css",
    "**/*.map",
    "**/*.generated.*",
)

# Clases de fichero por defecto (012 C4/C5, spec §5.4). `code` es el resto. Un
# patron `**/x` casa tambien `x` en la raiz (ver `_class_matches`). El perfil del
# repo las amplia con `scope.classes`; nunca las sustituye.
DEFAULT_FILE_CLASSES: dict[FileClass, tuple[str, ...]] = {
    FileClass.TEST: ("**/tests/**", "**/test_*.py", "**/*Test.php"),
    FileClass.DOCS: ("**/*.md", "**/docs/**"),
    FileClass.CONFIG: (
        "**/docker-compose*.yml",
        "**/docker-compose*.yaml",
        "**/Dockerfile*",
        "**/*.yaml",
        "**/*.yml",
        "**/*.toml",
        "**/.env.example",
        "**/settings.py",
        "**/config/**",
    ),
}
# Orden de comprobacion: `tests/config/a.yaml` es test, no configuracion.
_CLASS_ORDER = (FileClass.TEST, FileClass.DOCS, FileClass.CONFIG)

_DIFF_HEADER = "diff --git "


def merge_ignore_patterns(configured: str) -> list[str]:
    """Patrones de ignorado: los por defecto mas los declarados por deploy."""
    extra = [item.strip() for item in configured.split(",") if item.strip()]
    return [*DEFAULT_IGNORE_PATTERNS, *extra]


def is_ignored(path: str, patterns: list[str]) -> bool:
    return matches_any(path.removeprefix("./"), patterns)


def merge_file_classes(
    extra: Mapping[str, list[str]] | None = None,
) -> dict[FileClass, tuple[str, ...]]:
    """Clases por defecto mas las del perfil (`scope.classes`), sumadas."""
    merged = dict(DEFAULT_FILE_CLASSES)
    for name, patterns in (extra or {}).items():
        file_class = FileClass(name)
        merged[file_class] = (*merged.get(file_class, ()), *patterns)
    return merged


def classify(path: str, classes: Mapping[FileClass, tuple[str, ...]] | None = None) -> FileClass:
    """Clase del fichero: test, docs, config o, si no casa ninguna, code (012 C4)."""
    table = DEFAULT_FILE_CLASSES if classes is None else classes
    clean = path.removeprefix("./")
    for file_class in _CLASS_ORDER:
        if any(_class_matches(clean, pattern) for pattern in table.get(file_class, ())):
            return file_class
    return FileClass.CODE


def _class_matches(path: str, pattern: str) -> bool:
    # `**/x` en `path_matches` exige un directorio delante; para clasificar, un
    # `docker-compose.yml` o un `config/` en la raiz cuentan igual.
    if path_matches(path, pattern):
        return True
    return pattern.startswith("**/") and path_matches(path, pattern[3:])


# Prioridad de revision por clase (012 C4). La configuracion va antes que el codigo:
# es pequena y es donde se rompe produccion (en el PR #3, dos de los tres P1 vivian
# en `docker-compose.yml` y `settings.py`); con el presupuesto justo, lo que se
# queda fuera debe ser lo grande y lo de menos riesgo.
_CLASS_PRIORITY = {FileClass.CONFIG: 0, FileClass.CODE: 1, FileClass.TEST: 2, FileClass.DOCS: 3}


def split_diff(diff: str) -> list[tuple[str, str]]:
    """Trocea el diff unificado en `(ruta, bloque)` por fichero, en su orden."""
    blocks: list[tuple[str, str]] = []
    path: str | None = None
    current: list[str] = []
    for line in diff.splitlines(keepends=True):
        if line.startswith(_DIFF_HEADER):
            if path is not None:
                blocks.append((path, "".join(current)))
            path = _block_path(line)
            current = [line]
        elif path is not None:
            current.append(line)
    if path is not None:
        blocks.append((path, "".join(current)))
    return blocks


def filter_diff(diff: str, patterns: list[str]) -> str:
    """El diff sin los ficheros ignorados."""
    return "".join(block for path, block in split_diff(diff) if not is_ignored(path, patterns))


def churn(block: str) -> int:
    """Lineas anadidas mas quitadas de un bloque del diff (sin las cabeceras)."""
    return sum(
        1 for line in block.splitlines() if line[:1] in "+-" and not line.startswith(("+++", "---"))
    )


def prioritize_diff(
    diff: str,
    ignore: list[str],
    classes: Mapping[FileClass, tuple[str, ...]] | None = None,
) -> tuple[str, ReviewCoverage]:
    """Quita el ruido, ordena por prioridad y crea la cobertura (012 C1, C4).

    No recorta: todo lo que no es ruido llega a las personas, ordenado por clase
    (configuracion, codigo, tests, docs) y, dentro de cada clase, por churn. Quien
    revisa por lotes corta por presupuesto y lo declara; aqui solo se declaran los
    ignorados.
    """
    kept: list[tuple[str, str, FileClass]] = []
    skipped: dict[str, SkipReason] = {}
    file_class: dict[str, FileClass] = {}
    blocks = split_diff(diff)
    for path, block in blocks:
        file_class[path] = classify(path, classes)
        if is_ignored(path, ignore):
            skipped[path] = SkipReason.IGNORED
        else:
            kept.append((path, block, file_class[path]))
    kept.sort(key=lambda item: (_CLASS_PRIORITY[item[2]], -churn(item[1]), item[0]))
    coverage = ReviewCoverage(files_total=len(blocks), skipped=skipped, file_class=file_class)
    return "".join(block for _path, block, _cls in kept), coverage


def changed_files(diff: str) -> list[str]:
    """Rutas del lado nuevo que aparecen en el diff (deduplicadas, en orden)."""
    seen: dict[str, None] = {}
    for line in diff.splitlines():
        if not line.startswith(_DIFF_HEADER):
            continue
        path = _block_path(line)
        if path is not None:
            seen.setdefault(path, None)
    return list(seen)


def assemble_review_context(
    vcs: VcsPort,
    *,
    repo: str,
    head_sha: str,
    base_sha: str = "",
    diff: str,
    doc_paths: Sequence[str] = (),
) -> ReviewContext:
    """Lee ficheros del diff y docs del perfil una vez (008 CX6 / D4 / R3).

    Cache por `(path, ref)` dentro de la llamada. Un `get_file` fallido omite ese
    fichero y no tumba la review.
    """
    cache: dict[tuple[str, str], str | None] = {}

    def read(path: str, ref: str) -> str | None:
        if not path or not ref:
            return None
        key = (path, ref)
        if key not in cache:
            try:
                cache[key] = vcs.get_file(repo, path, ref=ref)
            except Exception:
                logger.warning(
                    "ReviewContext: no se pudo leer %s@%s; se omite",
                    path,
                    ref,
                    exc_info=True,
                )
                cache[key] = None
        return cache[key]

    contents: dict[str, str] = {}
    for path in parse_unified_diff(diff):
        text = read(path, head_sha)
        if text is not None:
            contents[path] = text

    docs: dict[str, str] = {}
    for raw in doc_paths:
        path, _, fragment = raw.partition("#")
        path = path.strip()
        fragment = fragment.strip()
        key = f"{path}#{fragment}" if fragment else path
        if not path or key in docs:
            continue
        text = read(path, head_sha)
        if text is None:
            continue
        if fragment:
            sliced = slice_markdown_section(text, fragment)
            if sliced is None:
                logger.warning(
                    "ReviewContext: sin seccion %r en %s; se usa el fichero entero",
                    fragment,
                    path,
                )
                sliced = text
            docs[key] = sliced
        else:
            docs[key] = text

    return ReviewContext(
        head_sha=head_sha,
        base_sha=base_sha,
        contents=contents,
        docs=docs,
    )


def slice_markdown_section(text: str, fragment: str) -> str | None:
    """Recorta un markdown desde el encabezado que empieza por `fragment`.

    `fragment` es lo que sigue a `#` en `context_docs.extra` (p. ej. `2` →
    `## 2. Configuración`). Corta hasta el siguiente encabezado del mismo nivel
    o superior.
    """
    needle = fragment.strip()
    if not needle:
        return None
    matches = list(_HEADING.finditer(text))
    start_at: int | None = None
    level = 0
    for match in matches:
        title = match.group(2).strip()
        if title == needle or title.startswith(f"{needle}.") or title.startswith(f"{needle} "):
            start_at = match.start()
            level = len(match.group(1))
            break
    if start_at is None:
        return None
    end_at = len(text)
    for match in matches:
        if match.start() <= start_at:
            continue
        if len(match.group(1)) <= level:
            end_at = match.start()
            break
    return text[start_at:end_at].rstrip() + "\n"


def _block_path(header_line: str) -> str | None:
    parsed = parse_unified_diff(header_line + "\n")
    return next(iter(parsed), None)
