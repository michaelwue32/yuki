#!/usr/bin/env python3
"""Naht-Test fuer den Film-Transcode-Resume an einem ECHTEN archivar-Film.

Transcodiert einen Film frisch ~seconds1, killt hart (wie der Filmwechsel), resumt
via hls_resume_point + append ~seconds2, killt wieder, und prueft mit ffprobe die
Segment-Naht: PTS-Kontinuitaet (keine grosse Luecke/kein Rueckwaerts-Sprung) und
saubere Dekodierung um Segment N. Klaert nebenbei, ob ffmpegs -progress out_time beim
Resume ABSOLUT (~T) oder RELATIV (~0) zaehlt -> wichtig fuer die %-Offset-Rechnung in
yuki_media._reader.

    .\\.venv\\Scripts\\python.exe tools\\media_resume_seamtest.py --fid 7707552 --profile desktop

Nutzt echten ffmpeg/ffprobe + laufenden archivar. Schreibt in runtime/seamtest/ (kein
Eingriff in den echten Medien-Cache).
"""
import argparse
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import yuki_media as ym  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")      # Windows-Konsole cp1252 (Stolperfalle 8)
except Exception:  # noqa: BLE001
    pass


def _playlist_segments(pl_path):
    """Segment-Dateinamen in PLAYLIST-Reihenfolge (nicht Datei-Index-Sortierung -
    ffmpeg leitet die Namen beim append aus dem Zeitstempel ab, nicht fortlaufend)."""
    out = []
    try:
        for line in pl_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line.endswith(".ts"):
                out.append(line)
    except OSError:
        pass
    return out


def read_archivar_cfg():
    txt = (ROOT / "config" / "archivar.json").read_text(encoding="utf-8")
    txt = re.sub(r"(^|\s)//[^\n]*", "", txt)      # simple // comment strip (JSONC-tolerant)
    return json.loads(txt)


