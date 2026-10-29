"""Valida la estructura de la documentacion (regla `.agents/rules/documentacion.md`).

Comprueba ubicacion y nombre de cada Markdown, la cabecera de fechas, el formato de
ADR y specs, que cada iniciativa este en el indice, que los enlaces relativos
resuelvan y que las reglas esten enlazadas en `.cursor/rules` y `.claude/rules`.

Uso: `python scripts/check_docs.py` (lo ejecuta `make docs-check`). Sale con 1 y la
lista de problemas si algo no cumple.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SLUG = r"[a-z0-9]+(?:-[a-z0-9]+)*"
HEADER = re.compile(r"^Última modificación: \d{4}-\d{2}-\d{2} \| Creado: \d{4}-\d{2}-\d{2}$")

# Ubicaciones permitidas para Markdown (ruta relativa a la raiz, con `/`).
ALLOWED = [
    re.compile(r"^README\.md$"),
    re.compile(r"^CONTEXT\.md$"),
    re.compile(rf"^docs/(README|{SLUG})\.md$"),
    re.compile(rf"^docs/adr/(README|\d{{4}}-{SLUG})\.md$"),
    re.compile(r"^docs/iniciativas/README\.md$"),
    re.compile(rf"^docs/iniciativas/(archivo/)?\d{{3}}-{SLUG}/(spec|plan|todo)\.md$"),
    re.compile(rf"^docs/edu/(README|\d{{2}}-{SLUG})\.md$"),
    re.compile(rf"^\.agents/rules/{SLUG}\.md$"),
    re.compile(r"^\.agents/skills/.+\.md$"),
    re.compile(r"^image/claude/skills/.+\.md$"),
    re.compile(rf"^\.cursor/rules/{SLUG}\.mdc$"),
    re.compile(rf"^\.claude/rules/{SLUG}\.md$"),
    re.compile(rf"^contracts/(README|{SLUG})\.md$"),
    re.compile(r"^bin/README\.md$"),
]
FORBIDDEN_DIRS = ("tasks/", "trash/", "notas/", "old/", "tmp/")
# Fecha en el nombre (20261011, 2026-10-11): la fecha va en la cabecera, no en la ruta.
DATE_IN_NAME = re.compile(r"\d{8}|\d{4}-\d{2}-\d{2}")

LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
FENCE = re.compile(r"^(```|~~~).*?^\1", re.M | re.S)


def _vendor_skill(path: str) -> bool:
    """La skill copiada en la imagen no es documentación del repo."""
    return path.startswith(".agents/skills/") or path.startswith("image/claude/skills/")


def tracked_markdown() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return sorted({p for p in out if p.endswith((".md", ".mdc")) and (ROOT / p).exists()})


def needs_header(path: str) -> bool:
    if path == "CONTEXT.md":
        return True
    return path.startswith("docs/") and not path.startswith("docs/adr/")


def check_location(path: str, problems: list[str]) -> None:
    if path.startswith(FORBIDDEN_DIRS):
        problems.append(f"{path}: carpeta prohibida ({path.split('/')[0]}/)")
        return
    if not _vendor_skill(path) and DATE_IN_NAME.search(Path(path).name):
        problems.append(f"{path}: el nombre no lleva fecha (va en la cabecera)")
    if not any(pattern.match(path) for pattern in ALLOWED):
        problems.append(f"{path}: ubicacion o nombre fuera de la estructura")


def check_header(path: str, text: str, problems: list[str]) -> None:
    first = text.splitlines()[0] if text else ""
    if not HEADER.match(first):
        problems.append(
            f"{path}: la primera linea debe ser "
            "'Última modificación: YYYY-MM-DD | Creado: YYYY-MM-DD'"
        )


def check_adr(path: str, text: str, problems: list[str]) -> None:
    name = Path(path).name
    if name == "README.md":
        return
    number = name[:4]
    if not re.search(rf"^# ADR-{number} — ", text, re.M):
        problems.append(f"{path}: el titulo debe ser '# ADR-{number} — …'")
    if "- **Estado:**" not in text:
        problems.append(f"{path}: falta la linea '- **Estado:** …'")


def check_spec(path: str, text: str, problems: list[str]) -> None:
    if not re.search(r"^> \*\*Estado:\*\* ", text, re.M):
        problems.append(f"{path}: falta la linea '> **Estado:** …' bajo el titulo")


def check_links(path: str, text: str, problems: list[str]) -> None:
    base = (ROOT / path).parent
    for target in LINK.findall(FENCE.sub("", text)):
        if re.match(r"^[a-z]+:", target) or target.startswith("#"):
            continue
        relative = target.split("#", 1)[0]
        if relative and not (base / relative).exists():
            problems.append(f"{path}: enlace roto -> {target}")


def check_initiatives(problems: list[str]) -> None:
    root = ROOT / "docs/iniciativas"
    index = (root / "README.md").read_text(encoding="utf-8") if root.exists() else ""
    numbers: dict[str, str] = {}
    for folder in sorted([*root.glob("[0-9]*"), *root.glob("archivo/[0-9]*")]):
        if not folder.is_dir():
            continue
        rel = folder.relative_to(ROOT).as_posix()
        number = folder.name[:3]
        if number in numbers:
            problems.append(f"{rel}: numero {number} repetido con {numbers[number]}")
        numbers[number] = rel
        if not (folder / "spec.md").exists():
            problems.append(f"{rel}: falta spec.md")
        if "archivo" not in folder.parts and f"{folder.name}/" not in index:
            problems.append(f"{rel}: no aparece en docs/iniciativas/README.md")


def check_adr_numbers(problems: list[str]) -> None:
    seen: dict[str, str] = {}
    for adr in sorted((ROOT / "docs/adr").glob("[0-9]*.md")):
        number = adr.name[:4]
        if number in seen:
            problems.append(f"docs/adr/{adr.name}: numero {number} repetido con {seen[number]}")
        seen[number] = adr.name


def check_rule_links(problems: list[str]) -> None:
    for rule in sorted((ROOT / ".agents/rules").glob("*.md")):
        for link in (
            ROOT / ".cursor/rules" / f"{rule.stem}.mdc",
            ROOT / ".claude/rules" / f"{rule.stem}.md",
        ):
            rel = link.relative_to(ROOT).as_posix()
            if not link.is_symlink():
                problems.append(f"{rel}: debe ser un enlace simbolico a .agents/rules/{rule.name}")
            elif link.resolve() != rule.resolve():
                problems.append(f"{rel}: apunta a {link.resolve()}, no a {rule}")


def main() -> int:
    problems: list[str] = []
    for path in tracked_markdown():
        check_location(path, problems)
        if (ROOT / path).is_symlink():
            continue
        text = (ROOT / path).read_text(encoding="utf-8")
        if needs_header(path):
            check_header(path, text, problems)
        if path.startswith("docs/adr/"):
            check_adr(path, text, problems)
        if path.endswith("/spec.md"):
            check_spec(path, text, problems)
        if not _vendor_skill(path):
            check_links(path, text, problems)
    check_initiatives(problems)
    check_adr_numbers(problems)
    check_rule_links(problems)

    if problems:
        print("docs-check: problemas encontrados\n")
        for problem in problems:
            print(f"  - {problem}")
        print(f"\n{len(problems)} problema(s). Ver .agents/rules/documentacion.md")
        return 1
    print("docs-check: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
