Última modificación: 2026-10-24 | Creado: 2026-10-24

# Todo: Panel en React

Checklist accionable. El diseño está en [`spec.md`](spec.md); el orden, en
[`plan.md`](plan.md). **A y S pueden ir en paralelo**: no comparten ficheros.

## Bloque E — Esqueleto (checkpoint E-K)

- [x] **E1** `frontend/` con Vite, React 19 y TS estricto; `package-lock.json` versionado
- [x] **E2** Armazón de CoreUI recortado: layout, sidebar, navbar y login como base
- [x] **E3** `base: '/'` y `BrowserRouter`; fuera `HashRouter` y `useColorModes`
- [x] **E4** Tema oscuro fijo y textos en español
- [x] **E5** Borrar las views de demo, los gráficos y sus assets (no se copiaron)
- [x] **E6** `NOTICE` con la licencia MIT de CoreUI
- [x] **E7** `.gitignore` para `node_modules/` y `frontend/dist/`
- [x] **E8** `make frontend-check` y `make frontend-build`

## Bloque S — Sesión (checkpoint S-K)

- [x] **S1** `GET /api/session` con `{user, role}`
- [x] **S2** `POST /api/session`: valida el secreto y deja la cookie `HttpOnly`
- [x] **S3** `DELETE /api/session`: borra la cookie
- [x] **S4** Guardia de rutas: sin sesión, `/login`
- [x] **S5** Pantalla de login con un solo campo
- [x] **S6** El rol del cliente sale de `GET /api/session`; `ROLE_COOKIE` se borra
- [x] **S7** Tests de S1–S3 en Python, con `TestClient`
- [x] **S8** `/claude`: formulario del token del CLI, solo `operador`

## Bloque A — API de la ficha (checkpoint A-K)

- [x] **A1** `GET /api/reviews/{review_key}`: traza, pausa, `notes`, `journal` y disponibilidad
- [x] **A2** `404` cuando no hay traza, log ni descarte
- [x] **A3** `GET /api/config` añade `pins`
- [x] **A4** Tests de A1–A3 en Python

## Bloque D — Cuadro de mando (checkpoint D-K)

- [x] **D1** Las cuatro cifras: en cola, en vuelo, esperando persona, descartes
- [x] **D2** Secciones de cola, en vuelo, pausas, descartes y presupuesto
- [x] **D3** Lanzar review contra `POST /api/launch`
- [x] **D4** Cancelar, reintentar y olvidar con confirmación y *toast*
- [x] **D5** Invalidación de la caché tras cada comando

## Bloque F — Ficha (checkpoint F-K)

- [x] **F1** Recorrido de nodos con su duración
- [x] **F2** Pasos del CLI de Claude cuando `REVIEW_ENGINE=claude`
- [x] **F3** Log a partir de `notes` + `journal`, sin `dangerouslySetInnerHTML`
- [x] **F4** Formulario de decisión
- [x] **F5** Consola del sandbox por SSE
- [x] **F6** Razonamiento por SSE
- [x] **F7** Actividad de Claude por SSE y descarga
- [x] **F8** Ficha de un descarte sin recorrido

## Bloque B — Bandeja e histórico (checkpoint B-K)

- [x] **B1** Bandeja con tarjetas por pausa y respuesta por hallazgo o lote
- [x] **B2** Aviso de pendientes en el sidebar
- [x] **B3** Histórico con filtros `repo` / `pr_id`
- [x] **B4** Paginación en cliente (Q5)

## Bloque C — Configuración (checkpoint C-K)

- [x] **C1** Secciones y control por dial con su ayuda
- [x] **C2** **Restaurar** por control, deshabilitado cuando no hay cambio
- [x] **C3** Guardar con revisión y conflicto `409` legible
- [x] **C4** Candado `PANEL_CONFIG_LOCK` y revisión fijada por review en curso

## Bloque R — Retirada (checkpoint R-K)

- [ ] **R1** Borrar `src/agent_review/web/ui.py` y sus constantes de CSS y JS
- [ ] **R2** Borrar `PANEL_UI` y `ROLE_COOKIE`
- [ ] **R3** Retirar `tests/test_web_ui.py` y sustituirlo por tests de API y componente
- [ ] **R4** Test de que cada ruta de `spec.md` §3.3 sirve `index.html`
- [ ] **R5** Test de que `GET /api/inexistente` sigue siendo `404`
- [ ] **R6** Etapa de Node en el `Dockerfile`; la imagen final sin Node
- [ ] **R7** `make ci` incluye `frontend-check`
- [ ] **R8** `docs/operacion.md` §5: rutas nuevas y el login
- [ ] **R9** `docs/arquitectura.md` §2: el `web` sirve la SPA
- [ ] **R10** Spec a `implementada` e índice al día
