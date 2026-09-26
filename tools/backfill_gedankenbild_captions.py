r"""
backfill_gedankenbild_captions.py - stellt die vollen Beschreibungs-Captions
bestehender Gedankenbild-Galerie-Eintraege aus ihren .md-Sidecars wieder her.

Hintergrund: die Galerie-Caption wurde frueher hart auf prompt[:120] gekuerzt in
yuki_gallery.json gespeichert (mitten im Wort). Der volle Prompt steht aber
vollstaendig im .md-Sidecar neben dem PNG (## Prompt-Sektion, save_gedankenbild).
Dieses Skript liest ihn zurueck und ersetzt die gekuerzte Caption - aber NUR,
wenn die aktuelle Caption ein echtes Praefix des vollen Prompts ist (= genau der
gekuerzte prompt[:120]-Fall). Manuell editierte/andere Captions bleiben unberuehrt.

Aufruf:
    .\.venv\Scripts\python.exe tools\backfill_gedankenbild_captions.py           # Dry-Run (nur zeigen)
    .\.venv\Scripts\python.exe tools\backfill_gedankenbild_captions.py --apply    # schreiben (mit .bak)
"""
import sys
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import yuki_core as yc


def _full_prompt_from_sidecar(stem: str):
    """Vollen Prompt aus dem .md-Sidecar ziehen. Bevorzugt die '## Prompt'-Sektion
    (kanonisch, bis EOF), sonst die erste '# '-Ueberschrift. None wenn nichts da."""
    md = yc.GEDANKENBILDER_DIR / (stem + ".md")
    if not md.is_file():
        return None
    try:
        lines = md.read_text(encoding="utf-8").splitlines()
    except Exception:
        return None
    for i, line in enumerate(lines):
        if line.strip() == "## Prompt":
            body = "\n".join(lines[i + 1:]).strip()
            if body and body != "(keiner)":
                return body
            break
    for line in lines:
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip() or None
    return None


def main() -> None:
    apply = "--apply" in sys.argv[1:]
    items = yc.load_gallery()
    changes = []
    for e in items:
        if e.get("kind") != "gedankenbild":
            continue
        file = e.get("file") or ""
        cur = (e.get("caption") or "").strip()
        full = _full_prompt_from_sidecar(Path(file).stem)
        if not full:
            continue
        full = full.strip()
        if full == cur:
            continue
        # Nur ersetzen, wenn die aktuelle Caption ein Praefix des vollen Prompts
        # ist. Sonst Finger weg (nicht der [:120]-Truncation-Fall).
        if cur and not full.startswith(cur):
            print(f"  [skip] {file}: aktuelle Caption ist kein Praefix - nicht angefasst")
            continue
        changes.append((e, cur, full))

    if not changes:
        print("Nichts zu tun - keine gekuerzten Gedankenbild-Captions gefunden.")
        return

    verb = "werden aktualisiert" if apply else "WUERDEN aktualisiert (Dry-Run)"
    print(f"{len(changes)} Eintrag(e) {verb}:\n")
    for e, cur, full in changes:
        print(f"- {e.get('file')}")
        print(f"    alt ({len(cur):>3}): {cur[:100]}{'…' if len(cur) > 100 else ''}")
        print(f"    neu ({len(full):>3}): {full[:100]}{'…' if len(full) > 100 else ''}\n")

    if not apply:
        print("Dry-Run - nichts geschrieben. Mit --apply anwenden.")
        return

    bak = yc.GALLERY_FILE.with_suffix(".json.bak")
    shutil.copy2(yc.GALLERY_FILE, bak)
    print(f"Backup: {bak}")

    for e, cur, full in changes:
        e["caption"] = full
    yc._save_gallery(items)
    print(f"Fertig - {len(changes)} Caption(s) aktualisiert in {yc.GALLERY_FILE.name}")


if __name__ == "__main__":
    main()
