r"""OCR-Smoke-Gate: gemma (Haupt-LLM) vs. LFM2.5-VL auf echten Produktfotos.

Das ist der EHRLICHE Test aus docs/listen-und-produktlesen.md (Phase L1) - er ist
KEINE Formalie: das Ergebnis ist die Weiche, ob das Listen-/Produkt-Lese-Feature (L3)
gebaut wird ODER ob als naechste Eskalation PaddleOCR-JP als dedizierte OCR-Stufe vor
das LLM muss. Darum NICHT schummeln/ueberspringen - mit echten Fotos laufen lassen.

Jagt jedes Bild in einem Ordner durch BEIDE Vision-Pfade mit demselben Produkt-Lese-
Prompt (fairer Vergleich) und legt einen Side-by-Side-Report ab. Michael faellt das
Urteil "gut genug?" an den echten Texten - das Tool liest nur vor.

Voraussetzung gemma-Pfad: aktives Ollama-Modell ist bilderfaehig (gemma >=12B). Sonst
laeuft nur LFM2.5 und der Vergleich kann nicht gefaellt werden -> das Tool sagt es klar
und bricht mit Hinweis ab.

Aufruf (aus dem Repo-Root, Haupt-venv):
    .\.venv\Scripts\python.exe tools\ocr_smoke.py [ORDNER] [--prompt "..."]

ORDNER default = ocr_samples\ (gitignored; Fotos sind privat). Report landet als
<ORDNER>\ocr_smoke_report.md.
"""
import sys
import time
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import yuki_core as yc  # noqa: E402

_IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

# Derselbe Prompt fuer BEIDE Engines = fairer Vergleich. Eicht aufs Lesen, nicht aufs
# generische Beschreiben, und verlangt Ehrlichkeit statt Raten (sonst halluziniert das
# kleine VLM Etiketten-Text und der Vergleich wird wertlos).
DEFAULT_PROMPT = (
    "This is a photo of a grocery product (likely Japanese or German). "
    "Read and transcribe ALL clearly legible text on the packaging verbatim - "
    "Japanese (kanji/kana), German or English. Then say in one short sentence what "
    "the product most likely is. If text is too small or blurry to read reliably, say "
    "so honestly for that part instead of guessing."
)


def main():
    ap = argparse.ArgumentParser(description="OCR-Smoke: gemma vs LFM2.5-VL")
    ap.add_argument("folder", nargs="?", default="ocr_samples",
                    help="Ordner mit Produktfotos (default: ocr_samples\\)")
    ap.add_argument("--prompt", default=DEFAULT_PROMPT, help="Lese-Prompt (beide Engines)")
    ap.add_argument("--max-tokens", type=int, default=yc.VISION_MAX_TOKENS_FOCUSED,
                    help="Token-Budget pro Beschreibung")
    args = ap.parse_args()

    folder = Path(args.folder)
    if not folder.is_absolute():
        folder = Path(__file__).resolve().parent.parent / folder
    if not folder.exists():
        folder.mkdir(parents=True, exist_ok=True)
        print(f"Ordner angelegt: {folder}")
        print("-> Leg deine Produktfotos hier ab und ruf das Tool erneut auf.")
        return 0

    imgs = sorted(p for p in folder.iterdir() if p.suffix.lower() in _IMG_EXT)
    if not imgs:
        print(f"Keine Bilder in {folder} (erwarte {sorted(_IMG_EXT)}).")
        return 0

    # Standalone (ohne Server): erst den Failover-Server waehlen, sonst ist OLLAMA_MODEL
    # None und der gemma-Pfad waere faelschlich "nicht verfuegbar".
    print("Waehle Ollama-Server (Failover) ...")
    yc.select_ollama_server()
    gemma_ok = yc.vision_via_main_llm_capable()
    print(f"Aktives Ollama-Modell: {yc.OLLAMA_MODEL or '(keins)'}  "
          f"(~{yc._model_size_b():.0f}B)")
    if not gemma_ok:
        print()
        print("!! gemma-Pfad NICHT verfuegbar: das aktive Modell ist nicht bilderfaehig")
        print("   (kein 'gemma' im Namen) oder unter der Groessen-Schwelle.")
        print(f"   Schwelle: gemma >= {yc.DRAW_SELF_REVIEW_MIN_MODEL_B:.0f}B.")
        print("   -> Schalte OLLAMA_MODEL auf ein gemma-Modell (z.B. 4070-Failover) und")
        print("      starte erneut. Ohne gemma kann das Smoke-Gate nicht entschieden werden.")
        return 2

    vision_up = yc.VISION_ENABLED and bool(yc.describe_image(
        imgs[0].read_bytes(), prompt="Reply with the single word: OK", max_tokens=8, quiet=True))
    if not vision_up:
        print()
        print("Hinweis: LFM2.5-VL (:8081) antwortet nicht - LFM2.5-Spalte bleibt leer.")
        print("(serve-lfm2vl.ps1 laeuft? Vergleich braucht aber beide Engines.)")

    print(f"\n{len(imgs)} Bild(er) in {folder}\n" + "=" * 72)

    rows = []
    for i, p in enumerate(imgs, 1):
        data = p.read_bytes()
        print(f"\n[{i}/{len(imgs)}] {p.name}  ({len(data)//1024} KB)")

        t0 = time.time()
        g = yc.describe_image_via_main_llm(
            data, args.prompt, system=yc.VISION_MAIN_LLM_SYS,
            max_tokens=args.max_tokens, purpose="ocr_smoke")
        g_dt = time.time() - t0

        l, l_dt = None, 0.0
        if vision_up:
            t0 = time.time()
            l = yc.describe_image(data, prompt=args.prompt, max_tokens=args.max_tokens)
            l_dt = time.time() - t0

        print(f"  -- gemma ({g_dt:.1f}s) " + "-" * 40)
        print("  " + (g or "(kein Ergebnis)").replace("\n", "\n  "))
        print(f"  -- LFM2.5 ({l_dt:.1f}s) " + "-" * 39)
        print("  " + (l or "(kein Ergebnis / Server aus)").replace("\n", "\n  "))

        rows.append((p.name, g, g_dt, l, l_dt))

    # Report schreiben (Markdown, side-by-side pro Bild).
    report = folder / "ocr_smoke_report.md"
    lines = ["# OCR-Smoke-Report: gemma vs. LFM2.5-VL", "",
             f"- Modell (gemma-Pfad): `{yc.OLLAMA_MODEL}` (~{yc._model_size_b():.0f}B)",
             f"- Bilder: {len(imgs)} aus `{folder}`",
             f"- Prompt: {args.prompt}", "",
             "> Urteil: liest **gemma** mittelgrosse Labels verlaesslich -> weiter zu L3.",
             "> Patzt gemma am kleinen Druck -> PaddleOCR-JP-Eskalation pruefen, BEVOR L3.",
             ""]
    for name, g, g_dt, l, l_dt in rows:
        lines += [f"## {name}", "",
                  f"**gemma** ({g_dt:.1f}s):", "", "> " + (g or "_(kein Ergebnis)_").replace("\n", "\n> "), "",
                  f"**LFM2.5-VL** ({l_dt:.1f}s):", "", "> " + (l or "_(kein Ergebnis / Server aus)_").replace("\n", "\n> "), "",
                  "---", ""]
    report.write_text("\n".join(lines), encoding="utf-8")
    print("\n" + "=" * 72)
    print(f"Report: {report}")
    print("Jetzt das Urteil fuellen: liest gemma die Labels gut genug fuer L3?")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
