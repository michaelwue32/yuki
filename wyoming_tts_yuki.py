#!/usr/bin/env python3
"""Wyoming-TTS-Bridge: macht Yukis Stimme (GPT-SoVITS + F5-German) zu einer
TTS-Engine in Home Assistants Assist-Pipeline (Phase 2b).

Pipeline: Voice PE -> Whisper -> Yuki-Conversation-Agent (custom_component) ->
Antwort-Text -> [DIESE Bridge spricht Wyoming] -> HA -> Voice PE.

Die Bridge ist ein duenner Protokoll-Uebersetzer: pro Synthesize-Event POSTet sie
den Text an Yukis /tts-Endpoint (server.py, gleiche Maschine), bekommt ein WAV und
streamt es als Wyoming-Audio zurueck. KEINE Modelle hier - die TTS-Arbeit macht
Yukis Server (Engine-Routing SoVITS/F5 entscheidet er selbst).

Start (auf der RTX-Maschine, in Yukis .venv):
    pip install wyoming
    python wyoming_tts_yuki.py --uri tcp://0.0.0.0:10200 \
        --yuki-url https://127.0.0.1:8443/tts

HA-Seite: Einstellungen -> Geraete & Dienste -> Integration "Wyoming Protocol" ->
Host = RTX-IP, Port = 10200. Danach Assist-Pipeline -> Text-to-Speech = "yuki".
Siehe docs/setup-ha-voice.md.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import logging
import os
import wave
from functools import partial

import requests
import urllib3

from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.info import Attribution, Describe, Info, TtsProgram, TtsVoice
from wyoming.server import AsyncEventHandler, AsyncServer
from wyoming.tts import Synthesize

_LOGGER = logging.getLogger("wyoming_tts_yuki")

# Yuki-BFF ist self-signed HTTPS -> Zertifikat bewusst nicht pruefen (LAN-Trust wie
# der restliche Yuki-Verkehr). Die laute InsecureRequestWarning unterdruecken.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Wenn Yuki nichts Sprechbares liefert (503) oder down ist: Stille statt Crash.
# Default-Format fuer den leeren Stream (HA resampled eh).
_FALLBACK_RATE = 22050
_FALLBACK_WIDTH = 2
_FALLBACK_CHANNELS = 1
_BYTES_PER_CHUNK = 2048


def _fetch_wav(url: str, text: str, token: str, persona: str | None,
               timeout: int) -> bytes | None:
    """POST an Yukis /tts -> WAV-Bytes (oder None bei 503/Fehler). Blockierend -
    wird im Handler via asyncio.to_thread aufgerufen (TTS ist auf der GPU eh seriell)."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Yuki-Token"] = token
    body = {"text": text}
    if persona:
        body["persona"] = persona
    try:
        r = requests.post(url, json=body, headers=headers, timeout=timeout, verify=False)
    except requests.RequestException as e:
        _LOGGER.error("Yuki /tts nicht erreichbar (%s): %s", url, e)
        return None
    if r.status_code != 200:
        # 503 = nichts Sprechbares / TTS down. Kein Audio -> Stille.
        _LOGGER.warning("Yuki /tts HTTP %s: %s", r.status_code, r.text[:160])
        return None
    return r.content


class YukiTtsEventHandler(AsyncEventHandler):
    """Beantwortet Describe (was kann ich) + Synthesize (Text -> Audio)."""

    def __init__(self, cli_args, wyoming_info: Info, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._args = cli_args
        self._info_event = wyoming_info.event()

    async def handle_event(self, event) -> bool:
        if Describe.is_type(event.type):
            await self.write_event(self._info_event)
            return True

        if not Synthesize.is_type(event.type):
            return True

        synth = Synthesize.from_event(event)
        text = (synth.text or "").strip()
        # INFO (nicht debug), damit im Dashboard-Tab sichtbar ist, OB HA die Bruecke
        # ueberhaupt aufruft - der entscheidende HA-seitig-vs-lokal-Diagnose-Marker.
        _LOGGER.info("HA -> Synthesize (%d Zeichen): %r", len(text), text[:60])

        wav_bytes = await asyncio.to_thread(
            _fetch_wav, self._args.yuki_url, text, self._args.token,
            self._args.persona, self._args.timeout,
        )

        if not wav_bytes:
            # Stille (gueltiger leerer Stream), damit die Pipeline nicht haengt.
            await self.write_event(
                AudioStart(rate=_FALLBACK_RATE, width=_FALLBACK_WIDTH,
                           channels=_FALLBACK_CHANNELS).event())
            await self.write_event(AudioStop().event())
            return True

        try:
            with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
                rate = wf.getframerate()
                width = wf.getsampwidth()
                channels = wf.getnchannels()
                frames = wf.readframes(wf.getnframes())
        except wave.Error as e:
            _LOGGER.error("WAV von Yuki nicht lesbar: %s", e)
            await self.write_event(
                AudioStart(rate=_FALLBACK_RATE, width=_FALLBACK_WIDTH,
                           channels=_FALLBACK_CHANNELS).event())
            await self.write_event(AudioStop().event())
            return True

        await self.write_event(
            AudioStart(rate=rate, width=width, channels=channels).event())
        for i in range(0, len(frames), _BYTES_PER_CHUNK):
            await self.write_event(
                AudioChunk(rate=rate, width=width, channels=channels,
                           audio=frames[i:i + _BYTES_PER_CHUNK]).event())
        await self.write_event(AudioStop().event())
        _LOGGER.info("HA <- gesprochen: %d Bytes @ %d Hz", len(frames), rate)
        return True


def _build_info() -> Info:
    return Info(tts=[TtsProgram(
        name="yuki",
        description="Yuki (GPT-SoVITS + F5-German), lokal auf der RTX",
        attribution=Attribution(name="Yuki", url="https://github.com/yourname/yuki"),
        installed=True,
        version="1.0.0",
        voices=[TtsVoice(
            name="yuki",
            description="Yuki",
            attribution=Attribution(name="Yuki", url="https://github.com/yourname/yuki"),
            installed=True,
            version=None,
            # Yukis Server routet die Engine selbst nach Textsprache - die Stimme
            # deckt DE/EN/JA ab.
            languages=["de", "en", "ja"],
        )],
    )])


async def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default=os.environ.get("YUKI_TTS_URI", "tcp://0.0.0.0:10200"),
                        help="Wyoming-Server-URI (Default tcp://0.0.0.0:10200)")
    parser.add_argument("--yuki-url", default=os.environ.get("YUKI_TTS_URL",
                        "https://127.0.0.1:8443/tts"),
                        help="Yukis /tts-Endpoint")
    parser.add_argument("--token", default=os.environ.get("YUKI_TTS_TOKEN", ""),
                        help="optionaler X-Yuki-Token (= converse_token in Yukis HA-Config)")
    parser.add_argument("--persona", default=os.environ.get("YUKI_TTS_PERSONA", "") or None,
                        help="optionaler Persona-Override fuers Engine-Routing (Default: Yukis aktuelle)")
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("YUKI_TTS_TIMEOUT", "120")),
                        help="HTTP-Timeout fuer /tts in Sekunden")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    wyoming_info = _build_info()
    server = AsyncServer.from_uri(args.uri)
    _LOGGER.info("Yuki-Wyoming-TTS lauscht auf %s -> %s", args.uri, args.yuki_url)
    await server.run(partial(YukiTtsEventHandler, args, wyoming_info))


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
