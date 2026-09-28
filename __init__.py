"""quick-ack — acuse inmediato mientras el modelo grande piensa.

Cada mensaje de usuario (texto, o nota de voz ya transcrita) dispara, en paralelo al turno
normal, una llamada mínima a un modelo rápido por cualquier endpoint OpenAI-compatible: sin
SOUL, sin MCP, sin historial. Devuelve una frase tipo «Vale, miro los logs» que se envía al
chat al momento; el turno de verdad sigue su curso sin tocarse.

Caché de prefijo: el system prompt es constante byte a byte y el mensaje del usuario va
SIEMPRE al final. Alibaba solo cachea desde 1024 tokens y el prompt por defecto ronda 400:
no cachea ni le hace falta (1,1-2,2 s medidos con qwen3.8-flash). Si lo alargas, la caché
entra sola; no metas nada variable (fecha, nombre, perfil) en el system.

El modelo puede contestar ``-``: saludos, gracias, «sí/no» y demás mensajes donde un acuse
sobra. Entonces no se envía nada. Si tarda más de ``QUICK_ACK_TIMEOUT_S`` o falla, tampoco:
el acuse es un extra, nunca bloquea ni retrasa el turno.

Solo stdlib. La llamada HTTP corre en un hilo; el envío vuelve al loop del gateway.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import urllib.request

logger = logging.getLogger("plugins.quick_ack")

# Cualquier endpoint OpenAI-compatible (LiteLLM, DashScope compatible-mode, OpenRouter...).
BASE_URL = (os.environ.get("QUICK_ACK_BASE_URL")
            or "https://dashscope-intl.aliyuncs.com/compatible-mode/v1").rstrip("/")
MODEL = os.environ.get("QUICK_ACK_MODEL") or "qwen3.8-flash"
API_KEY_ENVS = ("QUICK_ACK_API_KEY", "DASHSCOPE_API_KEY", "LITELLM_API_KEY", "OPENAI_API_KEY")
# "none" apaga el razonamiento en qwen3.x; vacío = no se envía (proveedores que no lo aceptan).
REASONING_EFFORT = os.environ.get("QUICK_ACK_REASONING_EFFORT", "none")
TIMEOUT_S = float(os.environ.get("QUICK_ACK_TIMEOUT_S") or 6)
MIN_CHARS = int(os.environ.get("QUICK_ACK_MIN_CHARS") or 12)
MAX_INPUT_CHARS = 1500
# Perfiles con acuse (coma); vacío = todos.
PROFILES = {p.strip() for p in (os.environ.get("QUICK_ACK_PROFILES") or "").split(",") if p.strip()}
SKIP = "-"

# NO interpolar nada aquí: es el prefijo cacheado (ver docstring del módulo).
_DEFAULT_PROMPT = """Eres la voz rápida de un asistente personal. Mientras otro modelo más lento hace el trabajo de verdad, tú contestas AL INSTANTE con una sola frase corta que confirma que se ha recibido la petición y qué se va a hacer.

Reglas:
- Una sola frase, máximo 15 palabras, en el idioma del usuario (normalmente español), tono cercano y natural, como hablando.
- Di QUÉ se va a hacer con verbos de acción ("miro", "busco", "preparo", "reviso", "te lo genero"). Nunca des la respuesta, datos, cifras ni resultados: no los sabes.
- No prometas plazos ni resultados, no hagas preguntas, no uses emojis ni markdown.
- Si el mensaje no pide trabajo (saludo, gracias, ok, sí, no, una reacción, charla corta) o una frase de acuse sobraría, responde exactamente: -

