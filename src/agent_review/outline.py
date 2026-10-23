"""Índice de símbolos de un fichero para `read_file(mode="outline")` (023 R1, E5).

Leer 1 179 líneas para localizar una función es el gasto que este índice evita: el
modelo recibe `inicio-fin  nombre` y pide después solo ese rango.

El contrato es deliberadamente estrecho: el índice **no sustituye** al cuerpo y
**no inventa** rangos exactos. Donde hay AST (Python) los rangos son exactos; donde
no, es una heurística declarada en la cabecera del propio índice. Un fichero que no
se puede indexar devuelve `None` y el adaptador lo convierte en `ToolError` con el
consejo de usar `start`/`end`: nunca se sirve el fichero entero como si fuera un
índice.
"""

from __future__ import annotations

import ast
import re

# Un fichero mayor que esto no se indexa: el rango deja de ser útil (el modelo no
# va a leer 4 MB) y el parseo ocupa memoria en el proceso que sirve la review.
MAX_OUTLINE_BYTES = 4_000_000

# Extensiones con sintaxis de declaraciones reconocible sin AST. Los rangos de esta
# via son aproximados (llaves o siguiente declaración) y la cabecera lo dice.
_HEURISTIC_EXTENSIONS = frozenset(
    {
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".kts",
        ".scala",
        ".rb",
        ".php",
        ".cs",
        ".swift",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".cxx",
        ".hpp",
        ".hh",
    }
)
_BRACE_EXTENSIONS = frozenset({".go", ".rs", ".java", ".kt", ".scala", ".cs", ".swift"}) | {
    ".c",
    ".h",
    ".cc",
    ".cpp",
    ".cxx",
    ".hpp",
    ".hh",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".php",
}

_DECLARATION = re.compile(
    r"^(?:export\s+)?(?:default\s+)?(?:pub\s+)?"
    r"(?:public|private|protected|internal|static|final|abstract|async|extern|constexpr)?\s*"
    r"(?:def|defp|class|function|func|fn|sub|impl|struct|enum|interface|trait|module|mod|type|"
    r"const|let|var|record|object)\s+([A-Za-z_$][\w$]*)"
)


def outline_text(path: str, text: str) -> str | None:
    """Índice de `text` como `inicio-fin  nombre` por línea, o `None` si no se puede.

    `path` decide el indexador por extensión; no se lee nada más que `text` (el
    adaptador ya tiene el contenido completo). Un fichero vacío es un índice vacío,
    no un error.
    """
    if len(text.encode("utf-8")) > MAX_OUTLINE_BYTES:
        return None
    suffix = _suffix(path)
    if suffix in (".py", ".pyi"):
        return _python_outline(text)
    if suffix in _HEURISTIC_EXTENSIONS:
        return _heuristic_outline(text, suffix)
    return None


def _suffix(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    dot = name.rfind(".")
    return name[dot:].lower() if dot > 0 else ""


def _python_outline(text: str) -> str:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        # Un fichero que no compila sigue teniendo declaraciones que localizar: se
        # degrada a la heuristica y la cabecera lo declara.
        return _heuristic_outline(text, ".py")
    lines = _python_symbols(tree)
    header = "# indice (AST): inicio-fin  simbolo"
    return "\n".join([header, *lines]) if lines else f"{header}\n(sin funciones ni clases)"


def _python_symbols(tree: ast.Module) -> list[str]:
    lines: list[str] = []

    def walk(node: ast.AST, depth: int) -> None:
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            start = min([child.lineno, *(d.lineno for d in child.decorator_list)])
            end = getattr(child, "end_lineno", None) or child.lineno
            indent = "  " * depth
            if isinstance(child, ast.ClassDef):
                lines.append(f"{start}-{end}  {indent}class {child.name}")
            else:
                kind = "async def" if isinstance(child, ast.AsyncFunctionDef) else "def"
                lines.append(f"{start}-{end}  {indent}{kind} {child.name}({_signature(child)})")
            walk(child, depth + 1)

    walk(tree, 0)
    return lines


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args = node.args
    parts = [arg.arg for arg in [*args.posonlyargs, *args.args]]
    if args.vararg is not None:
        parts.append(f"*{args.vararg.arg}")
    if args.kwonlyargs:
        if args.vararg is None:
            parts.append("*")
        parts.extend(arg.arg for arg in args.kwonlyargs)
    if args.kwarg is not None:
        parts.append(f"**{args.kwarg.arg}")
    return ", ".join(parts)


def _heuristic_outline(text: str, suffix: str) -> str:
    raw = text.splitlines()
    starts: list[tuple[int, str, int]] = []
    for number, line in enumerate(raw, start=1):
        match = _DECLARATION.match(line)
        if match is None:
            continue
        starts.append((number, match.group(1), len(line) - len(line.lstrip())))
    header = "# indice heuristico (sin AST): inicio-fin  simbolo; rangos aproximados"
    if not starts:
        return f"{header}\n(sin declaraciones reconocibles)"
    entries: list[str] = []
    for position, (start, name, indent) in enumerate(starts):
        next_start = starts[position + 1][0] if position + 1 < len(starts) else len(raw) + 1
        end = next_start - 1
        if suffix in _BRACE_EXTENSIONS:
            braced = _brace_end(raw, start - 1, next_start - 1)
            if braced is not None:
                end = braced
        entries.append(f"{start}-{end}  {'  ' * (indent // 2)}{name}")
    return "\n".join([header, *entries])


def _brace_end(raw: list[str], start: int, limit: int) -> int | None:
    """Última línea del bloque que abre en `start`, contando llaves; `None` si no cierra."""
    depth = 0
    opened = False
    for index in range(start, limit):
        for char in raw[index]:
            if char == "{":
                depth += 1
                opened = True
            elif char == "}":
                depth -= 1
        if opened and depth <= 0:
            return index + 1
    return None
