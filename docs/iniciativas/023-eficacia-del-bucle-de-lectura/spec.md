Última modificación: 2026-10-17 | Creado: 2026-10-17

# Eficacia del bucle de lectura

> **Estado:** aprobada
>
> **Entrega objetivo:** que las tres herramientas que
> [014](../014-revisor-con-herramientas/spec.md) ya cablea (`read_file`, `grep`,
> `list_dir`) sean **eficaces**: que una búsqueda encuentre lo que el modelo pide,
> que una lectura larga se pueda acotar y **declare su corte**, y que el modelo no
> pague por lo que ya tiene. No añade herramientas: añade **modos** a las tres que
> hay.

---

## 1. Objetivo

014 dejó el bucle funcionando: 72 llamadas, 10 hallazgos publicados y cobertura 1.0
en el PR #3. La medición de su regrabación (2026-10-17) dice que las tres
herramientas rinden muy por debajo de lo que cuestan, y que el gasto **no está
donde parecía**. Esto no va de leer más, sino de que lo que se lee sirva.

Se quiere:

1. Que `grep` **encuentre lo que el modelo pide**. Hoy es literal
   (`git grep -F`, [016 Q1](../archivo/016-lector-de-contexto/spec.md)) y el modelo
   escribe expresiones regulares: **42 de 64** `grep` de la grabación llevan
   metacaracteres y devuelven **cero** coincidencias, quemando el turno entero.
2. Que una lectura **declare su corte** y se pueda acotar. `read_file` sirve el
   fichero entero (43 de 55 sin rango; `worker/main.py`, 1.179 líneas) y, cuando el
   presupuesto de bytes corta, **no lo dice**: el modelo concluye sobre medio
   fichero creyendo que lo vio entero.
3. Que no se **pierda trabajo pedido**: 14 peticiones se descartan en silencio por
   el tope de 3 por turno, en 11 de 52 turnos.
4. Que el coste se **mida antes de decidir**. El gasto del PR #3 es 744k tokens de
   *prompt* y 417k de *completion* (384k de razonamiento); el diseño se elige por
   el número que mueve cada palanca, no por lo que suena bien.

## 2. No-objetivos

- **Herramientas nuevas.** `search` no existe como herramienta (es un alias de
  `grep`); no se añade una cuarta operación. Lo nuevo entra como **modo** de las
  tres que hay (`read_file(path, mode="outline")`), sin ensanchar el puerto.
- **Ejecutar código (L3)** ni **verificar hallazgos (L2)**: son Q6 de 014 y
  [009](../archivo/009-verificador-adversarial/spec.md).
- **Cambiar el anclaje al diff ni L0** ([014 H7](../014-revisor-con-herramientas/spec.md)).
- **Especializar el *prompt* por rol (D20).** Es una carencia real —`correctness`,
  `security` y `arch` comparten `_SYSTEM` y sus hallazgos se prefijan
  `correctness`— pero no es eficacia de herramientas y hoy **no hay *gold set* que
  lo mida**: solo `correctness` está cableado en el harness. Se saca a su **propia
  iniciativa** (Q1).
- **Añadir herramientas o cambiar la metodología de 006 por gusto.** `search` no
  entra como herramienta nueva (§2) y el matcher solo se toca por lo que §6 obliga:
  para poder medir. Su cambio va a ADR y a la spec de 006, pero se ejecuta **aquí**,
  como bloque 0, porque sin él esta iniciativa no tiene número (R1).

## 3. Contexto y evidencia

Medición de `tests/eval/pr_3/recorded_tools.json` (regrabación del 2026-10-17,
72 llamadas, `deepseek-v4-flash-revisor`, razonamiento medio):

| Observación | Medida |
|---|---|
| Lotes reales (`_batches`, `LLM_BATCH_MAX_CHARS=20 000`) | **19** |
| Llamadas al modelo / turnos con herramientas | 72 / 52 |
| `grep` con metacaracteres ERE (hoy **0** coincidencias) | **42** de 64 |
| `read_file` sin rango / con rango | 43 / 12 |
| Ficheros leídos en más de un lote | 16 (`domain/models.py` en 5) |
| Repeticiones **dentro** de un mismo turno | 0 |
| Peticiones descartadas por el tope de 3 | **14**, en 11 turnos |
| Tokens de *prompt* / *completion* (razonamiento) | 744k / 417k (384k) |
| *Prompt* base por lote (diff + L1) | 2,4k–18,5k tokens |

