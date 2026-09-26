"""
Reapply-Skript: klemmt out-of-range Token-IDs im CUDAGraph-Pfad von faster-qwen3-tts.
====================================================================================
faster-qwen3-tts 0.4.0 emittiert im CUDAGraph-Schnellpfad (fast_generate) selten eine
out-of-range Codebook/Token-ID -> CUDA `index out of bounds` device-side assert, der den
Prozess vergiftet (danach alle TTS-Requests 500, nur Neustart hilft). Der Fix klemmt die
IDs auf den gueltigen Embedding-Bereich (No-Op fuer gueltige IDs; garbage-ID -> gueltig =
winziger Audio-Glitch statt Crash). CUDAGraph-Speed bleibt.

Der Patch sitzt in der venv-Library und geht bei einer (Neu-)Installation von
faster-qwen3-tts verloren. DIESES Skript nach jeder Installation ausfuehren:

    <venv>\\Scripts\\python.exe tools\\patch_qwen_cudagraph.py [<venv-pfad>]

Der venv-Pfad ist konfigurierbar (kein Quellcode-Edit noetig), Prioritaet:
  1. CLI-Argument (sys.argv[1]) - der venv-Ordner ODER direkt die generate.py
  2. Umgebungsvariable QWEN_TTS_VENV (venv-Ordner)
  3. Default D:\\Server\\qwen3-tts\\venv
Wird die Ziel-Datei nicht gefunden, bricht das Skript mit klarer Meldung ab (es
patcht NIE eine andere Datei). Idempotent (meldet "bereits gepatcht", wenn schon
drin). Siehe docs/setup-qwen-tts.md. Original-Backup: generate.py.bak-yuki.
"""
import os
import pathlib
import sys

_REL = "Lib/site-packages/faster_qwen3_tts/generate.py"
_DEFAULT_VENV = r"D:\Server\qwen3-tts\venv"


def _resolve_gen() -> pathlib.Path:
    """generate.py aus Arg/Env/Default bestimmen. Arg darf venv-Ordner ODER die
    generate.py direkt sein."""
    src = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("QWEN_TTS_VENV", _DEFAULT_VENV)
    p = pathlib.Path(src)
    return p if p.name == "generate.py" else p / _REL


GEN = _resolve_gen()

A_OLD = "        last_id_hidden = talker_codec_embed(token.unsqueeze(1))  # [1, 1, H]"
A_NEW = (
    "        # PATCH (Yuki): CUDAGraph-Predictor liefert selten out-of-range Token-IDs\n"
    "        # -> CUDA index-oob-Assert (Prozess tot). Defensiv klemmen (No-Op fuer gueltige).\n"
    "        token = token.clamp(0, talker_codec_embed.num_embeddings - 1)\n"
    "        last_id_hidden = talker_codec_embed(token.unsqueeze(1))  # [1, 1, H]"
)
B_OLD = "        codebook_token_ids = predictor_graph.run(pred_input)  # [15] long tensor"
B_NEW = (
    "        codebook_token_ids = predictor_graph.run(pred_input)  # [15] long tensor\n"
    "        # PATCH (Yuki): jede Codebook-ID auf ihren Embedding-Bereich klemmen.\n"
    "        for _pi in range(min(len(predictor_codec_embeds), codebook_token_ids.shape[0])):\n"
    "            codebook_token_ids[_pi].clamp_(0, predictor_codec_embeds[_pi].num_embeddings - 1)"
)


def main():
    if not GEN.is_file():
        print(f"FEHLER: {GEN} nicht gefunden - faster-qwen3-tts installiert?")
        sys.exit(1)
    src = GEN.read_text(encoding="utf-8")
    if "PATCH (Yuki)" in src:
        print("bereits gepatcht - nichts zu tun.")
        return
    if A_OLD not in src or B_OLD not in src:
        print("FEHLER: Anker-Zeilen nicht gefunden - faster-qwen3-tts-Version geaendert? "
              "Patch manuell pruefen (docs/setup-qwen-tts.md).")
        sys.exit(2)
    bak = GEN.with_suffix(".py.bak-yuki")
    if not bak.exists():
        bak.write_text(src, encoding="utf-8")
        print(f"Backup: {bak}")
    src = src.replace(A_OLD, A_NEW, 1).replace(B_OLD, B_NEW, 1)
    GEN.write_text(src, encoding="utf-8")
    print("gepatcht: Token-ID-Clamps eingefuegt. Qwen-Service neu starten.")


if __name__ == "__main__":
    main()
