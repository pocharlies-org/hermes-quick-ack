# ARCHITECTURE — hermes-quick-ack

Plugin de Hermes Agent (`plugin.yaml`, `__init__.py`) que lanza un acuse inmediato de una frase mientras el turno normal sigue trabajando. Pensado para LLM locales o lentos: el modelo rápido no lleva SOUL, ni MCP, ni historial.

## Clientes y versiones

- Un cliente: el gateway de Hermes (`NousResearch/hermes-agent`; probado en v2026.9.14), por el hook `pre_gateway_dispatch` (texto) y tras la transcripción (notas de voz).
- Plataformas con acuse: `telegram,whatsapp,discord,slack,signal,matrix` (`QUICK_ACK_PLATFORMS`); quedan fuera webhook, api_server y cron.
- Multiperfil: hay un gestor de plugins por `HERMES_HOME`; se instala y habilita en cada perfil que atienda chats. Licencia MIT.

## Dependencias (en ambos sentidos)

- Depende de: métodos internos del gateway (`_is_user_authorized_for_source`, `_session_key_for_source`, `_adapter_for_source`/`_delivery_adapter_for`, `_thread_metadata_for_source`, `_enrich_inbound_voice`) y un endpoint compatible con OpenAI (por defecto DashScope `qwen3.8-flash`; también LiteLLM u OpenRouter).
- Dependen de él: los perfiles de Hermes de la compañía (chart `helm/hermes` de `k8s-openclaw-qwen36-pocharlies`; el clon `wt-hermes-quick-ack` del host es un worktree de ese repo, no de este).
- Si una versión de Hermes cambia esos métodos internos, el plugin deja de enviar acuses y lo registra en el log, sin romper el turno.

## Stack

Python 3 con solo biblioteca estándar, `plugin.yaml` como manifiesto y `pytest` para los tests.

## Componentes compartidos

Ninguno hacia fuera. Internamente: un prompt de sistema constante (por defecto en español, ~410 tokens) y el mensaje del usuario siempre al final, para que el prefijo sea idéntico y la caché de prefijo entre sola si se alarga con `QUICK_ACK_PROMPT_FILE`.

## Cómo se construye

El hook siempre devuelve `None`: nunca reescribe ni descarta el mensaje. Sin acuse para saludos, «gracias», «ok» (el modelo responde `-`), comandos `/…`, bots, usuarios no autorizados, mensajes de menos de 12 caracteres y mensajes que llegan con la sesión ya trabajando. No se mete en el prompt nada que cambie entre llamadas (fecha, nombre, perfil): rompería la caché.

## Tests

`pytest -q tests` (`tests/test_quick_ack.py`).

## CI/CD y despliegue

Sin CI propio más allá de los workflows estándar de la org (`duplicados.yml` y `pr-review.yml`). Instalación: `hermes plugins install pocharlies-org/hermes-quick-ack --enable` (o clonar en `~/.hermes/plugins/quick-ack` y añadirlo a `plugins.enabled`) y reiniciar el gateway. En la compañía, los plugins de Hermes se cambian por PR a `k8s-openclaw-qwen36-pocharlies`.

## Decisiones y trampas

- La caché implícita solo cachea prefijos de 1024 tokens o más: el prompt por defecto (~410) no se cachea y no le hace falta (responde en 1,1–2,2 s); alargarlo solo para llegar a la caché lo haría más lento.
- Depende de métodos privados del gateway: cada actualización de Hermes puede romperlo en silencio (solo registra en el log).
- Si solo se habilita en el perfil por defecto, los chats enrutados a otro perfil no llevan acuse.
- El modelo y el endpoint por defecto son de Alibaba (DashScope), de pago: confirmar la política de gasto antes de usarlo con claves de la compañía.
