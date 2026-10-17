Última modificación: 2026-10-17 | Creado: 2026-10-17

# Todo: Gate de severidad por política

Checklist accionable. El diseño está en [`spec.md`](spec.md); el orden, en
[`plan.md`](plan.md). Spec aprobada el 2026-10-17.

## Bloque 1 — Motor (checkpoint C1)

- [x] **G1** Gate en `PolicyEngine` y `PolicyResult`
  - Validar `severity_gate` al construir; ampliar `PolicyResult` con `severity`,
    `severity_reason` y `publication` (`full`/`summary`/`drop`).
  - `decide`: refuted → `on_refuted`; unverified en umbral → degrada + `report`;
    luego reglas sobre severidad efectiva y techo.
  - Aceptación: P1 unverified → P3/`report`; P1 verified_static intacto; refuted+drop
    → `publication=drop`; P3 unverified indiferente; YAML mal formado falla al cargar.
  - Verifica: `tests/test_policy.py`.

## Bloque 2 — Decisión y merge (checkpoint C2)

- [x] **G2** `Decision` y nodo `policy`
  - Campos `severity`, `severity_reason`, `publication` en `Decision`;
    `apply_policy` los rellena; `build_approval` usa la severidad efectiva.
  - Verifica: `tests/test_graph.py` / helpers de fix.

- [x] **G3** Merge conserva el veredicto más fuerte (§5.3)
  - Escalera `executed > grounded > static > refuted > unverified` en
    `_merge_findings`.
  - Verifica: `tests/test_graph.py`.

## Bloque 3 — Publicación y cierre (checkpoint C3)

- [x] **G4** `publish` + render (G13)
  - Saltar `drop`; forzar solo-resumen en `summary`; severidad efectiva en
    cabeceras; línea de verificación / etiqueta de degradado según G13.
  - Verifica: `tests/test_graph.py`, `tests/test_fix_render.py`.

- [x] **G5** ADR-0024, docs vivos y medida
  - ADR-0024; `CONTEXT.md` («severidad efectiva»); `arquitectura.md` §3;
    índice de iniciativas; comentario de `autonomy.yaml`. Criterio 8 (dominancia
    006): el gate actúa en *replay* sin tokens; la regrabación del *gold set*
    (CX11 de 008) sigue aparte.
  - Verifica: `make docs-check`, `make ci`.
