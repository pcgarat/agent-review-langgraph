# ADR-0002 — Copiar el contrato de hallazgos de `ce-code-review`, no invocar su runtime

- **Estado:** Aceptado
- **Fecha:** 2026-10-10
- **Corrección:** 2026-10-24 — el runner sí ejecuta la skill, fuera del núcleo
  ([ADR-0033](0033-skill-fuera-del-nucleo.md)). El núcleo sigue validando el contrato.

## Contexto

`ce-code-review` ya tiene un vocabulario de clasificación probado (severidad,
`autofix_class`, confianza anclada, evidencia con la línea citada, tema protegido).
Pero es una skill de prompt: no es una API, su contrato cambia con el plugin y desde
un grafo no se puede probar en serio.

## Decisión

- Se copian a [`contracts/`](../../contracts/) `findings-schema.json` y
  `action-class-rubric.md` como contrato **versionado en este repo**.
- `domain.models.Finding` es su espejo Pydantic. Un test compara modelo y esquema: si
  cambia uno, cambia el otro en el mismo commit.
- Cada persona se reimplementa como un nodo con salida validada contra el contrato
  (`domain/contracts.validate_finding`).
- La confianza solo admite `0/25/50/75/100`.

## Consecuencias

- La política puede ser determinista ([ADR-0003](0003-autonomia-como-politica-por-hallazgo.md)):
  decide sobre campos con valores cerrados, no sobre texto libre.
- La imagen Docker **debe** copiar `contracts/`, porque la validación lo lee en
  tiempo de ejecución. Faltaba y lo destapó el smoke de Fase 1.
- Un cambio del contrato aguas arriba no llega solo: hay que traerlo a mano.

## Alternativas descartadas

- **Invocar la skill o un CLI agent (`claude --print`) como herramienta.** Añade una
  capa opaca al núcleo y no hay contrato estable que probar.
