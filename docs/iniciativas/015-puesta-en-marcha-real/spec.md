Última modificación: 2026-10-16 | Creado: 2026-10-16

# Puesta en marcha real: smokes, pendientes humanos y endurecimiento

> **Estado:** borrador
>
> **Entrega objetivo:** el agente corre contra infraestructura real (Postgres,
> Bitbucket, el `sandbox-runner` con Docker rootless, el modelo y el bot de Teams) y
> cada fase entregada tiene un **smoke real con evidencia**. Los pendientes humanos
> que lo bloquean están cerrados y, en `production`, ningún fallo de infraestructura
> se traga en silencio.

---

## 1. Objetivo

Las iniciativas 001–005 y 013 tienen el código cerrado y probado con *fakes*. Lo que
les queda es lo mismo en todas: **probarlo de verdad** y cerrar los pendientes
humanos que lo bloquean. Esos pendientes estaban repartidos en seis `todo.md` y en la
tabla de pendientes transversales, y P6 aparecía en cuatro sitios.

Se quiere:

1. **Un solo sitio** para todo lo que necesita infraestructura real o una persona, de
   modo que las iniciativas de origen puedan archivarse.
2. **Un orden explícito**: primero lo que bloquea (P7, P6), después los smokes.
3. **Evidencia** de cada smoke en [`operacion.md`](../../operacion.md), no solo un
   `[x]`.
4. Que en `production` no queden **fallbacks abiertos** (D6) ni secretos por defecto
   (D7) que hagan pasar un smoke en falso.

## 2. No-objetivos

- **Código nuevo de las fases.** Si un smoke encuentra un fallo, la corrección es una
  tarea aquí, pero un rediseño va a su propia iniciativa.
- **La cascada de calidad** (007–012, 014, 016, 017). Se mide con el *gold set* de
  [006](../archivo/006-eval-de-la-review/spec.md), no con estos smokes.
- **La Fase 4** (autonomía por política, Jira, memoria). Sigue esbozada en
  [001 §13](../archivo/001-diseno-base/spec.md) sin iniciativa.
- **Alta y operación continuada** (monitorización, guardias). Este trabajo deja el
  sistema probado una vez en real; operarlo es otro asunto (D14, D16).

## 3. Contexto y evidencia

### 3.1 Alcance: lo que se trae de otras iniciativas

Los IDs originales **se conservan**: son estables en el repo y los citan commits y
documentos. Lo que no tenía ID recibe uno `S`.

