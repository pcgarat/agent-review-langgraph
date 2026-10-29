Última modificación: 2026-10-29 | Creado: 2026-10-29

# Cada paso dice cuántos hallazgos entraron, salieron y por qué

> **Estado:** aprobada
>
> **Entrega objetivo:** en la tarjeta «Pasos» de la ficha, cada paso del motor
> `claude` dice, en vez de un texto fijo, qué hizo con los hallazgos: cuántos
> devolvió la skill, cuántos se descartaron y en qué filtro, cuántos quedaron
> tras juntar y cuántos se marcaron para publicar. Los descartes se pueden abrir
> para ver título y motivo.

---

## 1. Objetivo

La PR 27 (`4cdffd23`, 2026-10-29) terminó «sin hallazgos». La skill había
encontrado un P1 y lo había guardado en el artefacto, pero L0 lo tiró porque la
primera evidencia era prosa y no estaba en el diff (arreglado en `7acb8eb`).
Para saberlo hubo que leer el JSONL del CLI, el artefacto del volumen y los
checkpoints de Postgres. La ficha decía «Revisión de las personas terminada» y
«Hallazgos unidos», que no informan de nada.

Hoy, de cada filtro:

| Filtro | Dónde se descarta | Qué queda registrado |
|---|---|---|
| Contrato del objeto `mode:agent` | `adapt_agent_mode` (`adapters/agent_mode.py`), en el worker, antes del grafo | Una línea de texto en el diario de Redis, con hasta tres motivos |
| L0: la cita tiene que estar en el diff | `ClaudeArtifactReviewer.review` (`adapters/claude_artifact.py`), nodo `persona` | Nada |
| Duplicados | `merge` | Un `logger.info` con el número |
| Marcar para publicar | `stamp` | `decisions` en el checkpoint |

Se quiere que cada paso de la tarjeta lleve su cifra, y que un descarte diga qué
hallazgo era y por qué cayó.

## 2. No-objetivos

- **No se tocan el polling, el maximizado ni los pasos de antes del grafo**
  (`queued`, `claude`). Son V1, V4 y V9 de
  [031](../031-progreso-en-vivo-del-panel/spec.md). Esta iniciativa concreta
  solo su V6 («lo que ha hecho» sale del checkpoint), y la amplía con los
  descartes.
- **El motor `llm` no cambia** salvo en `scope` y `merge`, que comparte con
  `claude`. Sus nodos propios (`verify`, `policy`, `fix_*`) siguen con el texto
  fijo.
- **No se cambia ningún filtro.** Qué se descarta lo deciden el contrato y L0.
  Aquí solo se cuenta.
- **No se interpreta la transcripción del CLI.** Las cifras salen del adaptador
  y del grafo, no del JSONL ([033](../033-transcripcion-legible-del-cli/spec.md)).

## 3. Decisiones

| # | Decisión | Motivo |
|---|---|---|
| CP1 | **Las cifras viven en el checkpoint.** Un canal nuevo del estado, `discarded: list[DiscardedFinding]` con reducer de suma, guarda cada descarte con `{stage, id, title, file, reason}`. `stage` es `contract` o `l0` | El checkpointer ya es la fuente de la traza (019). Si una cifra viviera en el diario y otra en Postgres, se contradirían tras un reintento o un olvido. Un hilo olvidado borra las dos cosas a la vez |
| CP2 | **El adaptador deja sus descartes junto al artefacto**, en `<sha>.discarded.json`, y `ClaudeArtifactReviewer` los lee y los devuelve en su `ReviewerResult` junto con los de L0. `forget` borra los dos ficheros | El adaptador corre antes del grafo y no puede escribir en el estado. El artefacto ya es el puente entre el worker y el nodo `persona`. Un reintento que reutiliza el artefacto reutiliza también sus descartes |
| CP3 | **`ReviewerResult` gana `discarded`**, vacío por defecto. Las otras personas no lo pueblan | Es el camino por el que `persona` ya devuelve todo lo demás (cobertura, presupuesto). Un campo opcional no rompe a nadie |
| CP4 | **La traza lleva un resumen por nodo**, `TraceNode.summary`, calculado por el `web` a partir del checkpoint que cerró el nodo: cifras, no texto. Un nodo sin resumidor deja `summary` en `null` | V6 de 031. El navegador no lee el estado del grafo (P13 de 029). Las cifras son datos: el texto en español se escribe en el frontend, donde están los demás textos de la tarjeta |
| CP5 | **El texto lo compone `steps.ts`**, con la cifra si hay `summary` y el texto fijo de hoy si no | Una review vieja sin `summary` se sigue viendo como ahora |
| CP6 | **Los descartes se abren con un `<details>` cerrado** debajo del paso, uno por hallazgo: `#id · título · motivo`, con el motivo recortado a 200 caracteres | El motivo de contrato puede ser largo (un error de pydantic). Cerrado no empuja el resto de pasos |