De ahí salen tres lecturas que ordenan el diseño:

- **El gasto dominante es el reenvío, no el catálogo.** Cada turno reenvía la
  conversación entera ([016 W2](../archivo/016-lector-de-contexto/spec.md): no hay
  lista de mensajes). El *prompt* base de un lote es una fracción del total; el
  resto es volver a mandar lo mismo turno a turno. Añadir herramientas lo agrava.
- **El caché no ahorra *tokens*.** Las repeticiones son **entre lotes** (el lector
  se construye por lote en `_call_with_tools`), así que un caché de turno no
  ahorraría nada. Pero uno de *review* tampoco: cada lote es una conversación nueva
  y el modelo necesita ver los bytes igual. Ahorra E/S, no *prompt*. Lo que sí
  ahorra *prompt* es **no precargar** lo que el bucle puede leer (Q3 de 014) y
  **reducir el número de lotes**.
- **El modelo pide bien; el puerto responde mal.** No es que le falten
  herramientas: usa `grep` 64 veces con sintaxis regex que el puerto no entiende y
  `read_file` sin rango 43 veces. El fallo está en la interfaz, no en el catálogo.

## 4. Decisiones

| # | Decisión | Motivo |
|---|---|---|
| E0 | **D23 se arregla antes y aparte.** El *replay* con *fingerprint* caducado debe **abortar**, no degradar a «0 hallazgos» ([D23](../../arquitectura.md)) | Toda mejora de aquí **cambia el *prompt*** e invalida la grabación. Sin D23, cada una hace salir `make eval-tools` en verde con recall 0: mediríamos el pasado |
| E0b | **El matcher mide defecto, no redacción**, y se corrige aquí (bloque 0: T1 + T2) | Un mismo defecto redactado distinto puntúa distinto: `build.py:1123` es `R3` en el *baseline* y «0» con herramientas; `docker-compose.yml:76` es `integracion-1` **y** `integracion-2`. Hasta arreglarlo, ningún número de 023 ni el RH11 de 014 significan nada |
| E1 | El alcance es la **eficacia de las tres herramientas**, no el catálogo | La medición dice que el fallo es la interfaz (regex, rangos, avisos), no la falta de operaciones |
| E2 | `grep` gana `mode: "literal" \| "regex"`, con **default `literal`**; el *system prompt* anuncia el modo | Enmienda [016 Q1](../archivo/016-lector-de-contexto/spec.md), que eligió literal «para cerrar el riesgo de un patrón caro». No se cambia la semántica por debajo: `foo.bar` en literal no debe pasar a casar `fooXbar` sin que el modelo lo pida. ADR nuevo |
| E3 | `grep` se acota con `path` y `glob`, y gana `files_only` | El modelo ya manda `glob` (44 casos) pero no puede acotar a un subárbol; `files_only` evita traer 200 líneas para descubrir dos rutas |
| E4 | `read_file` **declara el truncado** (`…[cortado: quedan N bytes; usa start/end]`) | Hoy `_budget_bytes` corta en silencio y el modelo concluye sobre medio fichero |
| E5 | `read_file` gana `mode: "full" \| "outline"`: índice de símbolos con su rango | Evita leer 1.179 líneas para localizar una función; es el ahorro con mejor relación valor/coste de la lista |
| E6 | `list_dir` gana filtro por `glob` y recursión acotada (profundidad declarada) | El modelo lo usa 18 veces solo para orientarse |
| E7 | El tope de peticiones por turno **se mide**, no se sube a ciegas: `3` contra `4–5` con su flag | Subirlo baja turnos pero engorda cada uno (y el turno se reenvía): es una palanca a medir, no una mejora obvia |
| E8 | La **precarga de L1** en el bucle se vuelve conmutable (Q3 de 014) | Es la palanca grande de *prompt*: con herramientas activas, el CONTEXT precargado viaja en cada turno y el bucle puede leerlo. Se mide contra el *baseline* antes de fijarla |
| E9 | Un **bloque de estado** por review resume lo ya servido (rutas y rangos), y el lector los **re-sirve** desde caché de E/S | Evita el coste de re-leer 16 ficheros entre lotes sin pretender ahorrar *prompt* (que no lo haría). Los bytes siguen llegando al modelo |
| E10 | Los modos son **parámetro de las tres operaciones**; el puerto **no** gana operaciones | Mantiene el contrato de 016 y el *fingerprint* acotado a lo que cambia |
| E11 | Cada mejora entra detrás de **su flag**, con una corrida por flag | Pedido explícito: poder aislar el efecto de cada una contra el *baseline* |

