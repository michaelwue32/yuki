#!/usr/bin/env python3
"""Smoke-Test fuer die Yuki-Wyoming-STT-Bridge (Phase 2c).

Verbindet sich wie HA als Wyoming-Client mit der laufenden STT-Bridge, schickt ein
WAV (Transcribe + AudioStart + AudioChunk* + AudioStop) und druckt das Transcript.
Default-WAV ist die TTS-Smoke-Ausgabe -> netter Round-Trip (Yukis Stimme wieder
erkannt). Setzt voraus: server.py (mit /stt) + faster-whisper + wyoming_stt_yuki.py.

    .venv\\Scripts\\python.exe tools\\test_wyoming_stt.py
    .venv\\Scripts\\python.exe tools\\test_wyoming_stt.py --wav pfad\\zu\\sprache.wav --language de
"""

from __future__ import annotations

import argparse
import asyncio
import wave

from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.info import Describe, Info

_CHUNK = 2048


async def _run(host: str, port: int, wav_path: str, language: str | None) -> int:
    with wave.open(wav_path, "rb") as wf:
        rate, width, channels = wf.getframerate(), wf.getsampwidth(), wf.getnchannels()
        frames = wf.readframes(wf.getnframes())
    print(f"[WAV] {wav_path}: {rate} Hz, {width*8} bit, {channels} ch, {len(frames)} Bytes")

    async with AsyncTcpClient(host, port) as client:
        await client.write_event(Describe().event())
        info = await client.read_event()
        if info is not None and Info.is_type(info.type):
            models = [m.name for p in (Info.from_event(info).asr or []) for m in p.models]
            print(f"[Describe] Bridge meldet ASR-Modelle: {models}")

        await client.write_event(Transcribe(language=language).event())
        await client.write_event(AudioStart(rate=rate, width=width, channels=channels).event())
        for i in range(0, len(frames), _CHUNK):
            await client.write_event(
                AudioChunk(rate=rate, width=width, channels=channels,
                           audio=frames[i:i + _CHUNK]).event())
        await client.write_event(AudioStop().event())

        while True:
            ev = await client.read_event()
            if ev is None:
                print("[Fehler] Verbindung vor Transcript geschlossen")
                return 2
            if Transcript.is_type(ev.type):
                text = Transcript.from_event(ev).text
                print(f"[Transcript] {text!r}")
                return 0 if text.strip() else 1


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=10300)
    p.add_argument("--wav", default="tests/outputs/wyoming_tts_smoke.wav")
    p.add_argument("--language", default=None, help="z.B. de / en / ja (Default: Auto)")
    args = p.parse_args()
    return asyncio.run(_run(args.host, args.port, args.wav, args.language))


if __name__ == "__main__":
    raise SystemExit(main())
