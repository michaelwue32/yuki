#!/usr/bin/env python3
"""Smoke-Test fuer die Yuki-Wyoming-TTS-Bridge (Phase 2b).

Verbindet sich wie HA als Wyoming-Client mit der laufenden Bridge, schickt einen
Describe + einen Synthesize und sammelt die Audio-Chunks zu einer WAV-Datei.
Gruen = die Bridge spricht. Setzt voraus: server.py (mit /tts) + die TTS-Services
(SoVITS/F5) + wyoming_tts_yuki.py laufen.

    .venv\\Scripts\\python.exe tools\\test_wyoming_tts.py
    .venv\\Scripts\\python.exe tools\\test_wyoming_tts.py --host 127.0.0.1 --text "Guten Abend"
"""

from __future__ import annotations

import argparse
import asyncio
import wave

from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.info import Describe, Info
from wyoming.tts import Synthesize


async def _run(host: str, port: int, text: str, out: str) -> int:
    async with AsyncTcpClient(host, port) as client:
        # 1) Describe -> Info (welche Stimmen?)
        await client.write_event(Describe().event())
        info = await client.read_event()
        if info is not None and Info.is_type(info.type):
            voices = [v.name for p in (Info.from_event(info).tts or []) for v in p.voices]
            print(f"[Describe] Bridge meldet TTS-Stimmen: {voices}")
        else:
            print(f"[Describe] unerwartete Antwort: {info and info.type}")

        # 2) Synthesize -> Audio
        print(f"[Synthesize] sende: {text!r}")
        await client.write_event(Synthesize(text=text).event())

        rate = width = channels = None
        frames = bytearray()
        while True:
            ev = await client.read_event()
            if ev is None:
                print("[Fehler] Verbindung vor AudioStop geschlossen")
                return 2
            if AudioStart.is_type(ev.type):
                a = AudioStart.from_event(ev)
                rate, width, channels = a.rate, a.width, a.channels
                print(f"[AudioStart] {rate} Hz, {width*8} bit, {channels} ch")
            elif AudioChunk.is_type(ev.type):
                frames += AudioChunk.from_event(ev).audio
            elif AudioStop.is_type(ev.type):
                break

    if not frames or not rate:
        print("[Ergebnis] STILLE (kein Audio) - Yuki /tts down oder nichts Sprechbares?")
        return 1

    with wave.open(out, "wb") as wf:
        wf.setframerate(rate)
        wf.setsampwidth(width)
        wf.setnchannels(channels)
        wf.writeframes(bytes(frames))
    secs = len(frames) / (rate * width * channels)
    print(f"[OK] {len(frames)} Bytes = {secs:.1f}s Audio -> {out}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=10200)
    p.add_argument("--text", default="Hallo, ich bin Yuki. Schoen, dass das funktioniert.")
    p.add_argument("--out", default="tests/outputs/wyoming_tts_smoke.wav")
    args = p.parse_args()
    import os
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    return asyncio.run(_run(args.host, args.port, args.text, args.out))


if __name__ == "__main__":
    raise SystemExit(main())
