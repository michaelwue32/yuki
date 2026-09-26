#!/usr/bin/env bash
# =============================================================================
# backup_yuki.sh - Yuki-Backup als .tar.gz  (Linux-Pendant zu backup_yuki.ps1)
# =============================================================================
# Packt die unverzichtbaren Teile von Yuki in ein .tar.gz mit Zeitstempel.
# Was rein/raus gehoert: docs/backup.md ist die Quelle der Wahrheit.
#
# Aufruf:
#   ./tools/backup_yuki.sh                        # Standard (~Yuki-State + Code)
#   ./tools/backup_yuki.sh --include-wadoku       # + data/wadoku.sqlite (+100 MB)
#   ./tools/backup_yuki.sh --include-f5-model     # + schweres F5-Modell (.pt, +1.3 GB)
#   ./tools/backup_yuki.sh --include-avatar-source# + avatar/source + animations_source
#   ./tools/backup_yuki.sh --all                  # alle Includes oben drauf
#   ./tools/backup_yuki.sh --output-dir /pfad     # eigenes Ziel
#   ./tools/backup_yuki.sh --keep 14              # taegliche: die 14 neuesten behalten
#   ./tools/backup_yuki.sh --keep 14 --keep-monthly 12 --keep-yearly 3
#                                                 # GFS: 14 taeglich + 12 monatlich + 3 jaehrlich
#   ./tools/backup_yuki.sh --dry-run              # nur listen (zeigt auch was Rotation loeschen wuerde)
#
# Rotation (Grandfather-Father-Son): Union aus drei Keep-Mengen -
#   --keep N          die N neuesten Backups (taeglich)
#   --keep-monthly M  pro Kalendermonat das NEUESTE, fuer die letzten M Monate
#   --keep-yearly Y   pro Kalenderjahr das NEUESTE, fuer die letzten Y Jahre
#   Alles was in keiner Menge liegt, wird geloescht. Default aller drei = 0 =
#   nichts loeschen (konservativ; die Policy sitzt im Cron, nicht im Bare-Run).
#
# Standard-Ablageort:  ../yuki_backup/   (EINE Ebene ueber dem Repo, damit alte
#   Backups beim naechsten Lauf nicht versehentlich im neuen Archiv landen)
#
# Was IMMER rein geht (Defaults):
#   - Code (*.py *.ps1 *.sh *.md *.toml *.lock *.json *.jsonc *.txt) im Repo-Root
#   - config/     (alles - inkl. Klartext-Credentials! Archiv privat halten)
#   - memory/     (Yukis Gedaechtnis; SQLite via konsistentem Online-Backup-Snapshot)
#   - archive/    (Session-Volltranskripte + Facts-Backups)
#   - keepsakes/  (Bild-Album)
#   - drawings/       (Yukis Doodles - im PS-Script FEHLT das! hier bewusst drin)
#   - gedankenbilder/ (KI-Traumbilder  - im PS-Script FEHLT das! hier bewusst drin)
#   - voices/     (TTS-Referenzen; nur schweres F5-Modell unter f5tts/ ist gated)
#   - avatar/     (*.vrm, *.vroid, animations/, backgrounds/ - kein source/)
#   - data/*.tar.xz  (kleiner Original-Wadoku-XML-Dump)
#   - tools/ tests/ web/ docs/ certs/
#
# Was NIE rein geht:
#   - .venv/ __pycache__/ *.pyc          (via requirements.lock reproduzierbar)
#   - runtime/ tests/outputs/            (Debug-/Test-Artefakte)
#   - data/wadoku.sqlite                 (ausser --include-wadoku; via import_wadoku.py)
#   - data/kanjidic2.* data/kanjivg/ data/draw_symbols/ data/wadoku-xml-*/
#       -> alle reproduzierbar via docs/setup-*.md, bewusst DRAUSSEN (backup.md-
#          Philosophie: gross + re-erzeugbar bleibt raus). Weicht vom PS-Script ab,
#          das kanjivg/ + kanjidic2.sqlite versehentlich mitschaufelt.
#   - voices/f5tts/*.pt|.bin|.safetensors|... (ausser --include-f5-model)
#   - avatar/source/ avatar/animations_source/ (ausser --include-avatar-source)
# =============================================================================
set -euo pipefail

