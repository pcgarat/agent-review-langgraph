# ADR-0003 — La autonomía es una política por hallazgo con un techo global

- **Estado:** Aceptado
- **Fecha:** 2026-10-10

## Contexto

Un «nivel de autonomía» global del agente («está en nivel 2») no sirve como
mecanismo. Un `P3` de naming en un test se puede arreglar sin preguntar. Un cambio en
auth nunca, aunque sea una línea con confianza 100. La autonomía adecuada depende de
**cada hallazgo**.

## Decisión

- La autonomía es **policy-as-data**: [`config/autonomy.yaml`](../../config/autonomy.yaml)
  asigna una acción a cada hallazgo (`report`, `escalate`, `fix_with_approval`,
  `fix_auto`). Las reglas se evalúan en orden y gana la primera que casa.
- **Las guardas frenan el arreglo, no el escalado.** Primero se evalúan las reglas.
  Si el fichero está en `never_auto_paths` (más `.cursor/**`, que siempre se añade),
  la acción pasa a `fix_with_approval`, **salvo que la regla ya diera `escalate`**: un
  `P0` o un tema protegido en una ruta nunca-auto sigue escalando
  ([ADR-0009](0009-la-proteccion-manda-sobre-la-aprobacion.md)).
- Un **techo global** (`max_autonomy`) recorta la acción:
  - Con `report`, todo se degrada a `report`, **también** `escalate`. Se conserva la
    razón original.
  - Con techos mayores, `escalate` nunca se rebaja.
- `MAX_AUTONOMY_OVERRIDE` permite a un despliegue **bajar** el techo, nunca subirlo.
  Subirlo exige un cambio versionado y revisado del YAML.
- Cada `Decision` lleva `matched_rule` y `policy_version` para poder auditarla.
- El motor (`policy/engine.py`) no usa LLM.

## Consecuencias

- Seguridad puede auditar y frenar al agente editando un fichero, sin desplegar.
- **`max_autonomy` es un parámetro de uso, no un hito de fase.** Quien opera el
  agente lo sube o lo baja cuando quiere, según cuánta autonomía quiera darle. El
  valor por defecto del YAML es `report`; con él, el HITL y el fix están implementados
  pero no se activan, porque nada llega a pendiente. Subirlo se hace editando el YAML;
  `MAX_AUTONOMY_OVERRIDE` solo sirve para bajarlo por despliegue.
- Una clave desconocida en un `when` solo falla al evaluar el primer hallazgo
  (`PolicyError`), no al arrancar.
- Las guardas `dedupe_by` y `require_tests_pass` del YAML no las lee ningún código:
  son declarativas.

## Alternativas descartadas

- **Nivel escalar global.** Explicaba bien la idea, pero decidía mal cada caso.
- **Que el LLM decida la acción.** No es determinista ni auditable.
