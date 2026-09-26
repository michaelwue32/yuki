#!/usr/bin/env python3
"""
Yuki Wake-Word Training Pipeline (Windows + Blackwell)
=======================================================
Replaces the Linux-only Colab notebook notebooks/automatic_model_training.ipynb
from openwakeword. Adds Windows-compat shims discovered during setup:
- Phase D.2 (Audioset): the bal_train09.tar URL from the notebook now 404s;
                         agkphysics/AudioSet uses parquet under data/bal_train/*.parquet.
                         Switched to datasets.load_dataset(... "balanced" ... streaming=True)
                         with a 2000-clip slice (≈5.5h background).
- Phase D.4 (Features): urllib.urlretrieve instead of !wget
- Phase E:    target_false_positives_per_hour (not _activations_), batch_n_per_class
              stays a Dict, piper_sample_generator_path → repo path (not pip wheel)
Hardcoded paths assume the layout from docs/setup-wakeword-training.md.

Usage:
    py train_ohayoo_yuki.py --download    # Phase D (~25 GB, ~38 min @ 90 Mbit/s)
    py train_ohayoo_yuki.py --config      # Phase E (write ohayoo_yuki.yaml)
    py train_ohayoo_yuki.py --generate    # Phase F.1 (GPU, synthetic clips)
    py train_ohayoo_yuki.py --augment     # Phase F.2 (CPU)
    py train_ohayoo_yuki.py --train       # Phase F.3 (GPU-heavy)
    py train_ohayoo_yuki.py --status      # show what's done so far
"""

import argparse
import glob
import os
import subprocess
import sys
import urllib.request
from pathlib import Path


# Windows + Python 3.8+: ctypes.CDLL ignores PATH for DLL search.
# torchcodec (transitive dep of datasets 4.8+) needs FFmpeg's avcodec-*.dll;
# without add_dll_directory(), it falls back to error even with FFmpeg on PATH.
def _register_ffmpeg_dlls():
    candidates = glob.glob(os.path.expandvars(
        r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg.Shared_*\ffmpeg-*-full_build-shared\bin"
    ))
    if candidates:
        os.add_dll_directory(candidates[0])
        return candidates[0]
    return None


_FFMPEG_BIN = _register_ffmpeg_dlls()
if not _FFMPEG_BIN:
    print("WARNING: FFmpeg shared DLLs not found via winget.", flush=True)
    print("Install with: winget install Gyan.FFmpeg.Shared", flush=True)

# ── Hard paths ─────────────────────────────────────────────────────────────
WAKEWORD_ROOT = Path(r"D:\Wakeword")
OWW_REPO      = WAKEWORD_ROOT / "openWakeWord"
PIPER_REPO    = WAKEWORD_ROOT / "piper-sample-generator"
VENV_PY       = WAKEWORD_ROOT / "venv" / "Scripts" / "python.exe"

# Paths relative to OWW_REPO (because the YAML uses relative paths)
MIT_RIRS_DIR        = "mit_rirs"
AUDIOSET_16K_DIR    = "audioset_16k"
FMA_DIR             = "fma"
ACAV_FEATURES_FILE  = "openwakeword_features_ACAV100M_2000_hrs_16bit.npy"
VAL_FEATURES_FILE   = "validation_set_features.npy"
YAML_FILE           = "ohayoo_yuki.yaml"

ACAV_FEATURES_URL   = "https://huggingface.co/datasets/davidscripka/openwakeword_features/resolve/main/openwakeword_features_ACAV100M_2000_hrs_16bit.npy"
VAL_FEATURES_URL    = "https://huggingface.co/datasets/davidscripka/openwakeword_features/resolve/main/validation_set_features.npy"


def banner(msg):
    bar = "=" * 70
    print(f"\n{bar}\n  {msg}\n{bar}", flush=True)