# ---------------------------------------------------------------------------
# Argumente
# ---------------------------------------------------------------------------
INCLUDE_F5=false
INCLUDE_WADOKU=false
INCLUDE_AVATAR_SRC=false
DRY_RUN=false
KEEP=0            # taegliche Backups (die N neuesten)
KEEP_MONTHLY=0   # pro Monat das neueste, fuer M Monate
KEEP_YEARLY=0    # pro Jahr das neueste, fuer Y Jahre
OUTPUT_DIR=""

while [ $# -gt 0 ]; do
  case "$1" in
    --include-f5-model)      INCLUDE_F5=true ;;
    --include-wadoku)        INCLUDE_WADOKU=true ;;
    --include-avatar-source) INCLUDE_AVATAR_SRC=true ;;
    --all)                   INCLUDE_F5=true; INCLUDE_WADOKU=true; INCLUDE_AVATAR_SRC=true ;;
    --dry-run)               DRY_RUN=true ;;
    --keep)                  KEEP="${2:?--keep braucht eine Zahl}"; shift ;;
    --keep-monthly)          KEEP_MONTHLY="${2:?--keep-monthly braucht eine Zahl}"; shift ;;
    --keep-yearly)           KEEP_YEARLY="${2:?--keep-yearly braucht eine Zahl}"; shift ;;
    --output-dir)            OUTPUT_DIR="${2:?--output-dir braucht einen Pfad}"; shift ;;
    -h|--help)               sed -n '2,58p' "$0"; exit 0 ;;
    *) echo "Unbekannte Option: $1" >&2; exit 2 ;;
  esac
  shift
done

# Retentions-Zahlen muessen nicht-negative Integer sein (sonst crasht das Gate
# NACH dem Packen - lieber frueh und sauber abbrechen).
for _v in KEEP KEEP_MONTHLY KEEP_YEARLY; do
  [[ "${!_v}" =~ ^[0-9]+$ ]] || { echo "Fehler: $_v muss eine nicht-negative Zahl sein (ist: ${!_v})" >&2; exit 2; }
done

# ---------------------------------------------------------------------------
# Pfade
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_NAME="$(basename "$REPO")"

if [ -z "$OUTPUT_DIR" ]; then
  OUTPUT_DIR="$(cd "$REPO/.." && pwd)/yuki_backup"
fi
mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"

STAMP="$(date +%Y-%m-%d_%H%M%S)"
ARCHIVE="$OUTPUT_DIR/yuki_${STAMP}.tar.gz"

# Snapshot-/Arbeits-Tempdir (immer aufraeumen)
WORK="$(mktemp -d "${TMPDIR:-/tmp}/yuki_backup_${STAMP}.XXXXXX")"
SNAP="$WORK/snap"          # SQLite-Snapshots, gespiegelt unter Original-Relpfad
MANIF="$WORK/manifest"
mkdir -p "$SNAP" "$MANIF"
trap 'rm -rf "$WORK"' EXIT

# Python fuer den SQLite-Snapshot: bevorzugt Repo-venv, sonst system-python3
PY="$REPO/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3 || true)"

