"""Ad-hoc: zusaetzliche REIFE/ERWACHSENE VOICEVOX-Referenzkandidaten minten.

Ergaenzung zu make_reference.py – gezielt gegen "zu quietschig": tiefe/ruhige/
gesetzte Frauenstimmen, die noch nicht in voices/ liegen. Gleicher Referenzsatz
wie die bestehenden Clips -> ref_text bleibt unveraendert nutzbar.

Aufruf:  .venv\\Scripts\\python.exe tools\\mint_mature_candidates.py
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
import io
import requests
import scipy.io.wavfile as wavfile
from pathlib import Path

VOICEVOX = "http://127.0.0.1:50021"
OUT_DIR = Path(__file__).resolve().parent.parent / "voices"
REF_SENTENCE = "こんにちは。わたしの名前はゆきです。日本語の勉強を一緒にがんばりましょう。"
TARGET_MAX_S = 9.5

# Nur NEUE (noch nicht geminte) Kandidaten, Fokus erwachsen/tief/ruhig.
CANDIDATES = [
    ("春日部つむぎ", "ノーマル"),
    ("もち子さん", "セクシー／あん子"),
    ("No.7", "アナウンス"),
    ("猫使アル", "おちつき"),
    ("ナースロボ＿タイプＴ", "ノーマル"),
    ("ナースロボ＿タイプＴ", "内緒話"),
    ("満別花丸", "ノーマル"),
    ("琴詠ニア", "ノーマル"),
    ("夜語トバリ", "ノーマル"),
    ("夜語トバリ", "哀しみ"),
    ("あんこもん", "けだるげ"),
    ("暁記ミタマ", "ノーマル"),
]


def resolve_styles():
    sp = requests.get(f"{VOICEVOX}/speakers", timeout=10).json()
    table = {}
    for s in sp:
        for st in s["styles"]:
            table[(s["name"], st["name"])] = st["id"]
    return table


def synth(text, speaker_id, speed=1.0):
    q = requests.post(f"{VOICEVOX}/audio_query",
                      params={"text": text, "speaker": speaker_id}, timeout=20).json()
    q["speedScale"] = speed
    wav = requests.post(f"{VOICEVOX}/synthesis",
                        params={"speaker": speaker_id}, json=q, timeout=60).content
    rate, data = wavfile.read(io.BytesIO(wav))
    return wav, len(data) / rate


def main():
    OUT_DIR.mkdir(exist_ok=True)
    table = resolve_styles()
    print(f"Referenzsatz: {REF_SENTENCE}\n")
    results = []
    for name, style in CANDIDATES:
        sid = table.get((name, style))
        if sid is None:
            print(f"  [skip] {name}/{style} nicht gefunden")
            continue
        wav, dur = synth(REF_SENTENCE, sid)
        if dur > TARGET_MAX_S:
            speed = dur / TARGET_MAX_S
            wav, dur = synth(REF_SENTENCE, sid, speed=round(speed, 2))
        safe = name.replace("/", "_").replace(".", "")
        suffix = "" if style == "ノーマル" else "_" + style.replace("/", "_").replace("／", "_")
        path = OUT_DIR / f"vv_{sid}_{safe}{suffix}.wav"
        path.write_bytes(wav)
        results.append((name, style, sid, dur, path))
        print(f"  [OK] {name}/{style} (id {sid}) -> {path.name}  ({dur:.1f}s)")

    print("\n" + "=" * 64)
    print("Zum Reinhoeren (PowerShell):")
    for name, style, sid, dur, path in results:
        print(f"  # {name}/{style}")
        print(f"  (New-Object Media.SoundPlayer '{path}').PlaySync()")
    print("=" * 64)


if __name__ == "__main__":
    main()
