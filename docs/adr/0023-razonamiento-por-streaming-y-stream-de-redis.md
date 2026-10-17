# ADR-0023 — El razonamiento de los revisores se captura por *streaming* y viaja por un *stream* de Redis por review

- **Estado:** Aceptado
- **Fecha:** 2026-10-17

## Contexto

Sin razonamiento, el revisor no encontraba nada real en el PR #3 aunque viera todo el
código (cobertura 1.0, 0 TP; [012](../iniciativas/archivo/012-cobertura-de-la-review/spec.md)).
Con razonamiento alto aparecen hallazgos reales, pero cada lote piensa ~20 000 tokens
durante uno a tres minutos, y aparecen tres problemas:

- El proveedor (`nan.builders`) corta con `408` cualquier petición que pase ~116 s sin
  enviar nada, y además tiene un tope de duración total del *stream* de 3–4 minutos.
- El razonamiento se perdía: el adaptador solo leía `content`, aunque el proveedor
  devuelve `reasoning_content`. Nadie podía ver por qué el revisor afirmó algo, ni
  seguir una review de 20 minutos.
- El razonamiento ocupa decenas de KB por lote: no cabe en el estado del grafo ni en
  el *gold set* sin hacerlos crecer sin medida.

## Decisión

- **Streaming siempre** en `OpenAiCompatibleLlm`: la conexión no queda muda y el
  razonamiento llega a trozos. Si un proveedor ignora `stream`, se lee la respuesta
  normal.
- **Un único punto de captura**: el adaptador emite cada trozo por un `on_reasoning`
  opcional y *best-effort* del `LlmPort`; el `LlmReviewer` le añade el contexto
  (persona, lote *i/N*, ficheros) y lo entrega a un `ReasoningSink`.
- **Un *stream* de Redis por review** (`reviews:reasoning:<review_key>`): eventos
  `start`, trozos agrupados, `end` (con `usage`) o `error`, con `MAXLEN ~` (10 MB) y
  TTL (10 días). Lo escribe el worker y lo lee el panel por SSE, en vivo y después.
- **Fuera del estado y del *gold set***: el razonamiento no entra en `ReviewState`, ni
  en el `ReviewOutcome`, ni en el *fingerprint* de las grabaciones.
- **El tamaño de la llamada lo acota el revisor**: lotes de 20 000 caracteres, ficheros
  grandes troceados por hunks y **reintento por lote** ante un corte, en vez de repetir
  la persona entera.

## Consecuencias

- El panel muestra lo que piensa cada persona, lote a lote, mientras revisa; el
  histórico enlaza a cada review y su razonamiento se puede releer 10 días.
- Las trazas de [018](../iniciativas/018-trazas-de-la-review/spec.md) y, más adelante,
  Teams leen del mismo punto de captura; nadie vuelve a parsear el proveedor.
- Una review del PR #3 pasa de minutos a ~21 minutos y de ~105 000 a ~336 000 tokens.
  El presupuesto por review sube a 2 000 000; lo que no cabe sale `budget` en la
  cobertura.
- Un oyente o un Redis caídos nunca cambian la review: el sink es *best-effort*.

## Alternativas descartadas

- **Guardar el razonamiento en el estado del grafo**: el checkpointer y el *replay*
  crecerían decenas de KB por lote, y el panel tendría que leer Postgres en vivo.
- **Guardarlo en Postgres o en Langfuse como fuente para el panel**: sirve para
  analizar después (018), no para verlo en vivo con un SSE barato desde Redis.
- **Pedir sin *streaming* y subir el timeout del cliente**: el corte de ~116 s es del
  *gateway*, no del cliente.
- **Mandar el razonamiento a Teams**: choca con los límites de tamaño y frecuencia y
  expone código en el canal; Teams recibirá un enlace al panel (fase posterior).