# ---------------------------------------------------------------------------
# GFS-Rotation (Grandfather-Father-Son)
# ---------------------------------------------------------------------------
# Behaelt die neuesten $kd taeglichen + pro Monat das neueste (bis $km Monate)
# + pro Jahr das neueste (bis $ky Jahre). Das Datum kommt aus dem DATEINAMEN
# (yuki_YYYY-MM-DD_HHMMSS.tar.gz), der dank Zero-Padding lexikalisch =
# chronologisch sortiert - keine mtime, keine Metadaten noetig. Union der drei
# Keep-Mengen; ein Backup kann mehrere Rollen erfuellen (neuestes = daily UND
# Monats- UND Jahres-Anker) -> wird nur einmal behalten. Arg5 "true" = Dry:
# nur anzeigen, nichts loeschen. Fremd benannte Dateien werden nie angefasst.
prune_backups() {
  local dir="$1" kd="$2" km="$3" ky="$4" dry="${5:-false}"
  [ "$kd" -eq 0 ] && [ "$km" -eq 0 ] && [ "$ky" -eq 0 ] && return 0

  local all=() f b rest ym y
  while IFS= read -r f; do all+=("$f"); done < <(ls -1 "$dir"/yuki_*.tar.gz 2>/dev/null | sort -r)
  [ "${#all[@]}" -eq 0 ] && return 0

  local -A keep=() seen_m=() seen_y=()
  local n=0 mc=0 yc=0
  for f in "${all[@]}"; do
    b="$(basename "$f")"
    if [[ ! "$b" =~ ^yuki_[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{6}\.tar\.gz$ ]]; then
      keep["$f"]=1; continue                       # fremder Name -> sicher behalten
    fi
    rest="${b#yuki_}"; ym="${rest:0:7}"; y="${rest:0:4}"
    [ "$n" -lt "$kd" ] && { keep["$f"]=1; n=$((n+1)); }
    if [ "$km" -gt 0 ] && [ -z "${seen_m[$ym]:-}" ]; then
      seen_m[$ym]=1; [ "$mc" -lt "$km" ] && { keep["$f"]=1; mc=$((mc+1)); }
    fi
    if [ "$ky" -gt 0 ] && [ -z "${seen_y[$y]:-}" ]; then
      seen_y[$y]=1; [ "$yc" -lt "$ky" ] && { keep["$f"]=1; yc=$((yc+1)); }
    fi
  done

  local del=()
  for f in "${all[@]}"; do [ -z "${keep[$f]:-}" ] && del+=("$f"); done

  if [ "${#del[@]}" -eq 0 ]; then
    echo "Rotation (GFS d=$kd m=$km y=$ky): behalte alle ${#all[@]}, nichts zu loeschen."
  elif [ "$dry" = true ]; then
    echo "Rotation (GFS d=$kd m=$km y=$ky): wuerde ${#del[@]}/${#all[@]} loeschen:"
    printf '  - %s\n' "${del[@]##*/}"
  else
    printf '%s\0' "${del[@]}" | xargs -0 -r rm -f
    echo "Rotation (GFS d=$kd m=$km y=$ky): behalte $(( ${#all[@]} - ${#del[@]} ))/${#all[@]}, ${#del[@]} geloescht."
  fi
}

echo ""
echo "Repo-Root  : $REPO"
echo "Output-Dir : $OUTPUT_DIR"
echo "Ziel       : $ARCHIVE"
echo "Flags      : F5-Modell=$INCLUDE_F5 | Wadoku=$INCLUDE_WADOKU | Avatar-Source=$INCLUDE_AVATAR_SRC | keep=$KEEP monthly=$KEEP_MONTHLY yearly=$KEEP_YEARLY"
echo ""

# ---------------------------------------------------------------------------
# Datei-Liste sammeln (relativ zum Repo-Root)
# ---------------------------------------------------------------------------
cd "$REPO"

{
  # Top-Level-Code nach Extension (nur Dateien direkt im Root)
  find . -maxdepth 1 -type f \( \
      -name '*.py' -o -name '*.ps1' -o -name '*.sh' -o -name '*.md' \
      -o -name '*.toml' -o -name '*.lock' -o -name '*.json' \
      -o -name '*.jsonc' -o -name '*.txt' \)

  # Verzeichnisse, die KOMPLETT rein gehen
  for d in config memory archive keepsakes drawings gedankenbilder tools tests web docs certs; do
    [ -d "$d" ] && find "$d" -type f
  done

  # voices/ - alles, ausser dem schweren F5-Modell (gated)
  if [ -d voices ]; then
    if $INCLUDE_F5; then
      find voices -type f
    else
      find voices -type f -not \( -path 'voices/f5tts/*' -a \( \
          -name '*.pt' -o -name '*.bin' -o -name '*.safetensors' \
          -o -name '*.gguf' -o -name '*.ckpt' -o -name '*.pth' \) \)
    fi
  fi

  # avatar/ - ohne source/ + animations_source/ (gated)
  if [ -d avatar ]; then
    if $INCLUDE_AVATAR_SRC; then
      find avatar -type f
    else
      find avatar -type f -not -path 'avatar/source/*' -not -path 'avatar/animations_source/*'
    fi
  fi

  # data/ - nur der kleine Original-XML-Dump (Rest reproduzierbar, bewusst raus)
  [ -d data ] && find data -maxdepth 1 -type f -name '*.tar.xz'
} | sed 's,^\./,,' | LC_ALL=C sort -u > "$WORK/all.list"

# Junk + SQLite (Haupt-DB UND Sidecars) rausfiltern - SQLite kommt ueber den
# konsistenten Snapshot-Pfad rein, nicht als Rohkopie.
grep -vE '(^|/)__pycache__/' "$WORK/all.list" \
  | grep -vE '\.pyc$' \
  | grep -vE '^tests/outputs/' \
  | grep -vE '^runtime/' \
  | grep -vE '\.(sqlite|db)(-wal|-shm|-journal)?$' \
  > "$WORK/files.list" || true

# ---------------------------------------------------------------------------
# SQLite-Snapshots (konsistent, waehrend der Server laeuft)
# ---------------------------------------------------------------------------
# memory/*.sqlite immer; data/wadoku.sqlite nur mit --include-wadoku. Die
# Online-Backup-API (sqlite3.Connection.backup) kopiert page-fuer-page mit
# Reader-Lock -> konsistenter Snapshot trotz offener Server-Connection.
# WAL/SHM-Sidecars werden dabei automatisch reingemerged.
: > "$WORK/sqlite.list"
SQLITE_SRCS=()
while IFS= read -r f; do SQLITE_SRCS+=("$f"); done < <(find memory -maxdepth 1 -type f -name '*.sqlite' | sed 's,^\./,,' | LC_ALL=C sort)
$INCLUDE_WADOKU && [ -f data/wadoku.sqlite ] && SQLITE_SRCS+=("data/wadoku.sqlite")

snap_ok=0; snap_fail=0
if [ "${#SQLITE_SRCS[@]}" -gt 0 ]; then
  if [ -z "$PY" ]; then
    echo "WARN: kein python3 gefunden - SQLite-Files werden roh kopiert (evtl. inkonsistent, wenn Server laeuft)." >&2
    for src in "${SQLITE_SRCS[@]}"; do
      mkdir -p "$SNAP/$(dirname "$src")"
      cp -p "$src" "$SNAP/$src" && { echo "$src" >> "$WORK/sqlite.list"; snap_ok=$((snap_ok+1)); } || snap_fail=$((snap_fail+1))
    done
  else
    for src in "${SQLITE_SRCS[@]}"; do
      mkdir -p "$SNAP/$(dirname "$src")"
      if "$PY" - "$src" "$SNAP/$src" <<'PY'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
try:
    with dst:
        src.backup(dst)
finally:
    src.close(); dst.close()
PY
      then
        echo "$src" >> "$WORK/sqlite.list"; snap_ok=$((snap_ok+1))
      else
        echo "WARN: SQLite-Snapshot fehlgeschlagen: $src (Rohkopie als Fallback)" >&2
        cp -p "$src" "$SNAP/$src" && { echo "$src" >> "$WORK/sqlite.list"; snap_ok=$((snap_ok+1)); } || snap_fail=$((snap_fail+1))
      fi
    done
  fi
fi
echo "SQLite-Snapshots: $snap_ok ok, $snap_fail fehlgeschlagen"

# ---------------------------------------------------------------------------
# Statistik
# ---------------------------------------------------------------------------
FILE_COUNT="$(wc -l < "$WORK/files.list" | tr -d ' ')"
SQL_COUNT="$(wc -l < "$WORK/sqlite.list" | tr -d ' ')"
# LC_ALL=C: sonst schreibt du auf de_DE-Systemen "insgesamt" statt "total" und
# das Summen-awk greift nicht (+ awk %f wuerde Komma statt Punkt setzen).
RAW_KB="$( { tr '\n' '\0' < "$WORK/files.list" | LC_ALL=C du -ck --files0-from=- 2>/dev/null; \
             [ -s "$WORK/sqlite.list" ] && tr '\n' '\0' < "$WORK/sqlite.list" | (cd "$SNAP" && LC_ALL=C du -ck --files0-from=- 2>/dev/null); } \
           | LC_ALL=C awk '$2=="total"{s+=$1} END{print s+0}')"
RAW_MB="$(LC_ALL=C awk "BEGIN{printf \"%.1f\", ${RAW_KB:-0}/1024}")"

echo "Dateien zu packen : $FILE_COUNT + $SQL_COUNT SQLite-Snapshots  (~${RAW_MB} MB roh)"

# Credential-Warnung
for sec in config/yuki_calendar.json config/yuki_homeassistant.json config/cameras.json config/ntfy.json; do
  if [ -f "$sec" ]; then
    echo "Hinweis: config/ enthaelt Klartext-Credentials ($sec, ...). Archiv privat halten."
    break
  fi
done
echo ""

# ---------------------------------------------------------------------------
# Dry-Run: nur zeigen
# ---------------------------------------------------------------------------
if $DRY_RUN; then
  echo "[Dry-Run] kein Archiv geschrieben. Erste 20 Pfade:"
  head -n 20 "$WORK/files.list" | sed 's,^,  ,'
  [ "$FILE_COUNT" -gt 20 ] && echo "  ... +$((FILE_COUNT - 20)) weitere"
  [ "$SQL_COUNT" -gt 0 ] && { echo "  SQLite-Snapshots:"; sed 's,^,    ,' "$WORK/sqlite.list"; }
  echo ""
  prune_backups "$OUTPUT_DIR" "$KEEP" "$KEEP_MONTHLY" "$KEEP_YEARLY" true
  exit 0
fi

# ---------------------------------------------------------------------------
# Manifest (landet als _BACKUP_MANIFEST.txt oben im Archiv)
# ---------------------------------------------------------------------------
cat > "$MANIF/_BACKUP_MANIFEST.txt" <<EOF
Yuki Backup Manifest
====================
Erstellt    : $(date '+%Y-%m-%d %H:%M:%S')
Maschine    : $(hostname) ($(whoami))
Repo-Pfad   : $REPO
Optionen    : F5-Modell=$INCLUDE_F5 | Wadoku=$INCLUDE_WADOKU | Avatar-Source=$INCLUDE_AVATAR_SRC
Dateien     : $FILE_COUNT + $SQL_COUNT SQLite-Snapshots (~${RAW_MB} MB roh)

Wiederherstellen
----------------
1) Archiv entpacken:  tar -xzf yuki_${STAMP}.tar.gz -C <ziel>
   -> entpackt in einen Ordner '${REPO_NAME}/'
