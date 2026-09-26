# Code-Snippets (funktionierend!)

Lade diese Datei, wenn du eine der drei Pipelines (Whisper-CUDA, GPT-SoVITS, Ollama)
neu anbindest oder debuggst.

## CUDA-DLLs für faster-whisper laden (Windows-Quirk)

Muss **vor jedem Import von faster_whisper** stehen:

```python
import os
import sys
from pathlib import Path

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
```

## GPT-SoVITS API-Request (funktioniert!)

```python
import requests

payload = {
    "text": "こんにちは。私はゆきです。",
    "text_lang": "ja",
    "ref_audio_path": r"D:\Projects\yuki\voices\test2.wav",
    "prompt_text": "DEIN_JAPANISCHER_REFERENZ_TEXT",  # Whisper-Transkription des ref_audio
    "prompt_lang": "ja",
    "top_k": 15,
    "top_p": 1.0,
    "temperature": 1.0,
    "speed_factor": 1.0,
}
response = requests.post("http://localhost:9880/tts", json=payload)
with open("output.wav", "wb") as f:
    f.write(response.content)
```

## Ollama-Request

```python
import requests
response = requests.post("http://localhost:11434/api/chat", json={
    "model": "qwen3:8b",
    "messages": [
        {"role": "system", "content": "Du bist Yuki..."},
        {"role": "user", "content": "Hallo"}
    ],
    "stream": False
})
reply = response.json()["message"]["content"]
```
