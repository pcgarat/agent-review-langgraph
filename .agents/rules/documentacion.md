---
description: Estructura de carpetas, nomenclatura y ciclo de vida de la documentación, specs, planes y todos
globs:
alwaysApply: true
---

# Documentación: estructura, nomenclatura y metodología

Fuente única: `.agents/rules/documentacion.md`. `.cursor/rules/documentacion.mdc` y
`.claude/rules/documentacion.md` son enlaces simbólicos a este fichero: edítalo aquí,
nunca en los enlaces. La comprobación automática es `make docs-check`
(`scripts/check_docs.py`), que forma parte de `make ci`.

## 1. Dónde vive cada cosa

```
README.md                      Puerta de entrada: qué es, arranque rápido, enlaces
CONTEXT.md                     Glosario del dominio (lenguaje ubicuo)
docs/
  README.md                    Índice de toda la documentación
  arquitectura.md              Documento vivo: cómo es el sistema HOY
  operacion.md                 Documento vivo: arrancar, configurar, operar, diagnosticar
  desarrollo.md                Documento vivo: tests, cómo extender, invariantes
  adr/
    README.md                  Índice de decisiones
    NNNN-slug.md               Una decisión por fichero
  iniciativas/
    README.md                  Índice y estado de todas las iniciativas
    NNN-slug/
      spec.md                  QUÉ y POR QUÉ (obligatorio)
      plan.md                  CÓMO: orden, dependencias, checkpoints
      todo.md                  Checklist accionable
    archivo/NNN-slug/          Iniciativas cerradas sin pendientes
  edu/
    README.md                  Índice de la serie didáctica
    NN-slug.md
.agents/rules/                 Reglas para agentes (enlazadas desde .cursor y .claude)
```

**Excepciones permitidas fuera de `docs/`:**

- `README.md` de una carpeta de código (`bin/`, `contracts/`) que explica esa carpeta.
- Contratos versionados (`contracts/*.md`).
- Skills de agentes (`.agents/skills/**`).
- Copia de la skill del runner (`image/claude/skills/**`). No se edita: la imagen
  la vuelca tal cual.

**Prohibido:**

- Markdown suelto en la raíz o en `docs/` fuera de lo listado.
- Carpetas `tasks/`, `trash/`, `notas/`, `old/` o `tmp/` versionadas.
- Copias «v2» o «final» de un documento.

## 2. Nomenclatura

| Tipo | Patrón | Ejemplo |
|---|---|---|
| ADR | `docs/adr/NNNN-slug.md` (4 dígitos) | `0015-cola-de-fixes-separada.md` |
| Iniciativa | `docs/iniciativas/NNN-slug/` (3 dígitos) | `005-fase4-autonomia/` |
| Ficheros de iniciativa | exactamente `spec.md`, `plan.md`, `todo.md` | — |
| Didáctico | `docs/edu/NN-slug.md` (2 dígitos) | `11-subgrafos.md` |
| Documento vivo | `docs/<tema>.md`, un sustantivo | `observabilidad.md` |

- **slug:** kebab-case, en español, sin tildes ni `ñ`, ≤ 6 palabras, describe el
  contenido («ingesta-diferida», no «notas-reunion»).
- **Sin fechas en el nombre del fichero.** La fecha va en la cabecera y en git. Los
  nombres son estables para que los enlaces no se rompan.
- **Numeración:** siguiente número libre. Un número nunca se reutiliza, aunque se
  borre o se archive.

## 3. Formato

- Primera línea de todo documento de `docs/` (salvo ADR) y de `CONTEXT.md`:
  `Última modificación: YYYY-MM-DD | Creado: YYYY-MM-DD`. Después, una línea en
  blanco y `# Título`.