def decode_audio_row(row, fallback_stem):
    """datasets 4.x changed Audio feature: row['audio'] is AudioDecoder, not dict.
    Returns (filename, int16 numpy array, sample_rate) ready for scipy.io.wavfile.write."""
    import numpy as np
    ad = row["audio"]
    samples = ad.get_all_samples()
    data = samples.data  # Tensor[channels, frames] float32 in [-1, 1]
    if data.dim() == 2 and data.shape[0] > 1:
        data = data.mean(dim=0)
    elif data.dim() == 2:
        data = data[0]
    arr = (data.numpy() * 32767).clip(-32768, 32767).astype(np.int16)
    raw_path = getattr(ad.metadata, "path", None) or fallback_stem
    name = Path(raw_path).name or fallback_stem
    if not name.lower().endswith(".wav"):
        name = Path(name).stem + ".wav"
    return name, arr, samples.sample_rate


def reporthook_factory(label):
    last_pct = [-1]
    def hook(blocknum, blocksize, totalsize):
        if totalsize <= 0:
            return
        pct = int(blocknum * blocksize * 100 / totalsize)
        if pct != last_pct[0] and pct % 2 == 0:
            mb_done  = blocknum * blocksize / 1024 / 1024
            mb_total = totalsize / 1024 / 1024
            print(f"  {label}: {pct:3d}%  ({mb_done:.0f}/{mb_total:.0f} MB)", flush=True)
            last_pct[0] = pct
    return hook


def urlretrieve(url, dest, label):
    dest = Path(dest)
    if dest.exists() and dest.stat().st_size > 1024:
        print(f"  {label} already at {dest} ({dest.stat().st_size/1024/1024:.0f} MB), skip", flush=True)
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    urllib.request.urlretrieve(url, tmp, reporthook=reporthook_factory(label))
    tmp.rename(dest)


# ── Phase D ────────────────────────────────────────────────────────────────
def phase_d_download():
    banner("Phase D — Download training data (~25 GB)")
    os.chdir(OWW_REPO)
    print(f"cwd: {os.getcwd()}", flush=True)

    # --- D.1 MIT Room Impulse Responses (~600 MB) ---
    print("\n[D.1] MIT environmental impulse responses (~600 MB)", flush=True)
    out = Path(MIT_RIRS_DIR)
    out.mkdir(exist_ok=True)
    existing = list(out.glob("*.wav"))
    if len(existing) > 100:
        print(f"  {len(existing)} WAVs already in {out}, skip", flush=True)
    else:
        import scipy.io.wavfile
        from tqdm import tqdm
        import datasets
        import uuid
        ds = datasets.load_dataset(
            "davidscripka/MIT_environmental_impulse_responses",
            split="train", streaming=True,
        )
        for row in tqdm(ds, desc="  MIT RIRs"):
            name, arr, sr = decode_audio_row(row, f"rir_{uuid.uuid4().hex}.wav")
            scipy.io.wavfile.write(out / name, sr, arr)

    # --- D.2 Audioset balanced (streaming, ~2000 clips ≈ 5.5h of background) ---
    # Format changed 2024+: agkphysics/AudioSet is now parquet under data/bal_train/*.parquet,
    # not the old .tar from the notebook (which 404s). Stream a slice of `balanced` config.
    print("\n[D.2] Audioset balanced (~2000 clips for background mix)", flush=True)
    out16k = Path(AUDIOSET_16K_DIR)
    out16k.mkdir(exist_ok=True)
    audioset_n_max = 2000
    existing = len(list(out16k.glob("*.wav")))
    if existing >= audioset_n_max * 0.95:
        print(f"  audioset_16k already has {existing} WAVs, skip", flush=True)
    else:
        import scipy.io.wavfile
        from tqdm import tqdm
        import datasets
        import uuid
        ds = datasets.load_dataset(
            "agkphysics/AudioSet", "balanced", split="train", streaming=True,
        )
        ds = ds.cast_column("audio", datasets.Audio(sampling_rate=16000))
        for i, row in enumerate(tqdm(ds, desc="  audioset", total=audioset_n_max)):
            if i >= audioset_n_max:
                break
            try:
                name, arr, sr = decode_audio_row(row, f"as_{uuid.uuid4().hex}.wav")
                scipy.io.wavfile.write(out16k / name, sr, arr)
            except Exception as e:
                print(f"  skip row {i}: {e}", flush=True)

    # --- D.3 FMA — SKIPPED ---
    # rudraml/fma is a dataset-script loader; datasets 4.x dropped script support
    # ("Dataset scripts are no longer supported, but found fma.py").
    # The Audioset slice from D.2 already covers music (label /m/04rlf), so FMA
    # is redundant — Phase E uses only audioset_16k as background_paths.
    print("\n[D.3] FMA — skipped (datasets 4.x dropped script loaders; Audioset covers music)", flush=True)

    # --- D.4 Pre-computed openwakeword features ---
    print("\n[D.4] ACAV100M pre-computed features (~16 GB)", flush=True)
    urlretrieve(ACAV_FEATURES_URL, ACAV_FEATURES_FILE, "ACAV100M.npy")

    print("\n[D.5] Validation set features (~150 MB)", flush=True)
    urlretrieve(VAL_FEATURES_URL, VAL_FEATURES_FILE, "validation.npy")

    banner("Phase D done")


