# quick-ack — acuse inmediato para Hermes

Plugin de [Hermes Agent](https://github.com/NousResearch/hermes-agent). Si Hermes usa un LLM local o lento, la primera respuesta puede tardar bastante: antes de escribir nada, el modelo tiene que procesar el SOUL, las herramientas MCP y el historial. Por voz ese silencio se nota mucho.

Mientras el turno normal trabaja, quick-ack lanza **en paralelo** una llamada mínima a un modelo rápido. Esa llamada no lleva SOUL, ni MCP, ni historial. El modelo contesta con **una frase** que se envía al chat al momento:

```
Tú:     mira por qué el cronjob de backups ha fallado esta noche
Hermes: Voy a revisar los logs del cronjob de backups y te digo qué ha pasado.   ← ~1,5 s (quick-ack)
Hermes: El job falló porque…                                                     ← el turno de verdad
```

- **Texto**: la frase se pide al recibir el mensaje (hook `pre_gateway_dispatch`).
- **Notas de voz**: la frase se pide en cuanto termina la transcripción.
- **Sin acuse** en estos casos: saludos, «gracias», «ok» (el modelo responde `-`), comandos `/…`, mensajes de bots, usuarios no autorizados, mensajes de menos de 12 caracteres y mensajes que llegan mientras la sesión ya está trabajando (esos los cubre el `busy_ack` de Hermes).
- **No toca el turno**: el hook siempre devuelve `None`, así que no reescribe ni descarta nada. Si el modelo rápido falla o tarda más del timeout, no se envía nada.
- **Grupos con `require_mention`**: el adaptador filtra las menciones antes del hook, así que solo se acusan los mensajes dirigidos al bot.

## Instalación

```bash
hermes plugins install pocharlies-org/hermes-quick-ack --enable
# o a mano: clonar en ~/.hermes/plugins/quick-ack y añadir "quick-ack" a plugins.enabled
```

Reinicia el gateway.

## Configuración (variables de entorno)

| Variable | Por defecto | Qué hace |
|---|---|---|
| `QUICK_ACK_BASE_URL` | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` | Cualquier endpoint OpenAI-compatible (LiteLLM, DashScope, OpenRouter…) |
| `QUICK_ACK_MODEL` | `qwen3.8-flash` | Modelo del acuse; conviene que sea rápido y barato |
| `QUICK_ACK_API_KEY` | — | Si no está, prueba `DASHSCOPE_API_KEY`, `LITELLM_API_KEY` y `OPENAI_API_KEY`, en ese orden |
| `QUICK_ACK_REASONING_EFFORT` | `none` | Apaga el razonamiento en qwen3.x; déjalo vacío si tu proveedor no acepta el campo |
| `QUICK_ACK_TIMEOUT_S` | `6` | Pasado este tiempo, el acuse ya no se envía |
| `QUICK_ACK_MIN_CHARS` | `12` | Los mensajes más cortos no llevan acuse |
| `QUICK_ACK_PROFILES` | *(todos)* | Perfiles con acuse, separados por comas |
| `QUICK_ACK_PROMPT_FILE` | — | Sustituye el prompt por defecto (en español, con ejemplos) |

## Caché de prefijo

El system prompt es una constante y el mensaje del usuario va siempre al final. Así el prefijo es idéntico en todas las llamadas.

Medido contra Alibaba Model Studio con `qwen3.8-flash`:

- **Solo cachea prefijos de 1024 tokens o más.** Con 2195 tokens, la caché implícita reutilizó 2048 y la explícita (`cache_control`) 2175.
- El prompt por defecto tiene unos 410 tokens, así que **no se cachea**. Tampoco le hace falta: responde en **1,1–2,2 s**. Alargarlo solo para llegar a la caché lo haría más lento.
- El `cache_control` ya va puesto en la llamada. Si amplías el prompt con `QUICK_ACK_PROMPT_FILE`, la caché entra sola. No metas en el prompt nada que cambie entre llamadas (fecha, nombre, perfil), porque rompería la caché.

## Compatibilidad

- Probado en Hermes v2026.9.14.
- Usa métodos internos del gateway (`_is_user_authorized_for_source`, `_session_key_for_source`, `_adapter_for_source`/`_delivery_adapter_for`, `_thread_metadata_for_source`, `_enrich_inbound_voice`). Si una versión de Hermes los cambia, el plugin deja de enviar acuses y lo registra en el log, pero nunca rompe el turno.
- Solo usa la biblioteca estándar de Python.

## Tests

```bash
pytest -q tests
```

MIT.