| ID | Origen | Qué | Tipo | Depende de |
|---|---|---|---|---|
| **P7** | [005](../archivo/005-modelos-declarativos/todo.md) | Rotar la API key de `nan.builders` que quedó expuesta en el chat de diseño | Humano · seguridad | — |
| **S6** | [003 Q3](../archivo/003-fase3-fix-verificacion/todo.md) | Poner (y rotar) el token de Bitbucket de solo lectura del runner en el `.env` del host | Humano · seguridad | — |
| **P6** | [001](../archivo/001-diseno-base/todo.md), [003](../archivo/003-fase3-fix-verificacion/todo.md), [004](../archivo/004-fase3-e7-desacople/todo.md) | Revisión de seguridad de la heurística de tema protegido | Humano · seguridad | — |
| **S1** | [002](../archivo/002-fase2-hitl/todo.md) | Smoke del checkpointer contra Postgres real: pool y fallo de arranque en `production` | Smoke | — |
| **S4** | D6 de [`arquitectura.md`](../../arquitectura.md) §7 | En `production`, sin Redis la API, el CLI y el worker fallan en vez de encolar en memoria y responder OK | Código + test | — |
| **S5** | D7 de [`arquitectura.md`](../../arquitectura.md) §7 | Comprobar que el despliegue corre con `ENVIRONMENT=production` y secretos reales (arranque *fail-closed*, [ADR-0013](../../adr/0013-secretos-por-entorno-fail-closed.md)) | Smoke | P7, S6 |
| **T10** | [013](../archivo/013-canal-teams-bidireccional/todo.md) | Alta del bot de Teams en el tenant. Los pasos están en el `todo` de 013 | Humano | — |
| **T0** | [013](../archivo/013-canal-teams-bidireccional/todo.md) | Las comprobaciones del spike (hilo raíz, respuesta en el hilo, `Action.Execute` que llega al ingress, tarjeta actualizada) como aceptación de T10 en el tenant real | Smoke | T10 |
| **F11** | [003](../archivo/003-fase3-fix-verificacion/todo.md) | Spike R1: elegir el `DeliveryFormatter` según lo que renderice Bitbucket, con una credencial de prueba | Spike | Credencial |
| **F12** | [003](../archivo/003-fase3-fix-verificacion/todo.md), [004](../archivo/004-fase3-e7-desacople/todo.md) | Smoke real del fix: aprobar en el HITL → `fix-worker` → runner → sugerencia en el PR | Smoke | P6, S6, F11 |
| **S2** | [005](../archivo/005-modelos-declarativos/todo.md) | Smoke real sobre un PR de `emotivoo-v2` midiendo los hallazgos descartados por el contrato; decide 005 Q3 (`json_schema` estricto) | Smoke + decisión | P7 |
| **S3** | [004 Q1](../archivo/004-fase3-e7-desacople/todo.md) | Decidir la forma de la wave (una por wave o una por hallazgo) con la primera medición real | Decisión | F12 |
| **P5** | [001](../archivo/001-diseno-base/todo.md) | Medir la latencia con carga real (LLM + PR grande) | Medición | S2 |
| **P3** | [001](../archivo/001-diseno-base/todo.md) | Revisión humana del formato inline y del resumen ([ADR-0006](../../adr/0006-publicacion-inline-primero-resumen-al-final.md)) | Humano | S2 |
| **P1** | [001](../archivo/001-diseno-base/todo.md) | *(Opcional.)* El CI de `emotivoo-v2` hace `POST /webhook/tooling` firmado ([ADR-0004](../../adr/0004-ingesta-diferida-por-artefacto-del-ci.md)) | Externo | — |

### 3.2 Por qué D6 entra aquí

Con Redis caído, la API y el CLI encolan en memoria y responden OK. En un smoke real
eso da un verde falso: el trabajo se pierde al reiniciar el proceso, y la persona que
aprobó en Teams cree que su decisión llegó. D7 ya es *fail-closed* en `production`
([ADR-0013](../../adr/0013-secretos-por-entorno-fail-closed.md)); lo único que
falta es comprobar que el despliegue real corre en ese modo (S5).

## 4. Decisiones

| # | Decisión | Motivo |
|---|---|---|
| X1 | **Una sola iniciativa** para todo lo que necesita infraestructura real o una persona | Las iniciativas de código se archivan al cerrar su código; lo pendiente no se dispersa en seis `todo.md` |
| X2 | Los IDs de origen **se conservan**; en el origen la tarea se marca `[x]` como trasladada a 015 | Los IDs son estables en el repo y la historia queda trazable |
| X3 | **Orden:** P7 y S6 → P6 → (S1 · S4 · T10 · F11 en paralelo) → S5 → (F12 · S2) → (S3 · P5 · P3) | Primero se cierra lo expuesto, después lo que bloquea al runner y al final lo que necesita tráfico real |
| X4 | En `production`, **sin Redis no hay fallback a memoria**: la API responde `503`, el CLI sale con error y el worker no arranca. Fuera de `production` el fallback se mantiene | Mismo criterio *fail-closed* que [ADR-0013](../../adr/0013-secretos-por-entorno-fail-closed.md); en desarrollo, la comodidad sigue |
| X5 | Cada smoke deja **evidencia** en [`operacion.md`](../../operacion.md) §6–7: fecha, entorno, PR usado y resultado | Un `[x]` sin evidencia no se puede repetir ni auditar |

## 5. Diseño o contrato

