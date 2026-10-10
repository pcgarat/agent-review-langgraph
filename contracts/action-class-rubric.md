Última modificación: 2026-10-10 | Creado: 2026-10-10

# Rúbrica de clases de acción

Cómo clasificar un hallazgo. Es la entrada de la política; equivocarse aquí degrada
todo lo demás.

## `severity`

| Valor | Cuándo |
|---|---|
| `P0` | Corrupción de datos, caída en producción, brecha activa, pérdida de dinero. |
| `P1` | Bug funcional garantizado en el camino principal; fallo de seguridad explotable. |
| `P2` | Bug en camino secundario, deuda que romperá pronto, test que enmascara un fallo. |
| `P3` | Estilo, nombres, micro-refactor, documentación. |

## `autofix_class`

| Valor | Significado | Efecto en la política |
|---|---|---|
| `gated_auto` | Arreglo mecánico y verificable por tests. | Puede optar a `fix_with_approval`/`fix_auto`. |
| `manual` | Requiere criterio humano (diseño, negocio). | Siempre `escalate`/`report`. |
| `advisory` | Solo informar; no hay arreglo claro. | Siempre `report`. |

## `confidence` (anclado)

| Valor | Significado |
|---|---|
| `100` | Demostrable con la evidencia citada. |
| `75` | Muy probable; el patrón se repite en el repo. |
| `50` | Sospecha razonada; falta contexto. |
| `25` | Intuición; no publicar como acción. |
| `0` | Ruido. |

Publicar a partir de `>=75`. Por debajo, el hallazgo se guarda como memoria, no como
comentario accionable.

## `owner`

- `downstream-resolver`: el propio autor del PR puede resolverlo.
- `human`: necesita a una persona con contexto que el agente no tiene.
- `release`: depende de un despliegue o proceso de release.

## Regla de oro

Si dudas entre dos severidades, elige la menor **pero** deja la duda escrita en
`why_it_matters`. Un falso P0 quema la confianza en el agente más rápido que diez P3
ignorados.
