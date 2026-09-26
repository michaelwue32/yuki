#!/usr/bin/env python3
"""Wyoming-STT-Bridge: macht Yukis faster-whisper (large-v3, RTX) zur STT-Engine
in Home Assistants Assist-Pipeline (Phase 2c).

Ersetzt das lahme + ungenaue Pi-Whisper. Pipeline: Voice PE -> Audio -> [DIESE
Bridge] -> Yukis /stt (faster-whisper auf der RTX) -> Text -> Conversation-Agent.

Die Bridge ist ein duenner Protokoll-Uebersetzer: sie sammelt die Wyoming-Audio-
Chunks einer Aeusserung, packt sie als WAV und POSTet sie an Yukis /stt-Endpoint
(server.py, gleiche Maschine). KEINE Modelle hier - Whisper laeuft im Yuki-Server.

Bewusst SEPARAT von der TTS-Bridge (wyoming_tts_yuki.py, Port 10200): additiv,
eigener Port, unabhaengig restartbar; die laufende TTS bleibt unberuehrt.

Start (auf der RTX-Maschine, in Yukis .venv):
    pip install wyoming   # falls noch nicht (TTS-Bridge braucht es auch)
    python wyoming_stt_yuki.py --uri tcp://0.0.0.0:10300 \
        --yuki-url https://127.0.0.1:8443/stt

HA-Seite: zweite "Wyoming Protocol"-Integration -> Host = RTX-IP, Port = 10300.
Danach Assist-Pipeline -> Speech-to-Text = "yuki" (large-v3). docs/setup-ha-voice.md.
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

from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.info import AsrModel, AsrProgram, Attribution, Describe, Info
from wyoming.server import AsyncEventHandler, AsyncServer

_LOGGER = logging.getLogger("wyoming_stt_yuki")

# Yuki-BFF ist self-signed HTTPS -> Zertifikat bewusst nicht pruefen (LAN-Trust).
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _transcribe_via_yuki(url: str, wav_bytes: bytes, language: str | None,
                         timeout: int) -> str:
    """POST WAV an Yukis /stt -> erkannter Text ('' bei Fehler). Blockierend ->
    im Handler via asyncio.to_thread (Whisper ist auf der GPU eh seriell)."""
    files = {"audio": ("speech.wav", wav_bytes, "audio/wav")}
    data = {}
    if language:
        data["language"] = language
    try:
        r = requests.post(url, files=files, data=data, timeout=timeout, verify=False)
    except requests.RequestException as e:
        _LOGGER.error("Yuki /stt nicht erreichbar (%s): %s", url, e)
        return ""
    if r.status_code != 200:
        _LOGGER.warning("Yuki /stt HTTP %s: %s", r.status_code, r.text[:160])
        return ""
    try:
        return (r.json().get("text") or "").strip()
    except ValueError:
        _LOGGER.error("Yuki /stt: keine JSON-Antwort")
        return ""


class YukiSttEventHandler(AsyncEventHandler):
    """Sammelt eine Aeusserung (AudioStart..Chunk..Stop) und transkribiert via Yuki."""

    def __init__(self, cli_args, wyoming_info: Info, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._args = cli_args
        self._info_event = wyoming_info.event()
        self._reset()

    def _reset(self) -> None:
        self._audio = bytearray()
        self._rate = 16000
        self._width = 2
        self._channels = 1
        # Sprache: aus Transcribe (Pipeline-Sprache); 'auto'/leer -> Yuki Auto-Detect.
        self._language: str | None = None

    async def handle_event(self, event) -> bool:
        if Describe.is_type(event.type):
            await self.write_event(self._info_event)
            return True

        if Transcribe.is_type(event.type):
            t = Transcribe.from_event(event)
            if t.language:
                # HA schickt z.B. 'de' oder 'de-DE' -> Primary-Subtag, Yuki normalisiert weiter.
                self._language = t.language.split("-")[0].lower()
            return True

        if AudioStart.is_type(event.type):
            a = AudioStart.from_event(event)
            self._audio = bytearray()
            self._rate, self._width, self._channels = a.rate, a.width, a.channels
            return True

        if AudioChunk.is_type(event.type):
            self._audio += AudioChunk.from_event(event).audio
            return True

        if AudioStop.is_type(event.type):
            text = ""
            if self._audio:
                wav = self._to_wav()
                text = await asyncio.to_thread(
                    _transcribe_via_yuki, self._args.yuki_url, wav,
                    self._language, self._args.timeout)
            _LOGGER.debug("Transcript: %r", text[:120])
            await self.write_event(Transcript(text=text).event())
            self._reset()
            return True

        return True

    def _to_wav(self) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setframerate(self._rate)
            wf.setsampwidth(self._width)
            wf.setnchannels(self._channels)
            wf.writeframes(bytes(self._audio))
        return buf.getvalue()


def _build_info() -> Info:
    return Info(asr=[AsrProgram(
        name="yuki-whisper",
        description="Yuki faster-whisper (large-v3), lokal auf der RTX",
        attribution=Attribution(name="faster-whisper",
                                url="https://github.com/SYSTRAN/faster-whisper"),
        installed=True,
        version="1.0.0",
        models=[AsrModel(
            name="yuki",
            description="Yuki (large-v3)",
            attribution=Attribution(name="faster-whisper",
                                    url="https://github.com/SYSTRAN/faster-whisper"),
            installed=True,
            version=None,
            languages=["de", "en", "ja"],
        )],
    )])


async def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default=os.environ.get("YUKI_STT_URI", "tcp://0.0.0.0:10300"),
                        help="Wyoming-Server-URI (Default tcp://0.0.0.0:10300)")
    parser.add_argument("--yuki-url", default=os.environ.get("YUKI_STT_URL",
                        "https://127.0.0.1:8443/stt"),
                        help="Yukis /stt-Endpoint")
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("YUKI_STT_TIMEOUT", "60")),
                        help="HTTP-Timeout fuer /stt in Sekunden")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    wyoming_info = _build_info()
    server = AsyncServer.from_uri(args.uri)
    _LOGGER.info("Yuki-Wyoming-STT lauscht auf %s -> %s", args.uri, args.yuki_url)
    await server.run(partial(YukiSttEventHandler, args, wyoming_info))


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
