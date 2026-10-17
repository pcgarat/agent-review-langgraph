Última modificación: 2026-10-17 | Creado: 2026-10-16

# Plan: Evaluación de la review (gold set y métricas)

Medir antes de arreglar. El *qué y por qué* vive en
[`spec.md`](spec.md); las tareas, en [`todo.md`](todo.md). Este plan fija el **orden**,
las dependencias y los checkpoints. No se toca prompt, contrato ni política: el harness
**lee** el grafo real con puertos inyectados (E7/E8 de la spec).

## Overview

Un caso versionado (`tests/eval/pr_3/`) con el diff del PR #3, sus etiquetas y las
respuestas grabadas del modelo; un `harness.py` que carga el caso, corre el grafo real
con un VCS falso y un `LlmPort` de *replay*, atribuye hallazgos 1:1 contra las
etiquetas y emite un informe JSON con seis métricas; y una comparación por
**dominancia** que dice mejora / empeora / no concluyente. El informe se congela como
`expected_report.json` = baseline.

## Decisiones de implementación

- **Caso como directorio versionado** (E2). Un único caso (`pr_3`) con estructura
  `labels.yaml` + `diff.patch` + `recorded.json` + `expected_report.json`, para que
  añadir un caso nuevo sea copiar la carpeta.
- **El harness corre el grafo real** (`build_graph`) con `FakeVcs` y un puerto LLM
  inyectado; lo único que cambia entre *replay* y real es el `LlmPort` (E7).
- **«Publicado» desde el `FakeVcs`** (E12): el harness casa la marca inline
  determinista (`_inline_token` = `base32(sha256(finding_id))[:8]`) y las líneas del
  resumen, y lo contrasta con `merged`. No parsea prosa.
- **Cobertura instrumentada** (E11): se añade a `LlmReviewer` un campo de solo lectura
  (`last_seen_files`) con los ficheros que realmente entraron en el prompt.
- **Fingerprint por rol** (E4): el `replay` aborta si el fingerprint del prompt —que
  incluye el rol y el `max_findings`— o del caso no coincide con la grabación.
- **Determinismo del informe** (E6 de la spec): claves ordenadas y floats redondeados
  a 4 decimales para que dos corridas den el JSON byte a byte.
- **Tooling aparte** (E14): precisión y recall globales excluyen `subset: tooling`;
  ese subconjunto se reporta por separado.
- **`must_mention` no castiga** (E16): un `expected` casado pero invertido va a
  `inverted` y no mueve precisión ni recall.
- **Sin paquete en `tests/`** (spec §5.1): el test importa `harness` como módulo
  hermano; `scripts/eval.py` añade `tests/eval` a `sys.path`.

## Dependency graph

```
E1  esqueleto del harness + carga del caso + tipos del informe
     ├── E2  matcher 1:1 determinista
     │        └── E3  métricas + JSON determinista
     │                 └── E4  harness.compare (dominancia, 3 veredictos)
     ├── E5  caso pr_3: labels.yaml + diff.patch (captura una vez)
     │        └── E6  LlmPort de replay con fingerprint (por rol y caso)
     │                 └── E7  grabación recorded.json (make eval-record)
     │                          └── E8  test de regresión en CI + baseline congelado
     └── E9  CLI scripts/eval.py + comandos make (eval, eval-live, eval-record)
              └── E10  cobertura instrumentada del LlmReviewer
                       └── E11  corrida real (make eval-live, -m live)
                                └── E12  ADR (E3/E4) + docs vivos
```

E1 es el cimiento (puro y testeable sin el grafo). E5/E6/E7 son datos y el puerto.
E10 (cobertura instrumentada) alimenta el informe pero puede llegar después de la
primera corrida: hasta entonces `coverage` se reporta como `null`, nunca como cero.

## Task list

Ver [`todo.md`](todo.md) (E1–E12). Orden por dependencia: el harness y su matcher
primero, el caso después, la grabación luego, y los docs al final.

## Checkpoints

- **Tras E1–E4:** `harness` carga el caso, atribuye, calcula métricas y compara, todo
  con tests propios; `make ci` en verde.
- **Tras E5–E8:** la corrida *replay* es determinista (dos ejecuciones, mismo JSON) y
  el baseline queda congelado en `expected_report.json`.
- **Tras E9–E11:** `make eval` imprime el informe con el delta; `make eval-live` corre
  contra el modelo real sin bloquear CI.
- **Tras E12:** `coverage` sale de lo que vio el modelo; `make ci` y
  `make docs-check` en verde; ADR de E3/E4 escrito y D14 actualizado.

## Riesgos

| Riesgo | Mitigación |
|---|---|
| El *gold set* de un caso se sobreajusta al PR #3 | Sesgo declarado en el informe (`case`); 007+ no acepta una mejora sostenida solo por este caso |
| El matcher decide el número | Reglas conjuntivas cerradas, atribución 1:1 determinista y tests propios del matcher |
| Grabación obsoleta mide el pasado | El fingerprint (prompt por rol + caso) aborta la corrida en CI (E4) |
| La cobertura reconstruida no es fiel | Se instrumenta `LlmReviewer` (E11) en vez de reconstruir; 012 publicará el `ReviewCoverage` |
| Flotantes rompen el «byte a byte» | Redondeo a 4 decimales y claves ordenadas en el informe |
| La corrida real consume tokens (D17) | Un caso, fuera de CI y a mano; en CI coste cero |

## Dependencias externas

- El `diff.patch` del PR #3 se captura una vez (`a8ead73..008da57`; ambos shas existen
  en el repo). No se ejecuta código del PR.
- La corrida real (`eval-live`/`eval-record`) necesita credenciales del proveedor: es
  la única parte que no corre en CI, y es opt-in.
- La cobertura exacta depende del `ReviewCoverage` de
  [012](../012-cobertura-de-la-review/spec.md); hasta entonces E11 da la cota fiel de
  lo que vio el modelo.