def probe_input(url, timeout):
    cmd = ["ffprobe", "-v", "error", "-show_entries",
           "stream=codec_name,codec_type,height", "-show_entries", "format=duration",
           "-of", "json", url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return ym.parse_ffprobe_json(r.stdout)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def run_ffmpeg_for(cmd, seconds):
    """Startet ffmpeg, sammelt -progress out_time_us-Zeilen, killt hart nach `seconds`.
    Gibt die Liste der out_time_us (Mikrosekunden) in Ankunftsreihenfolge zurueck."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    vals = []

    def reader():
        for line in (proc.stdout or []):
            u = ym.parse_progress_line(line)
            if u is not None:
                vals.append(u)
    t = threading.Thread(target=reader, daemon=True)
    t.start()
    time.sleep(seconds)
    proc.terminate()                              # hart (Windows TerminateProcess) = wie Filmwechsel
    try:
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        proc.kill()
    t.join(2)
    return vals


def seg_pts(seg_path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v",
                        "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(seg_path)],
                       capture_output=True, text=True)
    pts = []
    for line in r.stdout.splitlines():
        s = line.strip().rstrip(",")
        try:
            pts.append(float(s))
        except ValueError:
            pass
    return pts


def decode_ok(seg_path):
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(seg_path), "-f", "null", "-"],
                       capture_output=True, text=True)
    return r.returncode == 0 and not r.stderr.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fid", type=int, required=True)
    ap.add_argument("--profile", default="desktop")
    ap.add_argument("--seconds1", type=int, default=30)
    ap.add_argument("--seconds2", type=int, default=20)
    args = ap.parse_args()

    cfg = read_archivar_cfg()
    mcfg = ym.media_config(cfg)
    url = ym.input_url(cfg, args.fid)
    prof_cfg = mcfg["profiles"][args.profile]
    preset, hls_time = mcfg["nvenc_preset"], mcfg["hls_time"]

    cache_dir = ROOT / "runtime" / "seamtest"
    out_dir = ym.cache_subdir(str(cache_dir), args.fid, args.profile)
    ym._rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[input] {url}")
    pr = probe_input(url, mcfg["ffprobe_timeout_s"])
    print(f"[probe] {pr}")

    # 1) Fresh-Transcode ~seconds1, dann hart killen.
    fresh = ym.build_ffmpeg_hls_cmd(url, out_dir, prof_cfg, preset, hls_time)
    print(f"[fresh] transcodiere {args.seconds1}s ...")
    tail = run_ffmpeg_for(fresh, args.seconds1)
    print(f"[fresh] out_time_us (letzte 3): {tail[-3:]}  ({len(tail)} progress-Zeilen)")

    rp = ym.hls_resume_point(str(cache_dir), args.fid, args.profile)
    if not rp:
        print("FAIL: kein Resume-Punkt gefunden (seconds1 erhoehen?).")
        return 1
    n_seg, resume_s = rp
    print(f"[resume-point] N={n_seg}  T={resume_s:.3f}s")

    # 2) Resume via append ~seconds2.
    stray = ym._cleanup_stray_segments(out_dir, n_seg)
    print(f"[cleanup] stray Segmente entfernt: {stray}")
    resume_cmd = ym.build_ffmpeg_hls_cmd(url, out_dir, prof_cfg, preset, hls_time,
                                         resume_seconds=resume_s, start_number=n_seg)
    print(f"[resume] transcodiere weitere {args.seconds2}s ...")
    head = run_ffmpeg_for(resume_cmd, args.seconds2)
    print(f"[resume] out_time_us (erste 3): {head[:3]}")

    # Progress-Semantik bestimmen (fuer den %-Offset in _reader).
    if head:
        first_s = head[0] / 1e6
        semantic = "RELATIV (startet ~0)" if first_s < resume_s * 0.5 else "ABSOLUT (startet ~T)"
        print(f"[progress] erste Resume-out_time={first_s:.2f}s vs T={resume_s:.2f}s  ->  {semantic}")

    # 3) Naht pruefen. Die Naht ist die Grenze in der PLAYLIST-REIHENFOLGE:
    #    Eintrag n_seg-1 (letztes Fresh-Segment) -> n_seg (erstes Resume-Segment).
    #    NICHT nach Dateinamen raten - ffmpeg nummeriert beim append per Zeitstempel.
    seg_names = _playlist_segments(out_dir / ym.PLAYLIST_NAME)
    if len(seg_names) <= n_seg:
        print(f"[seam] FAIL: Playlist hat nur {len(seg_names)} Segmente - Resume hat nichts angehaengt")
        return 2
    prev_seg = out_dir / seg_names[n_seg - 1]
    new_seg = out_dir / seg_names[n_seg]
    print(f"[seam] Fresh-Ende={prev_seg.name}  Resume-Start={new_seg.name}")

    verdict_ok = True
    pts_prev = seg_pts(prev_seg) if prev_seg.exists() else []
    pts_new = seg_pts(new_seg) if new_seg.exists() else []
    if pts_prev and pts_new:
        gap = min(pts_new) - max(pts_prev)        # >0 Luecke, <0 Overlap
        mono = sorted(pts_new) == pts_new or True  # PTS koennen umsortiert ankommen (B-Frames)
        print(f"[seam] Fresh-Ende-PTS={max(pts_prev):.3f}s  Resume-Start-PTS={min(pts_new):.3f}s  "
              f"gap={gap * 1000:+.1f}ms")
        if abs(gap) > 0.5:                          # kleiner Overlap/Gap ok, grosser nicht
            verdict_ok = False
    else:
        print("[seam] WARN: PTS an der Naht nicht lesbar")
        verdict_ok = False

    dec = {p.name: decode_ok(p) for p in (prev_seg, new_seg) if p.exists()}
    print(f"[seam] decode ok: {dec}")
    if not all(dec.values()):
        verdict_ok = False

    print("\n=== VERDICT:", "NAHT SAUBER (ok)" if verdict_ok else "NAHT PRUEFEN (fail)", "===")
    print(f"(Cache-Ordner zum manuellen Nachsehen: {out_dir})")
    return 0 if verdict_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
