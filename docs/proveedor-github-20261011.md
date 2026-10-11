Última modificación: 2026-10-11 | Creado: 2026-10-11

# Proveedor de VCS: GitHub

Cómo revisar un PR de GitHub sin tocar el grafo. El diseño ya lo prometía: el
grafo solo conoce `VcsPort` (`docs/diseno-agente-code-review-20261010.md`, §Patrones).
Lo que cambia es un adaptador, una clave de settings y el punto de cableado.

## 1. Contrato cumplido

`GithubVcsAdapter` (`src/agent_review/adapters/github_vcs.py`) implementa el
protocolo completo sobre la **API REST v3** con `httpx` (ya era dependencia del
proyecto para el sandbox). No hay CLI: a diferencia de Bitbucket, GitHub se
consume directamente por HTTP.

| Método del puerto | Endpoint / estrategia |
|---|---|
| `get_pull_request` | `GET /repos/{o}/{r}/pulls/{n}` (`head.sha`, `base.ref`, `user.login`) |
| `get_diff` | mismo endpoint con `Accept: application/vnd.github.v3.diff` |
| `get_diffstat` | `GET /repos/{o}/{r}/pulls/{n}/files`, paginado |
| `get_file` | `GET /repos/{o}/{r}/contents/{path}?ref=` (base64 decodificado) |
| `resolve_inline_anchor` | **en cliente**, sobre el diff (ver §2) |
| `post_inline_comment` | `POST /repos/{o}/{r}/pulls/{n}/comments` con `side=RIGHT` |
| `post_comment` | `POST /repos/{o}/{r}/issues/{n}/comments` |
| `list_review_markers` | comments de issue **+** de review, paginados |
| `post_task` | no-op: GitHub no tiene tasks de PR (el grafo no lo llama) |

Clasificación de errores coherente con `errors.is_transient`: 5xx/429 y fallos de
transporte son `GithubUnavailable` (reintentables); 4xx son `GithubError`
(permanentes). El grafo ya sabe reintentar lo primero y no gastar en lo segundo.

## 2. La resolución de ancla, compartida y no delegada

Bitbucket resuelve `path+texto -> línea` en el servidor (`twg prs diff-line`).
GitHub **no tiene** ese endpoint. En vez de duplicar el matching dentro del
adaptador, se extrajo a `diff_parser.resolve_added_line` (junto a `best_line`):
ambos adaptadores comparten el criterio y el contrato del puerto es el mismo en
los dos hosts. Sin coincidencia devuelve `None` y el publish manda el hallazgo al
resumen, en lugar de anclar un comentario a una línea inventada.

El adaptador **cachea el diff por PR**: sin caché, cada hallazgo volvería a
descargar el PR entero para resolver su ancla.

### Notas de fidelidad de la API

- Se pide el media type `...v3.diff` (no el `patch` del JSON): son los mismos
  bytes del fichero y evita el resecalado por caracteres no ASCII.
- Un rango de líneas va en `start_line` (menor) + `line` (mayor), ambos con
  `side=RIGHT`; el `from_line` del puerto mapea a `start_line`.
- `list_review_markers` **pagina** (100/página, tope de 30): una marca o el
  resumen más allá de la primera página ocultaría que la review ya se publicó.

## 3. Configuración

```dotenv
VCS_PROVIDER=github
GITHUB_OWNER=cqm
GITHUB_REPO=emotivoo-v2
GITHUB_TOKEN=...            # PAT fine-grained
GITHUB_API_URL=https://api.github.com   # cambiar en GitHub Enterprise Server
```

- **Credencial:** PAT *fine-grained* con `pull_requests: write` (comentar el PR) y
  `contents: read` (leer el fichero en Fase 3). Para producción, el paso siguiente
  es una **GitHub App** (installation token, sin secreto de larga vida); no entra
  en este cambio.
- `VCS_PROVIDER` se valida al construir `Settings`: un valor desconocido falla en
  el arranque, no en la primera review.
- En producción, `require_production_secrets` exige `GITHUB_TOKEN` cuando el
  proveedor activo es GitHub (y sigue exigiendo `TWG_BBC_TOKEN` con `twg`).
- La API de repo de la ingesta (`_verify_repo`) compara la identidad
  `owner/repo` del proveedor activo: el mismo formato que ya manda el CI.

## 4. Qué NO cambia

El grafo y sus nodos, el contrato de hallazgos, la política, el HITL, el fix, el
sandbox, la ingesta diferida y el dedupe. El marcador `<!-- reviews:... -->` vive
en el cuerpo del comentario, así que la no-republicación funciona igual.

- `docker-compose.yml` no cambia más allá de que el token viaja por `env_file`.
- `scripts/bootstrap-env.sh` comprueba twg solo cuando el proveedor es `twg`.

## 5. Pruebas

- `tests/test_github_vcs.py`: contrato del puerto + mapeo de cada endpoint,
  paginación, clasificación de errores, decodificación base64 y ancla sobre diff.
- `tests/test_worker.py`: `build_vcs` elige GitHub según `VCS_PROVIDER` y falla
  ruidoso sin token.
- `tests/test_api.py`: `_expected_repo` con el proveedor activo, valor de
  `VCS_PROVIDER` inválido y secreto de producción de GitHub.