2) docs/backup.md folgen (Disaster-Recovery-Reihenfolge)
3) venv:  python3 -m venv .venv && .venv/bin/pip install -r requirements.lock
4) Reproduzierbare data/-Assets (wadoku/kanji/draw_symbols) via docs/setup-*.md neu bauen
EOF

# ---------------------------------------------------------------------------
# Packen
# ---------------------------------------------------------------------------
# Alles landet unter einem '${REPO_NAME}/'-Praefix (--transform), damit sich das
# Archiv sauber in einen eigenen Ordner entpackt. SQLite-Snapshots liegen physisch
# unter $SNAP, sollen aber unter ihrem Original-Relpfad ins Archiv -> eigenes -C.
TAR_ARGS=( -C "$REPO" -T "$WORK/files.list" )
[ "$SQL_COUNT" -gt 0 ] && TAR_ARGS+=( -C "$SNAP" -T "$WORK/sqlite.list" )
TAR_ARGS+=( -C "$MANIF" _BACKUP_MANIFEST.txt )

START=$(date +%s)
tar -czf "$ARCHIVE" \
    --owner=0 --group=0 --numeric-owner \
    --transform="s,^,${REPO_NAME}/," \
    "${TAR_ARGS[@]}"
DUR=$(( $(date +%s) - START ))

ZIP_MB="$(du -m "$ARCHIVE" | cut -f1)"
echo "Fertig!"
echo "Archiv : $ARCHIVE"
echo "Groesse: ${ZIP_MB} MB"
echo "Dauer  : ${DUR}s"

# ---------------------------------------------------------------------------
# Rotation (GFS): taeglich $KEEP + monatlich $KEEP_MONTHLY + jaehrlich $KEEP_YEARLY
# (alle 0 = nichts loeschen)
# ---------------------------------------------------------------------------
prune_backups "$OUTPUT_DIR" "$KEEP" "$KEEP_MONTHLY" "$KEEP_YEARLY" false
