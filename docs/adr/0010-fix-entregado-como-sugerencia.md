# ADR-0010 — El fix se entrega como sugerencia; el agente no escribe en el repo

- **Estado:** Aceptado
- **Fecha:** 2026-10-11

## Contexto

Un agente que hace commit o push en el repo revisado es superficie de cadena de
suministro y obliga a una saga de compensación (revertir commits). Bitbucket Cloud no
tiene *suggested changes* como GitHub, y `twg` no expone nada parecido.

## Decisión

- **Sin escritura en el repo.** No existe en el código ningún camino a
  `commit`/`push`/`branch create`. El fix verificado se publica **dentro del
  comentario inline** y lo aplica una persona.
- **Generación por LLM** (`LlmFixer` sobre el `LlmPort`, con el mismo presupuesto). El
  `suggested_fix` en texto libre del hallazgo es una pista, no el parche. El fixer ve
  el hallazgo, el fichero al `head_sha` y, en el segundo intento, los `stderr_tail`
  del primero. **Nunca** ve el `.env`, el perfil ni la salida de `setup`.
- **Validación local** antes del sandbox: el fichero del hallazgo, un hunk,
  ≤ `max_fix_lines` (30) y un rango dentro de las líneas **añadidas** del PR. Si no
  pasa, es `INVALID_PATCH` y cuenta como intento.
- Como mucho `max_fix_attempts` (2) intentos y `max_fixes_per_pr` (3) fixes; el resto
  queda `SKIPPED`.
- **Seam de entrega.** Un `DeliveryFormatter` (Strategy) emite ` ```diff ` (por
  defecto, `FIX_DELIVERY=diff`) o ` ```suggestion `. Que Bitbucket renderice o no
  `suggestion` (riesgo R1) solo cambia el adaptador.
- Sin fixer o sin runner configurado, el fix queda `NOT_VERIFIABLE`: no se finge.

## Consecuencias

- La compensación se reduce a destruir el workspace y no publicar nada aplicable.
- Un fix que falla no vuelve a pausar: se publica con su log, para evitar bucles de
  decisión.
- Fix multi-fichero y fixers deterministas (php-cs-fixer, rector) quedan fuera de v1.
  El `FixerPort` deja la puerta abierta.

## Alternativas descartadas

- **Commit en la rama del PR o PR de seguimiento.** Se reconsidera con datos (Fase 4).
- **Aplicar el `suggested_fix` en texto libre.** No se puede validar ni aplicar.
