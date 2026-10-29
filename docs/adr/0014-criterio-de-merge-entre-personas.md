# ADR-0014 — Criterio único de dedupe y fusión entre personas

- **Estado:** Aceptado
- **Fecha:** 2026-10-11
- **Matiz:** 2026-10-24 — el representante conserva también la severidad más grave del grupo y el primer `protected_subject` no vacío.

## Contexto

El merge deduplicaba por `(fichero, título)`. PHPStan repite el mismo mensaje en
líneas distintas («Call to an undefined method…»), así que se perdían hallazgos
reales (B5 de la revisión de `main`). A la vez, el mismo problema visto por la
estática y por el LLM debe aparecer una sola vez, aunque cada uno lo ancle en una
línea algo distinta.

## Decisión

- **Clave única en todo el grafo:** `(fichero, línea, título)`, en
  `graph/build._merge_key`. El merge y la política usan la misma.
- **Fusión entre personas:** mismo `(fichero, título)` con anclas a
  ≤ `_CROSS_PERSONA_NEAR_LINES` (5) **y de personas distintas**. Dos hallazgos de la
  misma persona en líneas distintas nunca se fusionan, ni siquiera a través de un
  tercero.
- La procedencia viaja en el estado (`finding_sources`), no en `Finding`.
- De cada grupo se queda el hallazgo con más evidencia. El resultado no depende del
  orden del fan-out. Sobre esa copia se copian, si otra del grupo los trae, la
  severidad más grave y el `protected_subject`: la evidencia más larga no puede
  tapar un tema protegido ni un P0.
- La identidad de publicación de un hallazgo es `_finding_id` (`id` o
  `fichero:línea:título`). Es la misma para la marca inline, la decisión de política
  y la decisión humana.

## Consecuencias

- Un hallazgo sin `id` sigue teniendo una identidad estable y su propia marca.
- `guards.dedupe_by` en el YAML **no** lo lee nadie. El criterio vive en el código.

## Alternativas descartadas

- **`(fichero, título)`.** Se comía hallazgos.
- **Ventana de líneas sin mirar la persona.** Fusionaría repeticiones legítimas de la
  misma herramienta.