## 5. Diseño o contrato

### 5.1 Modos sobre tres operaciones

```json
{"tools": [
  {"tool": "grep", "pattern": "complete\\(|model_for\\(", "mode": "regex", "glob": "src/**/*.py", "files_only": false},
  {"tool": "read_file", "path": "src/agent_review/worker/main.py", "mode": "outline"},
  {"tool": "read_file", "path": "src/agent_review/worker/main.py", "start": 640, "end": 700},
  {"tool": "list_dir", "path": "src/agent_review", "glob": "*.py", "depth": 2}
]}
```

`mode` ausente equivale al comportamiento de hoy (`literal`, `full`), de modo que
una grabación vieja no cambia de significado y el modelo que no lo declare sigue
funcionando. El *system prompt* documenta el modo y su default.

### 5.2 Aviso de corte

`_budget_bytes` (o su sucesor) deja de cortar en silencio:

```
<tool_result>read_file src/agent_review/worker/main.py
…[cortado a 12 000 de 45 701 bytes; vuelve a pedirlo con start/end]</tool_result>
```

Si no se puede servir lo pedido, es preferible un error explícito a media verdad
(el modelo **razona** sobre lo que cree que leyó).

### 5.3 Bloque de estado y caché

Cada turno lleva un bloque compacto con lo ya servido en la review:

```
<tool_result>LECTURAS (esta review): src/agent_review/worker/main.py [1-1179],
src/agent_review/ports/llm.py [1-120], config/models.yaml [1-192]</tool_result>
```

Coste: decenas de tokens. Dueño: el `LlmReviewer` (vida de la review), no el bucle
(vida del lote), porque es donde se repiten las lecturas. El caché de E/S va en un
**decorador** sobre `ContextReaderPort` (`CachingReader`), no en el puerto: el
puerto y sus tres adaptadores no se tocan.

### 5.4 Variables nuevas

| Variable | Default propuesto | Qué acota |
|---|---|---|
| `REVIEW_TOOLS_GREP_MODE` | `literal` | `literal` \| `regex` por defecto en el bucle |
| `REVIEW_TOOLS_READ_OUTLINE` | `off` | habilita `mode="outline"` |
| `REVIEW_TOOLS_READ_STATE` | `off` | bloque de lecturas de 5.3 |
| `REVIEW_TOOLS_TRUNCATION_NOTICE` | `on` | aviso de corte de 5.2 |
| `REVIEW_TOOLS_STRICT_CALLS` | `off` | no descartar en silencio: error explícito por petición no atendida |
| `REVIEW_TOOLS_CALLS_PER_TURN` | `3` | la palanca E7 (3 contra 4–5) |
| `REVIEW_TOOLS_PRECACHE_CONTEXT` | `on` | L1 precargado en el bucle (E8); `off` = el bucle lee lo que necesita |
| `REVIEW_TOOLS_IO_CACHE` | `off` | caché de E/S por review (E9) |

## 6. Riesgos