- **ADR:** `# ADR-NNNN — Título`, y debajo `- **Estado:** …` y `- **Fecha:** …`.
  Secciones: Contexto, Decisión, Consecuencias, Alternativas descartadas.
  - Estados: `Propuesto`, `Aceptado`, `Sustituido por ADR-NNNN`, `Retirado`.
  - Un ADR aceptado **no se reescribe**. Si la decisión cambia, se crea uno nuevo y
    el viejo pasa a `Sustituido por`. Un matiz o corrección menor sí se edita, con
    fecha.
- **spec.md:** debajo del título, `> **Estado:** borrador | aprobada | implementada |
  sustituida por NNN`. Secciones mínimas: Objetivo, No-objetivos, Decisiones, Diseño o
  contrato, Riesgos, Criterios de hecho, Preguntas abiertas.
- **plan.md:** Overview, Decisiones de implementación, Orden y dependencias,
  Checkpoints, Riesgos.
- **todo.md:** tareas `- [ ] **ID** Título`, con ID estable dentro del repo (`T1`,
  `F14`, `P6`…), agrupadas por bloques con su checkpoint. Una tarea cerrada se marca
  `[x]`, no se borra.
- Enlaces **relativos** entre documentos (por ejemplo `../adr/0004-slug.md`). Rutas de código
  entre comillas invertidas. Diagramas en Mermaid.
- Idioma: español con tildes en la prosa; identificadores, rutas y comandos tal cual.

## 4. Metodología: ciclo de vida de una iniciativa

Todo cambio que no sea trivial (más de un fichero de código con lógica nueva, un
contrato, un comportamiento visible o una decisión de diseño) es una iniciativa.

1. **Spec.** Crea `docs/iniciativas/NNN-slug/spec.md` con `Estado: borrador` y añade
   la fila al índice `docs/iniciativas/README.md`. **No se escribe código hasta que
   la spec esté `aprobada`** por una persona. Las dudas van a «Preguntas abiertas»; si
   bloquean, se pregunta.
2. **Plan y todo.** Con la spec aprobada, escribe `plan.md` (cómo y en qué orden) y
   `todo.md` (tareas con ID y checkpoints).
3. **Implementación.** Cada tarea cerrada se marca en `todo.md` **en el mismo commit**
   que el código. Una decisión de arquitectura que surja va a un ADR; un término
   nuevo, a `CONTEXT.md`.
4. **Cierre.** Para dar una iniciativa por terminada:
   - Los documentos vivos describen el sistema resultante (ver
     `documentacion-mantenimiento`).
   - La spec pasa a `implementada`.
   - El índice muestra el estado final.
5. **Archivo.** Cuando no le quedan tareas abiertas, incluidas las humanas, se mueve
   con `git mv` a `docs/iniciativas/archivo/NNN-slug/` y se actualizan sus enlaces.
6. **Borrado.** Se borra (con `git rm`, la historia queda en git) lo que ya no aporta:
   notas de trabajo superadas, auditorías cuyo contenido vive en un documento vivo o
   en un ADR, duplicados. Nunca se deja un documento marcado como «obsoleto».

## 5. Qué va en cada sitio (y qué no)

| Pregunta | Dónde |
|---|---|
| ¿Qué significa este término? | `CONTEXT.md` |
| ¿Por qué se decidió así? | `docs/adr/` |
| ¿Cómo es el sistema ahora? | `docs/arquitectura.md` |
| ¿Cómo lo arranco, configuro o diagnostico? | `docs/operacion.md` |
| ¿Cómo lo extiendo o lo pruebo? | `docs/desarrollo.md` |
| ¿Qué vamos a construir y por qué? | `iniciativas/NNN/spec.md` |
| ¿En qué orden y cómo? | `iniciativas/NNN/plan.md` |
| ¿Qué falta por hacer? | `iniciativas/NNN/todo.md` |
| ¿En qué estado está todo? | `docs/iniciativas/README.md` |
| ¿Cómo funciona LangGraph aquí? | `docs/edu/` |

Una información vive en **un** sitio; los demás enlazan. Si te encuentras copiando un
párrafo entre documentos, enlaza en su lugar.
