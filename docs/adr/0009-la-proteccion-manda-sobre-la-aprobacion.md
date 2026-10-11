# ADR-0009 — La protección de seguridad manda sobre la aprobación humana

- **Estado:** Aceptado. Sustituye a la decisión D3 de la spec de Fase 2.
- **Fecha:** 2026-10-11

## Contexto

D3 (Fase 2) decía que una decisión humana mandaba sobre el clamp y sobre
`never_auto_paths`, «también en Fase 3». En Fase 3 el agente **ejecuta código de PRs
de terceros** en un sandbox. Con D3, un solo actor con el token de decisión podría
autorizar un parche en migraciones, auth, `docker/**` o el propio `.github/**`: una
superficie de cadena de suministro.

## Decisión

`never_auto_paths` y `protected_subject` son un **bloqueo duro**. Aunque una persona
marque `approved`, un hallazgo en esas rutas o temas **no** produce parche ni toca el
sandbox: queda `FixStatus.BLOCKED` con su razón y así se publica
(`fix/node.blocked_reason`).

La persona decide qué **puede** hacer el agente dentro de lo permitido. No amplía lo
que la política prohíbe.

## Consecuencias

- Es coherente con el principio de diseño «`protected_subject` bloquea el autofix
  siempre».
- El bloqueo se comprueba **antes** de gastar fixer o sandbox.
- **Lo que se escala no se rebaja a «¿lo arreglo?».** La guarda `never_auto_paths`
  ya no tapa un `escalate` de las reglas
  ([ADR-0003](0003-autonomia-como-politica-por-hallazgo.md)). Un `P0` o un tema
  protegido en `migrations/**` escala a una persona, en vez de pedir aprobación para
  un arreglo que acabaría `BLOCKED`. Cambio del 2026-10-11, con su test en
  `tests/test_policy.py`.
- La auditoría de cada decisión (`actor`, `finding_id`, `note`, `decided_at`) se
  mantiene.

## Alternativas descartadas

- **D3 tal cual.** Un actor único con la llave del repo.
- **Segunda señal (doble aprobación).** Innecesaria si lo protegido nunca se ejecuta.