| # | Riesgo | Mitigación |
|---|---|---|
| R1 | **La medida no mide lo que cree.** Con el matcher anclado a redacción, dos corridas del mismo defecto puntúan distinto; el `recall` de la variante con herramientas (0.125) es hoy un artefacto | **Antes de medir** (bloque 0): T1 cambia el ancla del matcher a ubicación/identidad con su ADR y enmienda la spec de 006, y T2 reancla las etiquetas del *gold set* y refija el *baseline* |
| R2 | **D23**: una grabación caducada se lee como recall 0 en verde | E0; `make eval-tools` debe fallar ruidosamente si el *fingerprint* no casa |
| R3 | **Regex caro** (R4 de 016): un patrón que explota en el `git grep` del runner | Default `literal`; `mode: "regex"` explícito, acotado por `path`/`glob` y con tope de coincidencias y de tiempo |
| R4 | **`outline` opina mal** en lenguajes sin parser (YAML, Dockerfile) | Índice por AST donde exista y por heurística declarada donde no; si duda, cae a `full` |
| R5 | **Más avisos, más *prompt*** (E4, E9): el bloque de estado viaja en cada turno | Bloques acotados (decenas de tokens) y medidos con 006; cada uno detrás de su flag |
| R6 | **Invalidación de grabaciones**: cada flag cambiar el *prompt* obliga a regrabar | Es el comportamiento de [ADR-0017](../../adr/0017-replay-determinista-con-fingerprint.md); una regrabación por configuración medida, no por mejora acumulada |
| R7 | **La mejora no se nota** y el bucle sigue igual de caro | La entrega se juzga por el informe de cada flag contra el *baseline*, no por el número de mejoras |

## 7. Criterios de hecho

1. `grep` con `mode="regex"` casa `complete\(|model_for\(` en el *gold set* y en el
   *fixture*; con `mode` ausente el resultado es idéntico a hoy (test).
2. Un `grep` acotado por `path`/`glob` no devuelve coincidencias de fuera; con
   `files_only` devuelve solo rutas (test).
3. Un `read_file` cortado por presupuesto lleva su aviso de corte con el tamaño
   real y el rango servido (test).
4. `read_file(mode="outline")` devuelve los símbolos con su rango y una lectura
   posterior del rango devuelve el cuerpo (test).
5. El bloque de estado aparece en el turno siguiente a una lectura y no crece sin
   límite (test).
6. Con `REVIEW_TOOLS_STRICT_CALLS`, una petición de más recibe error explícito y
   **no** se descarta en silencio (test).
7. Con todos los flags en su default, el comportamiento es el de hoy (test de
   regresión, criterio 1 de 014).
8. El informe de 006 con cada flag, contra el *baseline*, queda anotado en el
   `todo.md` (o en la spec): *prompt*, *completion* y recall por configuración.
9. **D23 resuelto y verificado**: `make eval-tools` con una grabación caducada
   termina en error, no en un informe con recall 0 (test).
9b. **El matcher mide defecto**: dos redacciones distintas del mismo defecto casan
   la **misma** etiqueta (test sintético); las etiquetas del *gold set* del PR #3
   reancladas dan un informe **estable** y el *baseline* queda refijado (T2).
10. `make ci` y `make docs-check` en verde; documentos vivos al día
    ([`arquitectura.md`](../../arquitectura.md) §7 si D23 se cierra,
    [`operacion.md`](../../operacion.md) §2 con las variables de §5.4).

## 8. Preguntas abiertas

- **Q1 — ¿El *prompt* por rol entra aquí o es su propia iniciativa?**
  *Resuelta (2026-10-17):* **su propia iniciativa**. D20 está registrada «sin
  iniciativa»: `correctness`, `security` y `arch` comparten `_SYSTEM` y el hallazgo
  se prefija `correctness` para las tres. Es una carencia real, pero el *harness*
  solo corre `correctness`, así que especializar `security`/`arch` **no se puede
  medir** con el *gold set* de hoy. La vía es la que no engorda esta spec: hacer el
  *prompt* **declarativo por rol** (como `config/models.yaml` declara el modelo por
  rol) con su caso de *gold set* propio.
- **Q2 — ¿`outline` necesita el runner o basta el `VcsContextReader`?** Depende de
  dónde se construya el índice (AST en memoria sobre los bytes ya servidos, o en el
  workspace). Decide si `outline` funciona en la degradación de 014 H3.
- **Q3 — ¿El tope de peticiones por turno se mide con 3 y 4–5, o se fija por
  evidencia previa?** Recomendación: medir las dos; es la palanca que más fácil se
  supone y la que más fácil engorda el *prompt*.
- **Q4 — ¿`REVIEW_TOOLS_PRECACHE_CONTEXT=off` (E8) sube o baja el recall?** Es la
  pregunta de 014 Q3, ahora con número. Si baja el recall más de lo que baja el
  coste, se queda en `on` y el ahorro hay que buscarlo en el fan-out.