Ejemplos:
Usuario: mira por qué el pod de litellm se está reiniciando
Tú: Vale, miro los logs de LiteLLM y te digo qué pasa.
Usuario: recuérdame mañana a las 9 llamar al gestor
Tú: Hecho, te lo apunto para mañana a las nueve.
Usuario: hazme una imagen de un dragón en una ciudad cyberpunk
Tú: Me pongo con la imagen del dragón cyberpunk.
Usuario: qué tiempo va a hacer este finde en Barcelona
Tú: Déjame mirar la previsión para Barcelona.
Usuario: resume los correos de hoy
Tú: Voy a leer los correos de hoy y te hago el resumen.
Usuario: gracias!
Tú: -
Usuario: hola
Tú: -
Usuario: ok perfecto
Tú: -"""


def _load_prompt() -> str:
    path = os.environ.get("QUICK_ACK_PROMPT_FILE")
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    return _DEFAULT_PROMPT


SYSTEM_PROMPT = _load_prompt()


def _complete(text: str) -> str:
    body = {
        "model": MODEL,
        # cache_control: Alibaba solo cachea desde 1024 tokens (medido: con 411 no hay caché,
        # con 2195 lee 2175). A este tamaño no hace nada; entra solo si el prompt crece.
        "messages": [{"role": "system", "content": [{"type": "text", "text": SYSTEM_PROMPT,
                                                     "cache_control": {"type": "ephemeral"}}]},
                     {"role": "user", "content": text[:MAX_INPUT_CHARS]}],
        "max_tokens": 60,
        "temperature": 0.3,
        "stream": False,
    }
    if REASONING_EFFORT:
        body["reasoning_effort"] = REASONING_EFFORT
    api_key = next((os.environ[k] for k in API_KEY_ENVS if os.environ.get(k)), "")
    req = urllib.request.Request(
        BASE_URL + "/chat/completions", data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + api_key})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""


def _clean(reply: str) -> str:
    reply = reply.strip().strip('"').strip()
    if not reply or reply == SKIP or reply.startswith(SKIP + " ") or len(reply) > 200:
        return ""
    return reply.splitlines()[0].strip()


def _wanted(gateway, source, text: str, check_busy: bool = True) -> bool:
    text = (text or "").strip()
    if len(text) < MIN_CHARS or text.startswith("/"):
        return False
    if getattr(source, "is_bot", False):
        return False
    if PROFILES and str(getattr(source, "profile", "") or "default") not in PROFILES:
        return False
    try:
        if not gateway._is_user_authorized_for_source(source):
            return False
        # Sesión ocupada: el mensaje es un follow-up/interrupción; eso ya lo acusa busy_ack.
        # (La voz llega aquí con el turno ya reservado: ahí no se mira.)
        running = getattr(gateway, "_running_agents", None) if check_busy else None
        if running and gateway._session_key_for_source(source) in running:
            return False
    except Exception as exc:  # APIs privadas del gateway: si cambian, sin acuse y avisamos
        logger.warning("quick-ack: no puedo evaluar la fuente (%s); sin acuse", exc)
        return False
    return True


def _fire(gateway, event, source, text: str) -> None:
    """Lanza el acuse sin bloquear: HTTP en un hilo, envío en el loop del gateway."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    started = time.monotonic()

    def _work():
        try:
            reply = _clean(_complete(text))
        except Exception as exc:
            logger.info("quick-ack: sin acuse (%s: %s)", type(exc).__name__, exc)
            return
        took = time.monotonic() - started
        if not reply:
            logger.debug("quick-ack: el modelo dijo que sobra (%.2fs)", took)
            return
        asyncio.run_coroutine_threadsafe(_send(gateway, event, source, reply, took), loop)

    threading.Thread(target=_work, name="quick-ack", daemon=True).start()


async def _send(gateway, event, source, reply: str, took: float) -> None:
    try:
        # El nombre del resolvedor cambia entre versiones de Hermes.
        resolve = getattr(gateway, "_delivery_adapter_for", None) or gateway._adapter_for_source
        adapter = resolve(source)
        if adapter is None:
            return
        anchor = getattr(gateway, "_reply_anchor_for_event", None)
        metadata = gateway._thread_metadata_for_source(
            source, anchor(event) if anchor else getattr(event, "message_id", None))
        await adapter.send(source.chat_id, reply, metadata=metadata)
        logger.info("quick-ack: enviado en %.2fs (%s)", took, getattr(source, "profile", None) or "default")
    except Exception as exc:
        logger.warning("quick-ack: envío fallido: %s", exc)


def _wrap_voice(gateway) -> None:
    """Las notas de voz se transcriben DESPUÉS de pre_gateway_dispatch: se envuelve el paso de
    transcripción de esta instancia para acusar con el texto ya transcrito."""
    original = getattr(gateway, "_enrich_inbound_voice", None)
    if original is None or getattr(original, "_quick_ack", False):
        return

    async def _enrich_inbound_voice(event, source, message_text, audio_paths):
        text = await original(event, source, message_text, audio_paths)
        try:
            if _wanted(gateway, source, text, check_busy=False):
                _fire(gateway, event, source, text)
        except Exception as exc:
            logger.warning("quick-ack (voz): %s", exc)
        return text

    _enrich_inbound_voice._quick_ack = True
    gateway._enrich_inbound_voice = _enrich_inbound_voice


def _on_dispatch(event=None, gateway=None, **_kwargs):
    if event is None or gateway is None:
        return None
    try:
        _wrap_voice(gateway)
        source = event.source
        kind = getattr(getattr(event, "message_type", None), "value", "text")
        if kind in ("voice", "audio"):
            return None  # lo acusa el envoltorio de la transcripción
        if _wanted(gateway, source, event.text):
            _fire(gateway, event, source, event.text)
    except Exception as exc:
        logger.warning("quick-ack: %s", exc)
    return None  # nunca altera ni descarta el mensaje


def register(ctx) -> None:
    ctx.register_hook("pre_gateway_dispatch", _on_dispatch)
