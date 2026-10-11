# ADR-0006 — Inline primero, resumen con marca al final, sin compensación

- **Estado:** Aceptado (el formato visual sigue pendiente del visto bueno humano, P3)
- **Fecha:** 2026-10-11

## Contexto

El valor de una review automática está en el sitio del cambio, no en un muro de
Markdown. Publicar en un PR no es atómico: el proceso puede morir entre el último
inline y el resumen. Un comentario anclado a una línea equivocada es peor que no
anclarlo.

## Decisión

**Orden fijo:**

1. Un comentario **inline** por hallazgo anclado, por orden de severidad (P0→P3) y
   como máximo `max_inline_comments` (20). Cada uno lleva su marca inline.
2. Un **resumen** con la marca de ejecución `:ok` o `:partial`. Es la **última**
   escritura.
3. El aviso de **Teams** sale de un nodo `notify` aparte. Un fallo de Teams no repite
   `publish`, y un fallo no transitorio no tumba una review ya publicada.

Además:

- **No hay compensación.** Si la ejecución muere tras los inline, el reintento no los
  repite (los reconoce por su marca) y escribe el resumen que faltaba. No se borran
  comentarios que alguien podría estar leyendo.
- **El anclaje nunca se inventa.** `resolve_inline_anchor` devuelve una línea del
  diff (lado nuevo) o `None`, y con `None` el hallazgo va al resumen. Bitbucket lo
  resuelve en servidor (`twg prs diff-line`); GitHub, en el cliente sobre el diff.
- **Los borrados puros van al resumen.** No se ancla con `--from-line`: el lado viejo
  es otro contrato y un ancla mal resuelta engaña. `from_line` existe en el puerto pero
  no se usa.
- Un hallazgo `rejected` por una persona va solo al resumen.

## Consecuencias

- Plantilla actual: cabecera `**[Px] título**`, `why_it_matters` (sin repetirlo si el
  título es su truncado), `_Accion: … · confianza N_`, línea de política solo si la
  razón aporta, línea de decisión humana y bloque de fix o sugerencia. Texto en
  español; rutas y símbolos tal cual; sin mencionar al autor.
- El aviso de pausa **nunca** lleva marca: si la llevara, `publish` lo leería como
  review publicada y no publicaría nada al reanudar.
- Si no se pueden leer las marcas del PR, se publica igual: mejor un duplicado que una
  review perdida.
- `list_review_markers` lee hasta 200 comentarios en Bitbucket. En PRs más largos,
  las marcas antiguas dejan de verse.

## Alternativas descartadas

- **Resumen primero.** Una marca `:ok` sin inline haría que el reintento no
  completara nada.
- **Borrar los inline huérfanos.** Es destructivo y no hace falta.
