# ADR-0017 — Replay determinista con fingerprint para medir la review

- **Estado:** Aceptado
- **Fecha:** 2026-10-16

## Contexto

El *gold set* de [006](../iniciativas/archivo/006-eval-de-la-review/spec.md) mide la calidad
de la review en CI, pero el modelo real no es determinista: dos corridas del mismo
prompt dan números distintos. Un gate de CI contra el modelo sería *flaky* y además
se pagaría en cada `push`. Y una corrida contra el modelo real no reproduce el
baseline: re-descubre unos hallazgos y no encuentra otros, de modo que no sirve para
detectar que un cambio de contrato, de merge o de política movió una métrica.

La alternativa —inventar un `FakeLlm` que devuelva hallazgos fijos— mediría una copia
del sistema, no el sistema: el camino real (anclaje, merge, política, publicación)
quedaría sin cubrir.

## Decisión

- **Dos modos sobre el mismo puerto.** El harness corre el **grafo real** con un
  `LlmPort` inyectado; lo único que cambia entre *replay* y real es el puerto (E7 de
  006). En CI corre *replay*; el modelo real corre a mano (`make eval-live`).
- **Grabación por rol y llamada.** `recorded.json` guarda `text` y `usage` de cada
  llamada del rol, indexada por el **fingerprint del prompt**.
- **Fingerprint del prompt y del caso.** El del prompt cubre rol + `system` + prompt
  + `max_tokens` + el contrato de salida (`json_mode` y `response_schema` canónico);
  el del caso, nombre + `diff` + `scope`. *(Corrección 2026-10-16: la primera versión
  no incluía el contrato de salida, y un cambio del esquema estructurado reutilizaba la
  respuesta vieja sin abortar. Huella v2; la grabación de `pr_3` se re-indexó sin
  cambiar sus respuestas.)* Un prompt o un caso que no
  casan **abortan** la corrida (`FingerprintMismatch`) en vez de devolver una
  respuesta ajena: una grabación vieja no mide el pasado en silencio (E4).
- **El rol forma parte del fingerprint** porque dos roles con el mismo diff y tope de
  hallazgos generan el mismo prompt; sin él, regrabar un rol invalidaría los demás.
- **La grabación es un artefacto revisable.** Regrabar es un diff en `recorded.json`
  y forma parte del PR que cambia el prompt o el contrato que el modelo consume.
- **El baseline es un informe congelado** (`expected_report.json`): compara los dos
  modos por dominancia, no por igualdad de números.

## Consecuencias

- CI comprueba que ningún cambio **posterior** al modelo (merge, política, anclaje)
  mueve las métricas, sin gastar un token.
- Un cambio que altera **lo que recibe** el modelo (lotes, contexto, prompt) invalida
  el fingerprint: exige regrabar, y esa regrabación se revisa.
- La corrida real es comparativa y no bloquea CI; su varianza queda **no medida**
  (n=1, E15).
- La cobertura sale de `LlmReviewer.last_seen_files`, un campo de solo lectura
  independiente del puerto: se mide igual en *replay* y en real.

## Alternativas descartadas

- **Gate de CI contra el modelo real.** *Flaky* y de pago en cada `push`.
- **`FakeLlm` con hallazgos fijos.** Mediría una copia del sistema, no el grafo real.
- **Congelar las métricas sin grabación.** Un número congelado a mano no dice de qué
  salida salió ni cuándo cambió el prompt: sin el fingerprint, una grabación obsoleta
  mediría el pasado en silencio.
