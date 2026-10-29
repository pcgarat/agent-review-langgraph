# ADR-0037 — La skill corre contra un backend que no es Anthropic

- **Estado:** Aceptado (punto 5 sustituido por [ADR-0038](0038-el-cluster-se-espera.md))
- **Fecha:** 2026-10-29

Amplía [ADR-0033](0033-skill-fuera-del-nucleo.md): la jaula deja de ser de un
solo dominio cuando el destino del CLI es el gateway. El valor por defecto
(`api.anthropic.com`) no cambia. Spec:
[032](../iniciativas/archivo/032-skill-con-modelo-de-la-cluster/spec.md).

## Contexto

[ADR-0033](0033-skill-fuera-del-nucleo.md) cierra `Bash` con el sandbox del
CLI y una lista estricta de un solo dominio. Eso asume que el hijo habla con
Anthropic. Para revisar con un modelo del clúster (`nan.builders`) el hijo
sigue siendo Claude Code —mismo argv, mismo `error_max_turns`, mismo objeto
`mode:agent`— pero el clúster solo habla `/v1/chat/completions`.
`https://api.nan.builders/v1/messages` responde 404 (comprobado el
2026-10-29). Hace falta un proceso que traduzca.

El *gold set* mide el motor `llm` ([ADR-0017](0017-replay-determinista-con-fingerprint.md)).
No es el sitio donde se juzga si la skill aguanta otro modelo.

## Decisión

1. **No se generaliza el runner.** No hay `RunnerSpec` ni un tercer
   `REVIEW_ENGINE`. El binario, el argv y el contrato de salida no cambian.
   Se añade un destino, no un proveedor.
2. **El gateway es un servicio del perfil `claude` y una capa
   anticorrupción.** El núcleo no conoce el clúster ni LiteLLM. Solo
   `claude-runner` cambia a quién le pide, con variables que vacías dejan el
   comportamiento de hoy. No es una pieza de evaluación que se retire al
   medir: si el spike de [032](../iniciativas/archivo/032-skill-con-modelo-de-la-cluster/spec.md)
   sale, el servicio se queda.
3. **La jaula declara su dominio permitido.** `allowedDomains` deja de ser
   una constante y pasa a parámetro de `runner_sandbox_settings`. Con
   `anthropic`, la lista sigue siendo `api.anthropic.com`. Con `gateway`, el
   nombre del servicio en la red de Compose. El cierre sigue siendo el
   sandbox del CLI.
4. **La clave del clúster no sale del gateway.** `claude-runner` recibe un
   `master_key` local. Ni el worker, ni el hijo, ni el checkout ven
   `NAN_API_KEY`.
5. **Sin sesión OAuth no hay parking por `session`.** El gateway no caduca
   cada 12 h. `active()` mira su health. Un 429 del clúster no usa el
   parking `quota`: el gateway lo reintenta dentro de la llamada y, si
   sigue, el worker lo reencola como fallo transitorio. `quota` queda para
   el desenlace de cupo de Claude Code.
6. **LiteLLM va pineado.** Las versiones 1.82.7 y 1.82.8 se publicaron con
   código de exfiltración de credenciales.

## Consecuencias

- El mismo `REVIEW_ENGINE=claude` sirve para dos destinos. La página solo
  ofrece esos dos: `llm` no se elige ahí. `claude-runner` lee el destino en
  cada `POST /run`, así que pasar de Anthropic al gateway no reinicia nada.
  El panel no recibe el socket de Docker.
- El clasificador por `subtype` no se toca. El 429 del gateway no entra por
  ahí: no es el cupo de la suscripción.
- La imagen gana un servicio más, solo con el perfil `claude`.
- Cambia quién ve el diff cuando el destino es el gateway. Eso va a
  `docs/operacion.md` §3 al implementar, no a un comentario en el código.
- Un gateway caído se ve como `ClaudeProcessCrashed` (transitorio) y se
  reintenta. Para el worker da igual que haya caído LiteLLM o el CLI.
- El *gold set* no gana una tercera configuración. La calidad de este
  destino se juzga leyendo el artefacto del spike.

### Riesgos aceptados (matiz del 2026-10-29)

- **DNS.** El egress acepta DNS hacia el resolver de Docker (`127.0.0.11`,
  en loopback), que reenvía cualquier nombre. Un LiteLLM comprometido podría
  sacar `NAN_API_KEY` codificada en consultas DNS. No se cierra: el stack corre
  en un entorno privado y controlado, y la defensa es la imagen pineada.
- **`master_key` en el hijo.** Con el clúster, el hijo del CLI recibe el
  `master_key` de LiteLLM, no una clave virtual con alcance. Las claves
  virtuales exigen darle a LiteLLM una base de datos. Sin ella, ese key solo
  sirve para lo que el hijo ya hace: llamar a los dos modelos del clúster.

## Alternativas descartadas

- **`ANTHROPIC_BASE_URL` directo al clúster.** 404 en `/v1/messages`. El
  clúster solo habla OpenAI.
- **Traducir dentro de `claude-runner`.** Mete LiteLLM en el proceso que
  tiene el checkout y el bearer del runner.
- **Un `RunnerSpec` genérico.** Con un solo binario, sería generalizar por
  si acaso, contra [ADR-0028](0028-cli-claude-fuera-del-worker.md) y
  [ADR-0033](0033-skill-fuera-del-nucleo.md).
- **`cursor-agent` como tercer motor.** `--api-key` autentica contra Cursor
  (`api2.cursor.sh`), no contra un endpoint OpenAI-compatible. `-p` no tiene
  `--allowedTools`, así que el cierre de escritura sería más débil que
  `dontAsk`.
- **Medirlo en el *gold set*.** El arnés compara personas del motor `llm`
  contra un baseline grabado. Meter aquí la skill mezclaría dos contratos
  de salida y un procedimiento que no es determinista bajo *replay*.