### 5.1 Entorno

Un único entorno de pruebas real, con:

- Postgres y Redis de Compose, con volúmenes persistentes (S1, S4).
- El `sandbox-runner` con su Docker rootless dedicado y la imagen de CI por digest
  ([ADR-0011](../../adr/0011-sandbox-runner-aislado.md)) (F12).
- Un repo de prueba en Bitbucket, o un PR de `emotivoo-v2` acordado, con la cuenta de
  servicio de [ADR-0012](../../adr/0012-vcs-tras-un-puerto.md) (F11, F12, S2).
- El bot de Teams dado de alta en un canal estándar de pruebas (T10).

### 5.2 S4 — sin fallback en `production`

- `build_queue`, `build_paused` y `build_pending` dejan de caer a memoria cuando
  `ENVIRONMENT=production`, en la API, en el CLI y en el worker
  ([`worker/main.py`](../../../src/agent_review/worker/main.py), que hoy solo deja un
  `logger.warning`). El error de conexión sube como `TransientError`: la API lo
  traduce a `503`, el CLI sale con error y el worker no arranca.
- La duplicación de esas raíces de composición (D10) no se resuelve aquí, pero el
  cambio se aplica en las tres para que no diverjan más.

## 6. Riesgos

| # | Riesgo | Mitigación |
|---|---|---|
| R1 | **P6 no se cierra** y bloquea F12, S3 y medio plan | Se pide ya (X3); todo lo que no depende de P6 avanza en paralelo |
| R2 | El smoke encuentra un fallo de diseño, no un bug | Se registra en [`arquitectura.md`](../../arquitectura.md) §7 y se abre iniciativa; 015 no se convierte en un rediseño |
| R3 | S4 rompe flujos de desarrollo que contaban con el fallback | El fallback se mantiene fuera de `production` (X4), con un test por entorno |
| R4 | Las credenciales de prueba se filtran en logs o en el chat | Solo por entorno ([ADR-0013](../../adr/0013-secretos-por-entorno-fail-closed.md)); P7 y S6 van primero precisamente por eso |
| R5 | El PR de `emotivoo-v2` usado para los smokes cambia y no se puede repetir | La evidencia (X5) apunta a un `head_sha` concreto |

## 7. Criterios de hecho

1. P7, S6 y P6 cerrados por las personas responsables, con fecha.
2. S1, S5, F12 y S2 ejecutados en el entorno real, con evidencia en
   [`operacion.md`](../../operacion.md) (X5).
3. S4: con `ENVIRONMENT=production` y Redis caído, la API responde `503` y el CLI
   sale con código distinto de 0 (test); fuera de `production`, el comportamiento de
   hoy (test). D6 sale de [`arquitectura.md`](../../arquitectura.md) §7.
4. F11 deja elegido el `DeliveryFormatter`; S3 y 005 Q3 quedan decididos y
   anotados en sus specs de origen con fecha.
5. T10 hecho: el bot publica en el canal de pruebas y una decisión desde la tarjeta
   reanuda la review.
6. P5 medido y P3 revisado; P1 hecho o descartado de forma explícita.
7. `make ci` en verde, con `make docs-check`; [`arquitectura.md`](../../arquitectura.md)
   §6 sin «smoke real» pendiente en ninguna fase.

## 8. Preguntas abiertas

- **Q1 — ¿Qué entorno?** ¿Un host dedicado o el de producción con un repo de prueba?
  Para F12, el host necesita Docker rootless.
- **Q2 — ¿Quién cierra P6, y cuándo?** Es la ruta crítica de F12.
- **Q3 — ¿Qué PR se usa para S2 y P5?** ¿Uno real de `emotivoo-v2` acordado con el
  equipo, o un repo espejo?
- **Q4 — ¿P1 se hace o se descarta?** Es opcional desde
  [ADR-0004](../../adr/0004-ingesta-diferida-por-artefacto-del-ci.md): sin él solo se
  pierde la anotación inline de un pipeline rojo.
