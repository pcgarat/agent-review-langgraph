Última modificación: 2026-10-11 | Creado: 2026-10-11

# Decisión: el artefacto del CI es un puente desechable, no la cimentación

Complementa `docs/diseno-agente-code-review-20261010.md` (§9, §15.4),
`docs/ingesta-diferida-ci-20261011.md` y `docs/pendiente-fase1-20261011.md` (A1).
Nace de una pregunta legítima: *«¿para qué ingerir la salida de PHPStan si, cuando
el agente recibe la llamada, ese pipeline ya pasó?»*.

---

## 1. La premisa, matizada

El disparo de la ingesta diferida no es «el pipeline pasó» sino «el step de PHPStan
terminó y volcó su salida» (`capture-on: always`), lo que **incluye el caso rojo**.
Por eso el artefacto a veces sí lleva hallazgos. Pero eso juega a favor de la
intuición, no en contra:

En `emotivoo-v2`, `make stan` y `make deptrac` son checks **bloqueantes** del
pipeline de PR (`config/repo_profile.emotivoo-v2.yaml`, pasos `&stan` / `&deptrac`).

- Pipeline verde ⇒ PHPStan nivel max **sin errores** ⇒ artefacto **vacío**.
- Por tanto, en un PR sano, la persona `standards` sobre el artefacto del CI
  **aporta cero señal nueva**: re-emite la que el CI ya dio.

El único valor marginal del artefacto es anotar inline lo que el CI reporta como
step rojo. Es poco, y no justifica tratarlo como pieza estructural.

## 2. El argumento que sí es fuerte: ejecutar es obligatorio (pero no para revisar)

La verificación **tras un fix** no puede usar artefacto de CI, nunca: el artefacto
es del `head_sha` viejo. Cuando el agente escribe código necesita ejecutar **él**
las recipes sobre el árbol que acaba de tocar (tests dirigidos primero,
`docs/diseno-agente-code-review-20261010.md` §9.5).

Conclusión: la capacidad de ejecutar entra **sí o sí** en la Fase 3. La pregunta
correcta no es «artefacto o ejecución», sino *por qué hay dos abstracciones para la
misma señal* — «salida de una herramienta del repo» — según quién la produzca.

## 3. Forma correcta: un puerto de ejecución, dos adaptadores

Se propone (diseño, aún **no implementado**) un único puerto cuyo resultado sea el
`ExecResult` que §9.4 ya boceta:

```
CheckRunner (port)  run(recipe, repo, rev) -> ExecResult
   ├── SandboxCheckRunner     # ejecuta las recipes del perfil en la imagen del CI (Fase 3)
   └── CiArtifactCheckRunner  # lee el artefacto del CI (puente Fase 1, desechable)
```

`ExecResult` debe llevar al menos `recipe`, `argv`, `exit_code`, `duration_s` y
`stdout`/`stderr`. El contrato actual de `ToolingOutputSource`
(`src/reviews/ports/tooling.py`) devuelve `(status, str)` y **no lleva argv ni
exit code**: se queda corto para auditar una verificación. Converger ambos.
El `CiArtifactCheckRunner` fabrica un `ExecResult` con `argv=[]` y
`exit_code=None`, que es honesto: «esto no lo ejecuté yo».

Beneficios:

- El `ToolingReviewer` no cambia: consume `ExecResult.stdout` en vez de `read()`.
  El transporte se le vuelve invisible.
- El nodo `verify` (Fase 3) usa **el mismo** puerto. Nace una vez, sirve dos veces.
- El puente se puede retirar sin desmontar la casa.

## 4. Decisión (2026-10-11)

1. **El artefacto del CI se mantiene como puente barato de Fase 1** (ya está hecho,
   coste cero, no abre sandbox).
2. **A1 deja de ser bloqueante.** `docs/pendiente-fase1-20261011.md` lo marcaba
   como el interruptor de la Fase 1. Ya no lo es: la Fase 1 entrega valor con las
   personas LLM y los checks que no corren en CI, aunque el `POST` del CI no exista.
3. **No se endurece la ingesta** (`PendingReview`, barrera de N, `ttl_ready` /
   `ttl_pending`) como si fuera permanente. Es andamiaje que caduca con el
   `SandboxCheckRunner`; su complejidad **solo** existe porque la salida llega de
   fuera y llega tarde. Si el agente ejecuta, el disparo vuelve a ser el PR
   (`docs/ingesta-diferida-ci-20261011.md` §3.2 ya lo anticipa).
4. **La inversión de valor va a la ejecución**, no al transporte: personas LLM
   (semántica que la estática no ve), checks fuera de CI (Rector, mutation) y, en
   Fase 3, verificar cambios propios.
5. **El puerto `CheckRunner` se abre al entrar al sandbox** (Fase 3), unificando con
   §9.4. Entre tanto, no se toca `ToolingOutputSource`.

## 5. Consecuencias concretas

| Pieza | Efecto |
|---|---|
| A1 (POST del CI) | Pasa de bloqueante a *nice to have*. Si llega, se usa el puente; si no, la Fase 1 sigue siendo válida. |
| `PendingReview` + barrera + TTL | Se mantienen **tal cual**, congelados: no nuevas features, no más configuración. |
| `expected_tools` duplicado (settings vs perfil) | Deuda que se extingue sola con el puente; no merece trabajo previo. |
| `ToolingOutputSource` | Intacto en Fase 1. Se sustituye por `CheckRunner` en Fase 3. |
| Nodo `verify` (Fase 3) | Nace sobre `CheckRunner`, no sobre una copia de `ToolingOutputSource`. |

## 6. Criterio de retirada del puente

El puente se retira cuando se cumplan **las tres**:

1. Existe `SandboxCheckRunner` ejecutando las recipes del perfil con la imagen del CI.
2. El nodo `verify` (verificación tras fix) ya usa `CheckRunner`.
3. Alguien ha medido que la señal de la estática en modo ejecución es igual o mejor
   que en modo artefacto (hoy, en un PR verde, es idéntica).

Hasta entonces, el artefacto es un ahorro de coste, no una decisión de arquitectura.
