---
description: Cuándo y qué documentación actualizar al cambiar código, configuración o decisiones (docs as code)
globs:
alwaysApply: true
---

# Documentación: mantenerla al día

La documentación es parte del cambio, no una tarea posterior. Un cambio **no está
terminado** hasta que la documentación que describe lo que tocó dice la verdad. La
estructura y la nomenclatura están en la regla `documentacion`.

## 1. Reglas de oro

1. **Mismo commit.** Código y documentación que lo describe cambian juntos. Nada de
   «ya actualizo los docs luego».
2. **Los documentos vivos describen el presente.** `arquitectura.md`,
   `operacion.md`, `desarrollo.md` y `CONTEXT.md` cuentan cómo es el sistema hoy. La
   historia está en git, en los ADR y en las specs.
3. **Fuente única.** Si un dato ya está documentado en otro sitio, enlaza; no lo
   copies. Si encuentras dos versiones que discrepan, corrige la equivocada y deja
   solo un enlace.
4. **Borra lo obsoleto.** Un párrafo que ya no es verdad se corrige o se elimina; no
   se tacha ni se marca «obsoleto».
5. **Actualiza la cabecera.** Al editar un documento, pon la fecha de hoy en
   `Última modificación`.
6. **Antes de terminar**, ejecuta `make docs-check` (va incluido en `make ci`).

## 2. Si cambias X, actualiza Y

| Cambias… | Actualiza… |
|---|---|
| Un campo de `settings.py` | `.env.example` (comentado) y la tabla de variables de `docs/operacion.md` §2 |
| Un secreto o su obligatoriedad (`insecure_secrets`) | `docs/operacion.md` §3 (contrato y rotación) y `.env.example` |
| Un endpoint de la API, el CLI o sus códigos de respuesta | `docs/operacion.md` §5 |
| `docker-compose.yml`, el `Dockerfile` o los scripts de `scripts/` | `docs/operacion.md` §1 |
| Nodos, aristas, estado o reducers del grafo | `docs/arquitectura.md` §3–4 |
| Claves de Redis, TTL o almacenes | `docs/arquitectura.md` §5 |
| La semántica de `config/autonomy.yaml` o del motor de política | El ADR de política correspondiente (o uno nuevo) y `CONTEXT.md` |
| El perfil del repo o las recetas del sandbox | `docs/adr/0011-…`, `docs/operacion.md` §2 si afecta a la configuración |
| Una decisión de diseño con alternativas | Un ADR nuevo; si sustituye a otro, marca el viejo `Sustituido por` |
| Un concepto nuevo o un término que se usa con otro sentido | `CONTEXT.md` |
| Una deuda descubierta, resuelta o una discrepancia spec↔código | `docs/arquitectura.md` §7 (añadir, o quitar la fila al resolverla) |
| Una tarea cerrada | `[x]` en el `todo.md` de su iniciativa, en el mismo commit |
| El estado de una iniciativa o de una fase | `docs/iniciativas/README.md` y `docs/arquitectura.md` §6 |
| Una zona nueva de tests | `docs/desarrollo.md` §2 |
| Una forma nueva de extender el sistema (puerto, adaptador, persona) | `docs/desarrollo.md` §3 |
| Un invariante | `docs/desarrollo.md` §4 y, si nace de una decisión, su ADR |
| Una ruta de documento | Todos los enlaces que apuntan a ella (`make docs-check` los detecta) |

## 3. Cuando el código y la spec discrepan

La spec es la intención y el código, la realidad. Si no coinciden:

- Si el código tiene razón (la spec quedó atrás), anota la enmienda en la spec, con
  fecha, y actualiza los documentos vivos.
- Si la spec tiene razón (el código está mal), es un bug o una deuda: ve a
  `docs/arquitectura.md` §7 o a una tarea del `todo.md`.
- Si no está claro cuál manda, **pregunta**. No lo resuelvas en silencio.

## 4. Revisión

Al revisar un cambio (propio o ajeno), comprueba también:

- ¿Algún documento vivo dice algo que este cambio vuelve falso?
- ¿Hay una decisión implícita que merezca un ADR?
- ¿Aparece un término que no está en `CONTEXT.md`?
- ¿Se ha marcado la tarea en su `todo.md`?
- ¿`make docs-check` está en verde?
