"""Deterministischer Public-Export von Yuki (yuki-public).

Baut aus dem privaten Repo einen bereinigten Tree: lizenzbehaftete Medien +
interne Docs raus, interne Tokens ersetzt, Public-Dateisatz overlayt, Leak-Scan
als Build-Gate. Nur stdlib.

Details: docs/superpowers/specs/2026-09-25-yuki-public-export-design.md
"""
from __future__ import annotations

import fnmatch
import os
import stat
import sys
from pathlib import Path

# config_loader liegt im Projekt-Root; _strip_jsonc wiederverwenden (DRY).
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
from config_loader import _strip_jsonc  # noqa: E402
import argparse  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402


def load_manifest(path: Path) -> dict:
    """JSONC-Manifest laden (Kommentare erlaubt)."""
    raw = Path(path).read_text(encoding="utf-8")
    return json.loads(_strip_jsonc(raw)) or {}


def _matches_any(rel_path: str, globs) -> bool:
    rp = rel_path.replace("\\", "/")
    for g in globs or []:
        # fnmatch behandelt '**' nicht rekursiv -> zusaetzlicher Prefix-Check.
        if fnmatch.fnmatch(rp, g):
            return True
        if g.endswith("/**") and (rp == g[:-3] or rp.startswith(g[:-2])):
            return True
    return False


def path_included(rel_path: str, manifest: dict) -> bool:
    """True, wenn die Datei in den Public-Export gehoert."""
    rp = rel_path.replace("\\", "/")
    if _matches_any(rp, manifest.get("exclude_globs")):
        return False
    if rp.startswith("docs/") and not _matches_any(rp, manifest.get("docs_allow_globs")):
        return False
    return True


def should_sanitize(rel_path: str, manifest: dict) -> bool:
    """True nur fuer Textdateien, deren Endung in sanitize.extensions steht.
    Binaerdateien (.vrm/.wav/.png/...) werden nie sanitized (Korruptionsschutz)."""
    exts = manifest.get("sanitize", {}).get("extensions", [])
    return Path(rel_path).suffix.lower() in [e.lower() for e in exts]


def sanitize_text(text: str, replacements) -> tuple[str, int]:
    """Alle replacements (je {pattern, repl}) als re.subn anwenden.
    Gibt (neuer_text, gesamt_anzahl_ersetzungen) zurueck."""
    total = 0
    for r in replacements or []:
        # IGNORECASE, damit z.B. all-caps "example" (OCR-Anekdote) genauso
        # bereinigt wird wie der Leak-Scan (der ebenfalls case-insensitiv sucht).
        text, n = re.subn(r["pattern"], r["repl"], text, flags=re.IGNORECASE)
        total += n
    return text, total


def scan_for_leaks(root: Path, forbidden, skip_exts) -> list[tuple[str, int, str]]:
    """Als Gate: JEDE Datei unter root zeilenweise gegen die Forbidden-Patterns
    pruefen (case-insensitive). Uebersprungen wird nur, was (a) eine bekannte
    Binaer-Endung (skip_exts) hat ODER (b) nicht als UTF-8 dekodierbar ist.
    Text-by-default statt Endungs-Allowlist -> auch .template/extensionslose
    Skripte werden geprueft (Review-Fix #3). Rueckgabe je Treffer:
    (rel_path, lineno, pattern). Leere Liste = sauber."""
    root = Path(root)
    skip = {e.lower() for e in (skip_exts or [])}
    pats = [(p, re.compile(p, re.IGNORECASE)) for p in (forbidden or [])]
    hits: list[tuple[str, int, str]] = []
    for f in sorted(root.rglob("*")):
        if not f.is_file() or f.suffix.lower() in skip:
            continue
        rel = f.relative_to(root).as_posix()
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, ValueError):
            continue  # nicht als Text dekodierbar -> Binaer, skip
        for i, line in enumerate(lines, 1):
            for raw, rx in pats:
                if rx.search(line):
                    hits.append((rel, i, raw))
    return hits


def _force_rmtree(path: Path) -> None:
    """rmtree, das unter Windows auch read-only Dateien entfernt (z.B. die
    read-only git-Objekte in einem .git des Ziel-Ordners) - sonst WinError 5."""
    def _onexc(func, p, exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    shutil.rmtree(path, onexc=_onexc)


def list_tracked_files(repo_root: Path) -> list[str]:
    """git ls-files (nur getrackte Dateien), sortiert.

    -z (NUL-separiert) statt Zeilen: sonst QUOTET git Pfade mit Nicht-ASCII-
    Zeichen (z.B. die japanischen voices/*.wav) in "..."-Form mit Oktal-Escapes,
    was weder den Exclude-Glob matcht noch ein gueltiger Pfad ist. Bytes explizit
    als UTF-8 dekodieren (git speichert Pfade als UTF-8-Bytes; text=True wuerde
    auf Windows cp1252 nehmen und die Japanisch-Namen zerlegen)."""
    res = subprocess.run(["git", "-C", str(repo_root), "ls-files", "-z"],
                         capture_output=True, check=True)
    paths = res.stdout.decode("utf-8").split("\0")
    return sorted(p for p in paths if p.strip())


def build_export(repo_root, out_dir, manifest, public_dir, source_files) -> dict:
    """Bereinigten Export-Tree bauen: filtern, sanitizen, Public-Dateisatz
    overlayen, Leak-Scan. Report-Dict inkl. 'leaks' (leer = sauber)."""
    repo_root, out_dir, public_dir = Path(repo_root), Path(out_dir), Path(public_dir)
    if out_dir.exists():
        _force_rmtree(out_dir)
    out_dir.mkdir(parents=True)
    copied = excluded = subs = overlaid = 0
    for rel in sorted(source_files):
        if not path_included(rel, manifest):
            excluded += 1
            continue
        src = repo_root / rel
        dst = out_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if should_sanitize(rel, manifest):
            text = src.read_text(encoding="utf-8")
            text, n = sanitize_text(text, manifest.get("sanitize", {}).get("replacements"))
            subs += n
            dst.write_text(text, encoding="utf-8")
        else:
            shutil.copy2(src, dst)  # Binaer byte-identisch
        copied += 1
    for pub_src, dest in (manifest.get("public_overlay") or {}).items():
        d = out_dir / dest
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(public_dir / pub_src, d)
        overlaid += 1
    ls = manifest.get("leak_scan", {})
    leaks = scan_for_leaks(out_dir, ls.get("forbidden"), ls.get("skip_exts"))
    return {"copied": copied, "excluded": excluded, "sanitized_subs": subs,
            "overlaid": overlaid, "leaks": leaks}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Baut den yuki-public-Export.")
    ap.add_argument("--out", required=True)
    ap.add_argument("--repo-root", default=str(_ROOT))
    ap.add_argument("--manifest", default=str(_ROOT / "public" / "export_manifest.jsonc"))
    ap.add_argument("--public-dir", default=str(_ROOT / "public"))
    a = ap.parse_args(argv)
    manifest = load_manifest(Path(a.manifest))
    src = list_tracked_files(Path(a.repo_root))
    rep = build_export(a.repo_root, a.out, manifest, a.public_dir, src)
    print(f"[export] kopiert={rep['copied']} ausgeschlossen={rep['excluded']} "
          f"ersetzt={rep['sanitized_subs']} overlay={rep['overlaid']}")
    if rep["leaks"]:
        print(f"[export] ABBRUCH: {len(rep['leaks'])} Leak(s):")
        for f, ln, pat in rep["leaks"][:50]:
            print(f"  {f}:{ln}  /{pat}/")
        return 2
    print(f"[export] OK -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