# ── Phase E ────────────────────────────────────────────────────────────────
def phase_e_config():
    banner("Phase E — Write ohayoo_yuki.yaml")
    os.chdir(OWW_REPO)
    import yaml

    with open("examples/custom_model.yml", "r", encoding="utf-8") as f:
        config = yaml.load(f, yaml.Loader)

    # ── target phrase variants ─────────────────────────────────────────
    # Single phrase: Adversarial-text-generation runs once per variant, so 3 variants
    # would 3x the generate_adversarial_texts work without proportional sample gain
    # (train.py splits n_samples//len(target_phrase) for positives anyway).
    config["target_phrase"] = ["ohayoo yuki"]
    config["model_name"] = "ohayoo_yuki"

    # ── sample counts ──────────────────────────────────────────────────
    # Full Doku target. Safe to attempt now that:
    #   - generate_samples.py has the empty_cache/del/gc patch between batches
    #   - tts_batch_size=16 (50 was OOM-cycling and triggered the leak hard)
    config["n_samples"]      = 30000
    config["n_samples_val"]  = 3000
    config["tts_batch_size"] = 16

    # ── training behaviour ─────────────────────────────────────────────
    config["target_false_positives_per_hour"] = 0.5   # NOTE: real key, not "_activations_"
    config["augmentation_rounds"] = 2
    config["max_negative_weight"] = 1500

    # piper_sample_generator_path: absolute path to dscripka-fork repo
    config["piper_sample_generator_path"] = str(PIPER_REPO).replace("\\", "/")

    # Background paths — populated by Phase D (FMA dropped, see D.3 comment)
    config["background_paths"] = [f"./{AUDIOSET_16K_DIR}"]
    config["background_paths_duplication_rate"] = [1]

    # Pre-computed features
    config["false_positive_validation_data_path"] = VAL_FEATURES_FILE
    config["feature_data_files"] = {"ACAV100M_sample": ACAV_FEATURES_FILE}

    # batch_n_per_class stays a Dict (NOT 128 as the Doku misleadingly says).
    # Default is good. Keep ACAV100M_sample/adversarial_negative/positive.

    with open(YAML_FILE, "w", encoding="utf-8") as f:
        yaml.dump(config, f, sort_keys=False, allow_unicode=True)

    print(f"Wrote {OWW_REPO / YAML_FILE}", flush=True)
    print("\n--- key config values ---", flush=True)
    for k in [
        "target_phrase", "model_name", "n_samples", "n_samples_val",
        "augmentation_rounds", "max_negative_weight",
        "target_false_positives_per_hour", "piper_sample_generator_path",
        "background_paths", "feature_data_files", "batch_n_per_class",
    ]:
        print(f"  {k:40s}: {config[k]!r}", flush=True)

    banner("Phase E done")


