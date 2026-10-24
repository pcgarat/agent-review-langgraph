Última modificación: 2026-10-24 | Creado: 2026-10-24

# Todo: Panel en React

Checklist accionable. El diseño está en [`spec.md`](spec.md); el orden, en
[`plan.md`](plan.md). **A y S pueden ir en paralelo**: no comparten ficheros.

## Bloque E — Esqueleto (checkpoint E-K)

- [ ] **E1** `frontend/` con Vite, React 19 y TS estricto; `package-lock.json` versionado
- [ ] **E2** Armazón de CoreUI recortado: layout, sidebar, navbar y login como base
- [ ] **E3** `base: '/'` y `BrowserRouter`; fuera `HashRouter` y `useColorModes`
- [ ] **E4** Tema oscuro fijo y textos en español
- [ ] **E5** Borrar las views de demo, los gráficos y sus assets
- [ ] **E6** `NOTICE` con la licencia MIT de CoreUI
- [ ] **E7** `.gitignore` para `node_modules/` y `frontend/dist/`
- [ ] **E8** `make frontend-check` y `make frontend-build`

## Bloque S — Sesión (checkpoint S-K)

- [ ] **S1** `GET /api/session` con `{user, role}`
- [ ] **S2** `POST /api/session`: valida el secreto y deja la cookie `HttpOnly`
- [ ] **S3** `DELETE /api/session`: borra la cookie
- [ ] **S4** Guardia de rutas: sin sesión, `/login`
- [ ] **S5** Pantalla de login con un solo campo
- [ ] **S6** El rol del cliente sale de `GET /api/session`; `ROLE_COOKIE` se borra
- [ ] **S7** Tests de S1–S3 en Python, con `TestClient`

## Bloque A — API de la ficha (checkpoint A-K)

- [ ] **A1** `GET /api/reviews/{review_key}`: traza, pausa, `notes`, `journal` y disponibilidad
- [ ] **A2** `404` cuando no hay traza, log ni descarte
- [ ] **A3** `GET /api/config` añade `pins`
- [ ] **A4** Tests de A1–A3 en Python

## Bloque D — Cuadro de mando (checkpoint D-K)

- [ ] **D1** Las cuatro cifras: en cola, en vuelo, esperando persona, descartes
- [ ] **D2** Secciones de cola, en vuelo, pausas, descartes y presupuesto
- [ ] **D3** Lanzar review contra `POST /api/launch`
- [ ] **D4** Cancelar, reintentar y olvidar con confirmación y *toast*
- [ ] **D5** Invalidación de la caché tras cada comando

## Bloque F — Ficha (checkpoint F-K)

- [ ] **F1** Recorrido de nodos con su duración
- [ ] **F2** Pasos del CLI de Claude cuando `REVIEW_ENGINE=claude`
- [ ] **F3** Log a partir de `notes` + `journal`, sin `dangerouslySetInnerHTML`
- [ ] **F4** Formulario de decisión
- [ ] **F5** Consola del sandbox por SSE
- [ ] **F6** Razonamiento por SSE
- [ ] **F7** Actividad de Claude por SSE y descarga
- [ ] **F8** Ficha de un descarte sin recorrido

## Bloque B — Bandeja e histórico (checkpoint B-K)

- [ ] **B1** Bandeja con tarjetas por pausa y respuesta por hallazgo o lote
- [ ] **B2** Aviso de pendientes en el sidebar
- [ ] **B3** Histórico con filtros `repo` / `pr_id`
- [ ] **B4** Paginación en cliente (Q5)

## Bloque C — Configuración (checkpoint C-K)

- [ ] **C1** Secciones y control por dial con su ayuda
- [ ] **C2** **Restaurar** por control, deshabilitado cuando no hay cambio
- [ ] **C3** Guardar con revisión y conflicto `409` legible
- [ ] **C4** Candado `PANEL_CONFIG_LOCK` y revisión fijada por review en curso

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
