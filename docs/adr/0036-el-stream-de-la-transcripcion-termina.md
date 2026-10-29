# ADR-0036 — El stream de la transcripción siempre termina

- **Estado:** Aceptado
- **Fecha:** 2026-10-29

## Contexto

`transcript_events` servía la conversación del CLI por SSE con una condición de
salida única: el fichero de estado del commit tenía que traer `finished_at`. Ese
campo lo escribe el runner al terminar el subproceso.

Hay dos caminos en los que nunca llega:

1. **El runner muere antes de escribir `finished_at`** (reinicio, OOM, contenedor
   recreado). El fichero se queda con `started_at` y sin cierre.
2. **La clave apunta a un commit sin actividad**, o el fichero de estado existe
   pero el log no. El bucle espera un cierre que nadie va a producir.

En los dos casos el generador gira para siempre dentro de un `StreamingResponse`
**síncrono**. FastAPI lo ejecuta en un hilo del threadpool, y como el bucle no
emite nada, el hilo no se libera ni al desconectarse el cliente: no hay `yield`
que dé la oportunidad de detectar el cierre. Unas pocas decenas de peticiones a
`/api/reviews/<cualquiera>/claude/events` agotan el threadpool y dejan el panel
entero sin servir, incluidos los endpoints que no tienen nada que ver.

La ruta solo comprobaba que la clave identificase un commit. Cualquier sesión del
panel podía provocarlo, y sin `PANEL_SESSION_SECRET` cualquiera.

## Decisión

**El stream cierra siempre, por tres vías, y la ruta no abre un stream vacío.**

1. **Sin transcripción, `404`.** Antes de construir el `StreamingResponse` se
   comprueba que el log existe **y tiene contenido** (el runner lo crea vacío en
   `start()`). Es el mismo criterio que ya usa la consola del sandbox, y el
   motivo es el mismo: un `200` sin eventos hace que `EventSource` reintente sin
   fin, mientras que un `404` lo rinde.
2. **`finished_at`, `end`.** Como antes.
3. **Inactividad, `end`.** Sin una línea nueva durante `IDLE_S` (180 s) se cierra,
   y solo se cierra si además el estado no dice «en marcha» o si el stream lleva
   ese mismo tiempo abierto.

La inactividad se mide **desde que este stream empezó**, no por la antigüedad del
fichero de estado. El runner escribe ese fichero al arrancar y no lo vuelve a
tocar hasta el final, así que su `mtime` no dice nada mientras la review avanza:
usarlo habría cortado reviews vivas y largas.

## Consecuencias

- El threadpool no se puede agotar con este endpoint. El peor caso es un hilo
  retenido 180 s.
- Una review que no escribe nada durante 180 s pierde el directo y el panel ve
  «Transmisión terminada». Es aceptable: un CLI que lleva tres minutos sin una
  sola línea no está revisando.
- `events_available` pasa a significar «hay contenido que seguir», no «existe el
  fichero». La ficha deja de ofrecer el botón de descarga de un log vacío por la
  misma razón.
- La cabecera `Last-Event-ID` (ADR anterior, mismo día) sigue funcionando: la
  reanudación arranca en el byte pedido y el reloj de inactividad es nuevo.

## Alternativas descartadas

1. **Solo el `404` de la ruta.** Cierra el caso de la clave inventada, pero no el
   del runner muerto a mitad, que es el que ocurrió de verdad.
2. **Convertir el generador en asíncrono y usar `asyncio.sleep`.** Libera el hilo
   mientras espera, pero no cierra el stream: la conexión seguiría abierta para
   siempre y el bucle, girando. Arregla el síntoma (hilos) y deja el defecto
   (nunca termina). Además obliga a leer ficheros con `to_thread` en cada vuelta.
3. **Un `timeout` global del `StreamingResponse`.** No existe como opción; el
   corte lo decide el cliente. Y no distingue una review larga y viva de un
   cuelgue.
4. **Acotar por la antigüedad del fichero de estado.** Es lo primero que probé y
   es incorrecto: el fichero no se reescribe mientras corre, así que una review
   legítima de una hora se cortaría.
