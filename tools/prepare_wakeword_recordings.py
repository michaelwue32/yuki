"""Pre-Process echter Wake-Word-Aufnahmen fuer openwakeword-Training.

Pipeline pro Input-Datei (m4a/aac/wav/mp3/...):
  1) Load + Resample auf 16 kHz mono float32
  2) Finde lautestes 1.5s-Fenster (rolling RMS) - praktisch immer das
     Wake-Word, weil der User es bewusst gesprochen hat
  3) Optional: kleine Headroom-Normalisierung (peak auf -3dB)
  4) Schreibe als 16kHz mono PCM_16 WAV nach DST_DIR

Idempotent: Output-Files werden NICHT ueberschrieben. Re-Run skippt bereits
verarbeitete Files. Loescht NICHTS in SRC_DIR.

Voraussetzungen (siehe docs/setup-wakeword-training.md Phase B):
  pip install soundfile librosa numpy
  ffmpeg im PATH (fuer m4a/aac/mp3-Decoding; librosa nutzt audioread/soundfile fallback)

Run:
    cd D:\\Wakeword
    .\\venv\\Scripts\\Activate.ps1
    python D:\\Projects\\yuki\\tools\\prepare_wakeword_recordings.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import soundfile as sf

try:
    import librosa
except ImportError:
    print("librosa fehlt. Im D:\\Wakeword\\venv: pip install librosa")
    sys.exit(1)


SRC_DIR = Path(r"D:\Wakeword\recordings\raw")
DST_DIR = Path(r"D:\Wakeword\recordings\positive")

TARGET_SR = 16000
WINDOW_SEC = 1.5
TARGET_PEAK_DB = -3.0  # leicht unter Vollaussteuerung

# Akzeptierte Eingabe-Formate
SRC_EXTENSIONS = {".m4a", ".aac", ".mp3", ".wav", ".flac", ".ogg", ".opus", ".amr"}


def load_mono_16k(path: Path) -> np.ndarray:
    """Decode -> mono float32 -> 16 kHz."""
    # soundfile schlaegt bei m4a/aac fehl -> librosa.load mit ffmpeg-Backend
    try:
        data, sr = sf.read(str(path), dtype="float32", always_2d=False)
    except Exception:
        data, sr = librosa.load(str(path), sr=None, mono=False)
        data = data.astype(np.float32)

    if data.ndim > 1:
        data = data.mean(axis=-1)  # mono mixdown

    if sr != TARGET_SR:
        data = librosa.resample(data, orig_sr=sr, target_sr=TARGET_SR)
    return data.astype(np.float32)


def find_loudest_window(audio: np.ndarray, sr: int, window_sec: float) -> np.ndarray:
    """Schiebe ein window_sec-Fenster ueber das Signal, gib das Fenster mit
    hoechster RMS-Energie zurueck.
    """
    n_window = int(window_sec * sr)
    n_total = audio.shape[0]
    if n_total <= n_window:
        # Zu kurz - paddet symmetrisch
        pad = n_window - n_total
        return np.pad(audio, (pad // 2, pad - pad // 2), mode="constant")

    # Rolling sum of squares via cumsum-Trick
    sq = audio.astype(np.float64) ** 2
    cs = np.cumsum(sq)
    window_energy = cs[n_window - 1:] - np.concatenate(([0.0], cs[:-n_window]))
    start = int(np.argmax(window_energy))
    return audio[start:start + n_window]


def normalize_peak(audio: np.ndarray, target_db: float) -> np.ndarray:
    peak = float(np.max(np.abs(audio)))
    if peak < 1e-6:
        return audio
    target_amp = 10.0 ** (target_db / 20.0)
    return (audio / peak * target_amp).astype(np.float32)


def main() -> int:
    if not SRC_DIR.exists():
        print(f"Kein Source-Folder: {SRC_DIR}")
        print(f"Lege ihn an und kopiere deine Handy-Aufnahmen rein.")
        return 1

    DST_DIR.mkdir(parents=True, exist_ok=True)

    # Existing Outputs zaehlen damit wir die Nummerierung fortsetzen
    existing = sorted(DST_DIR.glob("*.wav"))
    next_idx = 1
    if existing:
        # ermittle hoechsten existierenden Index
        for p in existing:
            try:
                next_idx = max(next_idx, int(p.stem) + 1)
            except ValueError:
                pass

    sources = sorted([p for p in SRC_DIR.iterdir() if p.suffix.lower() in SRC_EXTENSIONS])
    if not sources:
        print(f"Keine Audio-Files in {SRC_DIR}")
        print(f"Akzeptiert: {sorted(SRC_EXTENSIONS)}")
        return 1

    # Welche Sources sind schon verarbeitet? Wir tracken via marker-File ".processed"
    marker = DST_DIR / ".processed.txt"
    processed = set()
    if marker.exists():
        processed = set(line.strip() for line in marker.read_text(encoding="utf-8").splitlines() if line.strip())

    new_marker_entries = []
    n_done = 0
    n_skip = 0

    for src in sources:
        key = src.name
        if key in processed:
            n_skip += 1
            continue

        try:
            audio = load_mono_16k(src)
        except Exception as e:
            print(f"  [skip] {key}  Decode-Fehler: {e}")
            continue

        win = find_loudest_window(audio, TARGET_SR, WINDOW_SEC)
        win = normalize_peak(win, TARGET_PEAK_DB)

        # Klippt auf int16 (PCM_16 ist openwakeword-Convention)
        win_i16 = np.clip(win * 32767.0, -32768, 32767).astype(np.int16)

        out_path = DST_DIR / f"{next_idx:03d}.wav"
        sf.write(str(out_path), win_i16, TARGET_SR, subtype="PCM_16")
        print(f"  [+] {key}  ({len(audio) / TARGET_SR:.1f}s)  ->  {out_path.name}")
        new_marker_entries.append(key)
        next_idx += 1
        n_done += 1

    if new_marker_entries:
        with marker.open("a", encoding="utf-8") as f:
            for k in new_marker_entries:
                f.write(k + "\n")

    print()
    print(f"Verarbeitet: {n_done}, uebersprungen: {n_skip}")
    print(f"Output-Folder: {DST_DIR}  ({len(list(DST_DIR.glob('*.wav')))} WAVs gesamt)")
    print()
    print("Naechste Schritte:")
    print(f"  Copy-Item {DST_DIR}\\*.wav D:\\Wakeword\\openWakeWord\\positive_train\\")
    print(f"  python openwakeword\\openwakeword\\train.py --training_config my_model.yaml --augment_clips")
    print(f"  python openwakeword\\openwakeword\\train.py --training_config my_model.yaml --train_model")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