## 4. Diseño o contrato

### 4.1 Resumen por nodo

| Nodo | `summary` | Texto en la tarjeta |
|---|---|---|
| `scope` | `{files, ignored}` de `coverage` | «{files} ficheros en el diff» y, si hay, «· {ignored} ignorados» |
| `persona` (motor `claude`) | `{returned, contract, l0, kept}` | «La skill devolvió {returned} · {contract} descartados por contrato · {l0} descartados por L0 · {kept} entran». Los ceros se omiten |
| `persona` (motor `llm`) | `null` | El de hoy |
| `merge` | `{before, after}` | «{after} tras juntar» y, si hay, «· {before − after} duplicados» |
| `stamp` | `{publish, other}` de `decisions` | «{publish} para publicar» |
| `publish` | `{published, inline}` de `published` e `inlined_count`, que ya escribe | «Comentario en el PR · {inline} en línea», o «No se escribió en el PR» |

`returned` es lo que devolvió la skill: `kept + contract + l0`. Con
`returned = 0` el texto es «La skill no devolvió hallazgos».

### 4.2 Forma

```python
class DiscardedFinding(BaseModel):
    stage: Literal["contract", "l0"]
    id: str | None          # el `#` de la skill, si lo había
    title: str | None
    file: str | None
    reason: str             # recortado a 300

class TraceNode(BaseModel):
    ...
    summary: dict[str, int] | None = None
    discarded: list[DiscardedFinding] = []   # solo en `persona`
```

`discarded` entra en la allowlist del serde del checkpointer.

Las cifras salen de lo que escribió la tarea del nodo: los `pending_writes` del
checkpoint desde el que corrió, el mismo sitio del que la traza ya saca las
aristas. `merge` lee además `findings` de ese checkpoint para saber cuántos
había antes. Si en el mismo paso corrieron varias ramas de `persona` (motor
`llm`), no hay resumen: las escrituras no dicen qué rama fue cada una.

### 4.3 Motivos de L0

`evaluate_l0` ya devuelve una disposición. El motivo es una de:

- «el fichero no está en el diff»;
- «la cita no aparece en el diff» (el caso de la PR 27);
- «la línea no se pudo anclar».

## 5. Riesgos

| # | Riesgo | Qué lo contiene |
|---|---|---|
| R1 | Un artefacto guardado antes de esta iniciativa no tiene `discarded.json` | Se lee como «sin descartes de contrato». La review vieja no se recalcula |
| R2 | El canal nuevo rompe la lectura de checkpoints viejos | Canal con valor por defecto; un checkpoint sin él se lee vacío. Un test lo cubre con un checkpoint de antes |
| R3 | El resumidor de un nodo se equivoca y la tarjeta miente | Tests del resumidor sobre los checkpoints de la PR 27 reproducidos en memoria: 1 devuelto, 1 descartado por L0, 0 entran |
| R4 | La traza crece con muchos descartes | El motivo se recorta y la lista va solo en `persona` |

## 6. Criterios de hecho

1. Con un artefacto como el de la PR 27 (cita que no está en el diff), el paso
   «Personas» dice «La skill devolvió 1 · 1 descartado por L0» y, al abrirlo,
   muestra el título del P1 y «la cita no aparece en el diff».
2. Un hallazgo que el contrato rechaza sale en el mismo paso, con su motivo,
   aunque el resto de hallazgos sí entre.
3. `merge` y `stamp` dicen sus cifras. `scope` dice los ficheros.
4. Una review anterior a la iniciativa se ve como hoy, sin errores.
5. `forget` borra también `discarded.json`.
6. `make ci` y `make frontend-check` en verde. `docs/arquitectura.md` §3–4
   recoge el canal nuevo y el resumen de la traza.

## 7. Preguntas abiertas

- **Q1** — cerrada el 2026-10-29: la línea del diario «Claude: se descartaron N
  hallazgos» se queda. Es lo único que queda si el CLI falla antes del grafo.
- **Q2** — cerrada el 2026-10-29: `publish` ya escribe `published` e
  `inlined_count`. El resumen sale de ahí y no hace falta un canal nuevo.
- **Q3** — cerrada el 2026-10-29: los descartes de contrato van en `persona`
  mientras no exista el paso `claude` de V4 de 031.
