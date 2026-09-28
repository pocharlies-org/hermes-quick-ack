"""quick-ack: acuse inmediato con un modelo rápido mientras el turno trabaja.

Lo que se fija aquí:
- el prompt es una constante (prefijo cacheable) y el mensaje del usuario va al final;
- «-» del modelo, comandos, bots, no autorizados y sesiones ocupadas = sin acuse;
- nunca altera ni descarta el mensaje (el hook devuelve None);
- la voz se acusa DESPUÉS de transcribir (envoltorio de _enrich_inbound_voice).
"""
import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "__init__.py"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def qa():
    return _load(PLUGIN, "quick_ack_under_test")


class FakeAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append((chat_id, content, metadata))


class FakeGateway:
    def __init__(self, authorized=True, busy=False):
        self.adapter = FakeAdapter()
        self.authorized = authorized
        self._running_agents = {"key": object()} if busy else {}

    def _is_user_authorized_for_source(self, source):
        return self.authorized

    def _session_key_for_source(self, source):
        return "key"

    def _adapter_for_source(self, source):
        return self.adapter

    def _thread_metadata_for_source(self, source, anchor):
        return {"thread_id": source.thread_id, "anchor": anchor}

    def _reply_anchor_for_event(self, event):
        return event.message_id

    async def _enrich_inbound_voice(self, event, source, message_text, audio_paths):
        return "revisa por qué falla el backup de esta noche"


def _event(text, kind="text", **src):
    src.setdefault("platform", SimpleNamespace(value="telegram"))
    source = SimpleNamespace(chat_id="42", thread_id="7", profile=None, is_bot=False, **src)
    return SimpleNamespace(text=text, source=source, message_id="m1",
                           message_type=SimpleNamespace(value=kind))


def _run(qa, gw, coro_factory, reply):
    """Ejecuta el hook dentro de un loop, con la llamada HTTP simulada, y espera al envío."""
    calls = []

    def fake_complete(text):
        calls.append(text)
        return reply

    qa._complete = fake_complete

    async def main():
        result = await coro_factory()
        for _ in range(100):
            await asyncio.sleep(0.01)
            if gw.adapter.sent:
                break
        return result

    return asyncio.run(main()), calls


def test_prompt_fijo_y_usuario_al_final(qa, monkeypatch):
    captured = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "Vale."}}]}).encode()

    def fake_urlopen(req, timeout):
        captured["body"] = json.loads(req.data)
        captured["timeout"] = timeout
        return Resp()

    monkeypatch.setattr(qa.urllib.request, "urlopen", fake_urlopen)
    assert qa._complete("mira los logs") == "Vale."
    msgs = captured["body"]["messages"]
    assert msgs[0]["content"][0]["text"] == qa.SYSTEM_PROMPT
    assert msgs[-1] == {"role": "user", "content": "mira los logs"}
    assert captured["body"]["reasoning_effort"] == "none"
    assert captured["body"]["model"] == qa.MODEL
    assert "{" not in qa.SYSTEM_PROMPT  # nada interpolado en el prefijo cacheado


def test_texto_envia_acuse_en_el_hilo_y_no_toca_el_mensaje(qa):
    gw = FakeGateway()
    ev = _event("mira por qué se reinicia litellm")

    async def go():
        return qa._on_dispatch(event=ev, gateway=gw)

    result, calls = _run(qa, gw, go, "Vale, miro los logs de LiteLLM.")
    assert result is None
    assert calls == ["mira por qué se reinicia litellm"]
    assert gw.adapter.sent == [("42", "Vale, miro los logs de LiteLLM.", {"thread_id": "7", "anchor": "m1"})]


@pytest.mark.parametrize("text,gw_kwargs,src,reply", [
    ("mira por qué se reinicia litellm", {}, {}, "-"),                     # el modelo dice que sobra
    ("/model qwen3.8-flash", {}, {}, "Vale."),                               # comando
    ("ok", {}, {}, "Vale."),                                                 # demasiado corto
    ("mira por qué se reinicia litellm", {"authorized": False}, {}, "Vale."),
    ("mira por qué se reinicia litellm", {"busy": True}, {}, "Vale."),      # follow-up: busy_ack
    ("mira por qué se reinicia litellm", {}, {"platform": SimpleNamespace(value="webhook")}, "Vale."),
    ("mira por qué se reinicia litellm", {}, {"platform": SimpleNamespace(value="api_server")}, "Vale."),
])
def test_casos_sin_acuse(qa, text, gw_kwargs, src, reply):
    gw = FakeGateway(**gw_kwargs)
    ev = _event(text, **src)

    async def go():
        return qa._on_dispatch(event=ev, gateway=gw)

    result, _ = _run(qa, gw, go, reply)
    assert result is None
    assert gw.adapter.sent == []


def test_voz_se_acusa_tras_transcribir(qa):
    gw = FakeGateway(busy=True)  # al transcribir el turno ya está reservado: no cuenta
    ev = _event("", kind="voice")

    async def go():
        assert qa._on_dispatch(event=ev, gateway=gw) is None
        return await gw._enrich_inbound_voice(ev, ev.source, "", ["/tmp/a.ogg"])

    text, calls = _run(qa, gw, go, "Vale, reviso el backup.")
    assert text == "revisa por qué falla el backup de esta noche"  # la transcripción sigue intacta
    assert calls == [text]
    assert gw.adapter.sent[0][1] == "Vale, reviso el backup."


def test_fallo_del_modelo_no_rompe_nada(qa):
    gw = FakeGateway()
    ev = _event("mira por qué se reinicia litellm")

    def boom(text):
        raise TimeoutError("lento")

    async def go():
        qa._complete = boom
        r = qa._on_dispatch(event=ev, gateway=gw)
        await asyncio.sleep(0.1)
        return r

    assert asyncio.run(go()) is None
    assert gw.adapter.sent == []
