import os
import sys
from pathlib import Path

# CUDA-DLLs
venv_root = Path(sys.executable).parent.parent
nvidia_root = venv_root / "Lib" / "site-packages" / "nvidia"
dll_paths = []
if nvidia_root.exists():
    for bin_dir in nvidia_root.rglob("bin"):
        if bin_dir.is_dir():
            dll_paths.append(str(bin_dir))
            os.add_dll_directory(str(bin_dir))
if dll_paths:
    os.environ["PATH"] = os.pathsep.join(dll_paths) + os.pathsep + os.environ.get("PATH", "")

from faster_whisper import WhisperModel

if len(sys.argv) < 2:
    print("Aufruf: python transcribe_reference.py <pfad_zur_wav>")
    sys.exit(1)

audio_path = sys.argv[1]
print(f"Transkribiere: {audio_path}")

# large-v3 für beste Japanisch-Qualität bei Referenz-Audio
# (auch wenn größer/langsamer – wir machen das ja nur einmal)
model = WhisperModel("large-v3", device="cuda", compute_type="float16")

# Sprache explizit auf Japanisch setzen für höhere Genauigkeit
segments, info = model.transcribe(audio_path, beam_size=5, language="ja")

print(f"\nSprache: {info.language} (Wahrscheinlichkeit: {info.language_probability:.2f})\n")
print("=" * 60)
print("Reference Text (fuer qwen_tts.ref_text):")
print("=" * 60)

full_text = ""
for segment in segments:
    full_text += segment.text
    
print(full_text.strip())
print("=" * 60)