# ── Phase F ────────────────────────────────────────────────────────────────
def run_train_py(flag):
    """Run openwakeword/train.py with given flag from inside OWW_REPO."""
    os.chdir(OWW_REPO)
    cmd = [str(VENV_PY), "openwakeword/train.py", "--training_config", YAML_FILE, flag]
    print(f"\n$ {' '.join(cmd)}", flush=True)
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"   # for the Phonemizer-shim's UTF-8 phoneme output
    rc = subprocess.run(cmd, env=env).returncode
    # 0xC0000409 = STATUS_STACK_BUFFER_OVERRUN — Windows cleanup crash after work is
    # complete (PyTorch+CUDA shutdown drift). Cosmetic; caller should verify output.
    WIN_CLEANUP_CRASH = 0xC0000409  # 3221226505
    if rc not in (0, WIN_CLEANUP_CRASH):
        sys.exit(f"train.py exited with code {rc}")
    if rc == WIN_CLEANUP_CRASH:
        print(f"warn: Windows shutdown crash 0x{rc:08x} after work completed — continuing.", flush=True)


def phase_f1_generate():
    banner("Phase F.1 — Generate synthetic clips (GPU, ~30 min on 5090)")
    run_train_py("--generate_clips")
    banner("Phase F.1 done")


def phase_f2_augment():
    banner("Phase F.2 — Augment clips (CPU, ~5-10 min)")
    # Pre-clean stale .npy files: train.py only checks positive_features_train.npy
    # as skip-marker, but earlier crashes can leave pre-allocated empty memmaps that
    # silently make subsequent runs no-op. Force a clean state.
    feature_dir = OWW_REPO / "my_custom_model" / "ohayoo_yuki"
    for npy in feature_dir.glob("*_features_*.npy"):
        print(f"  removing stale {npy.name} ({npy.stat().st_size//1024//1024} MB)", flush=True)
        npy.unlink()
    run_train_py("--augment_clips")
    banner("Phase F.2 done")


def phase_f3_train():
    banner("Phase F.3 — Train model (GPU-heavy, ~30-60 min on 5090)")
    run_train_py("--train_model")
    banner("Phase F.3 done — check my_custom_model/ohayoo_yuki/ for .onnx")


# ── Status ─────────────────────────────────────────────────────────────────
def phase_status():
    banner("Status check")
    os.chdir(OWW_REPO)
    checks = [
        ("MIT RIRs WAVs",       lambda: len(list(Path(MIT_RIRS_DIR).glob("*.wav"))) if Path(MIT_RIRS_DIR).exists() else 0),
        ("Audioset 16k WAVs",   lambda: len(list(Path(AUDIOSET_16K_DIR).glob("*.wav"))) if Path(AUDIOSET_16K_DIR).exists() else 0),
        ("ACAV100M.npy",        lambda: Path(ACAV_FEATURES_FILE).stat().st_size if Path(ACAV_FEATURES_FILE).exists() else 0),
        ("Validation.npy",      lambda: Path(VAL_FEATURES_FILE).stat().st_size if Path(VAL_FEATURES_FILE).exists() else 0),
        ("ohayoo_yuki.yaml",    lambda: Path(YAML_FILE).exists()),
        ("positive_train",      lambda: len(list(Path("my_custom_model/ohayoo_yuki/positive_train").glob("*.wav"))) if Path("my_custom_model/ohayoo_yuki/positive_train").exists() else 0),
        ("ohayoo_yuki.onnx",    lambda: Path("my_custom_model/ohayoo_yuki/ohayoo_yuki.onnx").exists()),
    ]
    for label, fn in checks:
        try:
            v = fn()
        except Exception as e:
            v = f"ERR: {e}"
        print(f"  {label:24s}: {v}", flush=True)


# ── Main ───────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--download", action="store_true", help="Phase D — fetch datasets")
    ap.add_argument("--config",   action="store_true", help="Phase E — write YAML")
    ap.add_argument("--generate", action="store_true", help="Phase F.1 — synthetic clips")
    ap.add_argument("--augment",  action="store_true", help="Phase F.2 — augment")
    ap.add_argument("--train",    action="store_true", help="Phase F.3 — train model")
    ap.add_argument("--status",   action="store_true", help="Show progress")
    args = ap.parse_args()

    if not any(vars(args).values()):
        ap.print_help()
        sys.exit(1)

    if args.status:   phase_status()
    if args.download: phase_d_download()
    if args.config:   phase_e_config()
    if args.generate: phase_f1_generate()
    if args.augment:  phase_f2_augment()
    if args.train:    phase_f3_train()


if __name__ == "__main__":
    main()
