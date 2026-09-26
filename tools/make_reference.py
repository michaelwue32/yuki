"""
make_reference.py – Saubere TTS-Referenzclips per VOICEVOX erzeugen.

Generiert aus mehreren weiblichen VOICEVOX-Charakteren je einen ~8s-Clip mit
identischem japanischem Text. Diese Clips sind studio-sauber (kein Rauschen),
ideal als Voice-Clone-Referenz (heute Qwen3-TTS, qwen_tts.ref_audio). Da der Text
bekannt ist, ist er zugleich der ref_text (keine Whisper-Transkription noetig).

Voraussetzung: VOICEVOX-Engine laeuft (D:\\voicevox\\windows-cpu\\run.exe) auf :50021.

Aufruf:  .\\.venv\\Scripts\\python.exe tools\\make_reference.py
Ausgabe: voices\\vv_<id>_<name>.wav  +  Konsolen-Uebersicht zum Reinhoeren.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")  # Japanische Namen auf cp1252-Konsole

import requests
import scipy.io.wavfile as wavfile
import io
from pathlib import Path

VOICEVOX = "http://127.0.0.1:50021"
# Skript lebt in tools/, Audio-Output gehoert ins Projekt-Root voices/.
OUT_DIR = Path(__file__).resolve().parent.parent / "voices"

# Ruhiger, Yuki-passender Referenzsatz (~8s). Inhalt egal fuers Klonen,
# aber so ist der Clip gleich ein huebscher Yuki-Intro.
REF_SENTENCE = "こんにちは。わたしの名前はゆきです。日本語の勉強を一緒にがんばりましょう。"

# Kandidaten: (Sprechername, Stilname) – weiblich, eher ruhig/erwachsen.
CANDIDATES = [
    ("九州そら", "ノーマル"),
    ("No.7", "ノーマル"),
    ("WhiteCUL", "ノーマル"),
    ("冥鳴ひまり", "ノーマル"),
    ("もち子さん", "ノーマル"),
    ("波音リツ", "ノーマル"),
    # --- etwas aeltere/reifere Stimmen (Stil macht hier viel aus) ---
    ("波音リツ", "クイーン"),       # regal, deutlich reifer
    ("九州そら", "セクシー"),       # tiefer, ruhig-erwachsen
    ("東北イタコ", "ノーマル"),     # ruhige, reife erwachsene Frau
    ("No.7", "読み聞かせ"),         # gesetzt, vortragend-ruhig
    ("もち子さん", "のんびり"),     # entspanntere Variante der aktiven Stimme
    ("猫使ビィ", "おちつき"),       # gefasst/kuehl, tiefer
    ("ぞん子", "低血圧"),           # traege/ruhig, wirkt reifer
]

TARGET_MAX_S = 9.5   # GPT-SoVITS verlangt strikt 3-10s


def resolve_styles():
    """Sprechername+Stil -> style_id Mapping von der Engine holen."""
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
        # Falls >10s: leicht beschleunigen, damit es ins 3-10s-Fenster passt
        if dur > TARGET_MAX_S:
            speed = dur / TARGET_MAX_S
            wav, dur = synth(REF_SENTENCE, sid, speed=round(speed, 2))
        safe = name.replace("/", "_").replace(".", "")
        # Nicht-Normal-Stile in den Dateinamen, damit man Varianten unterscheiden kann
        # (ノーマル bleibt suffixlos -> bestehende Referenzdateien aendern sich nicht).
        suffix = "" if style == "ノーマル" else "_" + style.replace("/", "_").replace("／", "_")
        path = OUT_DIR / f"vv_{sid}_{safe}{suffix}.wav"
        path.write_bytes(wav)
        results.append((name, style, sid, dur, path))
        print(f"  [OK] {name}/{style} (id {sid}) -> {path.name}  ({dur:.1f}s)")

    print("\n" + "=" * 64)
    print("Zum Reinhoeren (PowerShell):")
    for name, style, sid, dur, path in results:
        print(f"  # {name}: ")
        print(f"  (New-Object Media.SoundPlayer '{path}').PlaySync()")
    print("=" * 64)
    print("\nGewaehlten Clip in yuki_core.py eintragen (REF_TEXT bleibt gleich, da gleicher Satz):")
    print(f'  REF_AUDIO = r"<pfad zur gewaehlten vv_*.wav>"')
    print(f'  REF_TEXT  = "{REF_SENTENCE}"')


if __name__ == "__main__":
    main()
