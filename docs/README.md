Última modificación: 2026-10-23 | Creado: 2026-10-11

# Documentación

Índice de la documentación de Agent Review LangGraph. Las reglas de estructura y de
mantenimiento están en [`.agents/rules/`](../.agents/rules/documentacion.md) y se
comprueban con `make docs-check`.

## Por dónde empezar

| Si quieres… | Lee |
|---|---|
| Saber qué es y levantarlo la primera vez | [`README.md`](../README.md) |
| Entender el vocabulario | [`CONTEXT.md`](../CONTEXT.md) |
| Saber cómo es el sistema hoy | [`arquitectura.md`](arquitectura.md) |
| Arrancarlo, configurarlo o diagnosticarlo | [`operacion.md`](operacion.md) |
| Cambiarlo o extenderlo | [`desarrollo.md`](desarrollo.md) |
| Saber por qué es así | [`adr/`](adr/README.md) |
| Ver qué se está construyendo y su estado | [`iniciativas/`](iniciativas/README.md) |
| Aprender LangGraph con este repo | [`edu/`](edu/README.md) |

## Mapa

```
CONTEXT.md            glosario
docs/
  arquitectura.md     vivo: componentes, grafo, flujos, estado por fase, deuda
  operacion.md        vivo: arranque, configuración, secretos, CI, API/CLI, smoke
  desarrollo.md       vivo: tests, extensión, invariantes, convenciones
  adr/                decisiones (NNNN-slug.md)
  iniciativas/        NNN-slug/{spec,plan,todo}.md + archivo/
  edu/                serie didáctica (NN-slug.md)
```
