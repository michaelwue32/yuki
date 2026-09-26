"""
Yuki - Film-Pipeline (Phase 3 "Tun", Bau 2)
===========================================
Reine Logik fuer die Film-Wiedergabe im Medien-Modal: ffprobe-Gate
(Passthrough vs. HLS-Transcode), Cache-Pfade, ffmpeg-Kommandos,
Progress-Parsing, LRU-Evict und der MediaJobManager. subprocess/probe
sind Injektionspunkte -> ohne echtes ffmpeg testbar. server.py haelt nur
duenne Endpoints, die hier reinrufen.
"""

_MEDIA_DEFAULTS = {
    "cache_dir": "",              # leer -> server.py setzt runtime/media_cache
    "cache_min_free_gb": 250,
    "profiles": {
        "mobile":  {"height": 720,  "v_bitrate": "2500k"},
        "desktop": {"height": 1080, "v_bitrate": "4000k"},
    },
    "nvenc_preset": "p4",
    "hls_time": 4,
    "ffprobe_timeout_s": 20,
    "resume_enabled": True,       # unfertigen Transcode fortsetzen statt neu (HLS-Append)
}


def media_config(archivar_cfg):
    """Loest den media-Block aus der (flach gemergten) archivar-Config auf und
    legt pro Subkey Defaults nach (die Datei kann media partiell liefern)."""
    raw = {}
    if isinstance(archivar_cfg, dict) and isinstance(archivar_cfg.get("media"), dict):
        raw = archivar_cfg["media"]
    out = dict(_MEDIA_DEFAULTS)
    for k, v in raw.items():
        if k == "profiles":
            continue
        if k in out:
            out[k] = v
    # Profile tief mergen: Default-Profile bleiben, gelieferte ueberschreiben.
    prof = {name: dict(p) for name, p in _MEDIA_DEFAULTS["profiles"].items()}
    for name, p in (raw.get("profiles") or {}).items():
        if isinstance(p, dict):
            prof.setdefault(name, {}).update(p)
    out["profiles"] = prof
    return out


def profile_height(cfg_media, profile):
    return int(cfg_media["profiles"][profile]["height"])


# ---------------------------------------------------------------------------
# Task 2: ffprobe-Kommando + Parse + Passthrough-vs-HLS-Gate
# ---------------------------------------------------------------------------
import json as _json
import pathlib

PASSTHROUGH_CONTAINERS = {"mp4", "m4v", "mov"}
_OK_VCODEC = {"h264"}
_OK_ACODEC = {"aac", "mp3", None, ""}


def input_url(archivar_cfg, fid):
    base = (archivar_cfg.get("url") or "").rstrip("/")
    return f"{base}/download/{int(fid)}"


def build_ffprobe_cmd(url):
    return [
        "ffprobe", "-v", "error",
        "-show_entries", "stream=codec_name,codec_type,height",
        "-show_entries", "format=duration",
        "-of", "json", url,
    ]


def parse_ffprobe_json(raw):
    out = {"vcodec": None, "acodec": None, "height": None, "duration": None}
    try:
        data = _json.loads(raw)
    except (ValueError, TypeError):
        return out
    for s in (data.get("streams") or []):
        t = s.get("codec_type")
        if t == "video" and out["vcodec"] is None:
            out["vcodec"] = s.get("codec_name")
            h = s.get("height")
            out["height"] = int(h) if isinstance(h, (int, float)) else None
        elif t == "audio" and out["acodec"] is None:
            out["acodec"] = s.get("codec_name")
    try:
        d = (data.get("format") or {}).get("duration")
        out["duration"] = float(d) if d is not None else None
    except (ValueError, TypeError):
        out["duration"] = None
    return out


def media_decide(probe, target_height, ext):
    """Passthrough nur wenn Container UND Codecs browserfaehig UND Hoehe <= Ziel.
    Sonst (inkl. unvollstaendigem Probe) konservativ HLS-Transcode."""
    if (ext or "").lower() not in PASSTHROUGH_CONTAINERS:
        return "hls"
    if probe.get("vcodec") not in _OK_VCODEC:
        return "hls"
    if probe.get("acodec") not in _OK_ACODEC:
        return "hls"
    h = probe.get("height")
    if h is None or h > target_height:
        return "hls"
    return "passthrough"


# ---------------------------------------------------------------------------
# Task 3: Cache-Pfade + ENDLIST-Vollstaendigkeitscheck (traversal-sicher)
# ---------------------------------------------------------------------------

VALID_PROFILES = {"mobile", "desktop"}
PLAYLIST_NAME = "playlist.m3u8"


def validate_profile(profile):
    if profile not in VALID_PROFILES:
        raise ValueError(f"ungueltiges Profil: {profile!r}")
    return profile


def cache_key(fid, profile):
    return f"{int(fid)}_{validate_profile(profile)}"


def cache_subdir(cache_dir, fid, profile):
    return pathlib.Path(cache_dir) / cache_key(fid, profile)


def playlist_path(cache_dir, fid, profile):
    return cache_subdir(cache_dir, fid, profile) / PLAYLIST_NAME


def is_cache_complete(cache_dir, fid, profile):
    pl = playlist_path(cache_dir, fid, profile)
    try:
        return pl.is_file() and "#EXT-X-ENDLIST" in pl.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False


import re as _re


def hls_resume_point(cache_dir, fid, profile):
    """Liest den Transcode-Fortschritt aus der playlist.m3u8: (Segmentzahl N,
    Resume-Offset T = Summe der EXTINF-Dauern). None, wenn keine Playlist, 0
    Segmente oder bereits komplett (ENDLIST) - dann gibt es nichts zu resumen.
    T liegt auf einer Keyframe-Grenze (HLS schneidet dort)."""
    try:
        pl = playlist_path(cache_dir, fid, profile)
        if not pl.is_file():
            return None
        text = pl.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    if "#EXT-X-ENDLIST" in text:
        return None
    total = 0.0
    n = 0
    for line in text.splitlines():
        line = line.strip()
        m = _re.match(r"#EXTINF:([0-9.]+)", line)
        if m:
            try:
                total += float(m.group(1))
            except ValueError:
                pass
        elif line.endswith(".ts"):
            n += 1
    if n < 1:
        return None
    return (n, total)


def _cleanup_stray_segments(out_dir, n_segments):
    """Loescht seg_*.ts mit Index >= n_segments (das beim Kill halb geschriebene
    Segment + evtl. Reste), damit -start_number sauber weiterschreibt. Gibt die
    geloeschten Dateinamen zurueck."""
    out_dir = pathlib.Path(out_dir)
    deleted = []
    for p in out_dir.glob("seg_*.ts"):
        m = _re.match(r"seg_(\d+)\.ts$", p.name)
        if m and int(m.group(1)) >= int(n_segments):
            try:
                p.unlink()
                deleted.append(p.name)
            except OSError:
                pass
    return deleted


# ---------------------------------------------------------------------------
# Task 4: ffmpeg-HLS-Kommando-Builder (CPU-Decode/Scale + h264_nvenc)
# ---------------------------------------------------------------------------

def build_ffmpeg_hls_cmd(url, out_dir, profile_cfg, preset, hls_time,
                         resume_seconds=None, start_number=0):
    """Gibt eine ffmpeg-argv-Liste fuer HLS-Transcode zurueck (kein shell=True).

    CPU-Decode (kein -hwaccel), CPU-Scale (scale=-2:<height>), NVENC-Encode,
    AAC-Audio, HLS-event-Playlist, -progress pipe:1 fuer %-Anzeige.

    resume_seconds gesetzt -> Fortsetzung: -ss (Input-Seek an die Keyframe-Grenze T),
    -output_ts_offset (Output-PTS ab T weiter -> keine Discontinuity), -start_number +
    -hls_flags append_list (an die bestehende Playlist anhaengen). resume_seconds None
    -> Fresh (byte-identisch zum bisherigen Verhalten).
    """
    out_dir = pathlib.Path(out_dir)
    height = int(profile_cfg["height"])
    v_bitrate = str(profile_cfg.get("v_bitrate", "4000k"))
    seg = str(out_dir / "seg_%05d.ts")
    playlist = str(out_dir / PLAYLIST_NAME)
    resuming = resume_seconds is not None
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-y"]
    if resuming:
        cmd += ["-ss", str(resume_seconds)]
    cmd += [
        "-i", url,
        "-vf", f"scale=-2:{height}",
        "-c:v", "h264_nvenc", "-preset", str(preset), "-b:v", v_bitrate,
        "-c:a", "aac", "-b:a", "160k", "-ac", "2",
    ]
    if resuming:
        cmd += ["-output_ts_offset", str(resume_seconds)]
    cmd += [
        "-progress", "pipe:1",
        "-f", "hls",
        "-hls_time", str(int(hls_time)),
        "-hls_playlist_type", "event",
    ]
    if resuming:
        cmd += ["-start_number", str(int(start_number)), "-hls_flags", "append_list"]
    cmd += ["-hls_segment_filename", seg, playlist]
    return cmd


# ---------------------------------------------------------------------------
# Task 5: ffmpeg-Progress-Parsing (pure)
# ---------------------------------------------------------------------------

def parse_progress_line(line):
    """Parst eine ffmpeg -progress-Zeile und gibt Mikrosekunden zurueck.

    Erkennt out_time_us= und out_time_ms= (beide sind bei ffmpeg Mikrosekunden).
    Gibt None zurueck fuer alle anderen Zeilen oder bei ungueltigem Wert.
    """
    line = (line or "").strip()
    for key in ("out_time_us=", "out_time_ms="):
        if line.startswith(key):
            val = line[len(key):].strip()
            try:
                return int(val)
            except ValueError:
                return None
    return None


def progress_pct(out_time_us, duration_s):
    """Berechnet Fortschritt in Prozent (0..99) aus Mikrosekunden und Dauer.

    Gedeckelt auf 99 bis ENDLIST das Ende markiert.
    Gibt 0 zurueck wenn keine Dauer bekannt oder division-by-zero.
    """
    if not duration_s or duration_s <= 0:
        return 0
    pct = int((out_time_us / 1_000_000.0) / duration_s * 100)
    return max(0, min(99, pct))


# ---------------------------------------------------------------------------
# Task 6: LRU-Evict (freiraum-basiert, injizierte disk_usage)
# ---------------------------------------------------------------------------
import shutil

_GIB = 1024 ** 3


def _rmtree(path):
    """Indirektion, damit Tests das Loeschen tracken/mocken koennen."""
    shutil.rmtree(path, ignore_errors=True)


def _entry_mtime(d):
    pl = d / PLAYLIST_NAME
    try:
        return pl.stat().st_mtime if pl.exists() else d.stat().st_mtime
    except OSError:
        return 0.0


def lru_evict(cache_dir, min_free_gb, disk_usage_fn, protect=frozenset()):
    """Loescht aelteste Cache-Ordner bis wieder >= min_free_gb frei (oder nichts
    mehr loeschbar). protect = Menge von Cache-Keys, die tabu sind."""
    cache_dir = pathlib.Path(cache_dir)
    if not cache_dir.is_dir():
        return []
    need = int(min_free_gb) * _GIB
    entries = sorted(
        [d for d in cache_dir.iterdir() if d.is_dir() and d.name not in protect],
        key=_entry_mtime,
    )
    deleted = []
    for d in entries:
        try:
            _total, _used, free = disk_usage_fn(str(cache_dir))
        except OSError:
            break
        if free >= need:
            break
        _rmtree(d)
        deleted.append(d.name)
    return deleted


# ---------------------------------------------------------------------------
# Task 7: MediaJobManager (ein aktiver Job, Progress-Thread, Cancel, LRU)
# ---------------------------------------------------------------------------
import threading


class _Job:
    __slots__ = ("key", "fid", "profile", "proc", "thread", "state", "pct",
                 "duration", "resume_offset_s")

    def __init__(self, key, fid, profile, duration):
        self.key = key
        self.fid = fid
        self.profile = profile
        self.proc = None
        self.thread = None
        self.state = "transcoding"
        self.pct = 0
        self.duration = duration
        self.resume_offset_s = 0.0    # >0 bei Resume: Offset fuer die %-Rechnung


class MediaJobManager:
    def __init__(self, archivar_cfg_fn, spawn, probe, now):
        self._cfg_fn = archivar_cfg_fn
        self._spawn = spawn
        self._probe = probe
        self._now = now
        self._lock = threading.Lock()
        self._active = None          # _Job | None
        self._done = {}              # key -> state ("ready"/"error")

    # --- oeffentlich ---------------------------------------------------------
    def _media_cfg(self):
        return media_config(self._cfg_fn())

    def _cache_dir(self):
        return self._media_cfg()["cache_dir"]

    @property
    def active_key(self):
        j = self._active
        return j.key if j else None

    def cache_status_map(self, pairs):
        cd = self._cache_dir()
        out = {}
        for fid, profile in pairs:
            try:
                key = cache_key(fid, profile)
            except ValueError:
                continue
            out[key] = "ready" if is_cache_complete(cd, fid, profile) else "absent"
        return out

    def prepare(self, fid, profile, ext):
        profile = validate_profile(profile)
        key = cache_key(fid, profile)
        cd = self._cache_dir()
        # 1) fertiger Cache?
        if is_cache_complete(cd, fid, profile):
            return {"state": "ready", "kind": "hls", "key": key}
        # 2) laeuft dieser Job schon?
        with self._lock:
            if self._active and self._active.key == key:
                return {"state": "transcoding", "kind": "hls", "key": key}
        # 3) probe + Entscheidung
        cfg = self._cfg_fn()
        mcfg = media_config(cfg)
        url = input_url(cfg, fid)
        probe = self._probe(url, mcfg["ffprobe_timeout_s"])
        target = profile_height(mcfg, profile)
        if media_decide(probe, target, ext) == "passthrough":
            return {"state": "passthrough", "kind": "passthrough", "key": key}
        # 4) HLS-Transcode starten (alten canceln)
        self._start_transcode(fid, profile, key, url, mcfg, probe.get("duration"))
        return {"state": "transcoding", "kind": "hls", "key": key}

    def status(self, fid, profile):
        try:
            key = cache_key(fid, profile)
        except ValueError:
            return {"state": "error", "pct": 0, "kind": "hls", "key": ""}
        if is_cache_complete(self._cache_dir(), fid, profile):
            return {"state": "ready", "pct": 100, "kind": "hls", "key": key}
        with self._lock:
            j = self._active
            if j and j.key == key:
                return {"state": j.state, "pct": j.pct, "kind": "hls", "key": key}
        st = self._done.get(key)
        if st:
            return {"state": st, "pct": 100 if st == "ready" else 0, "kind": "hls", "key": key}
        return {"state": "absent", "pct": 0, "kind": "hls", "key": key}

    def join_active(self, timeout=None):
        """Nur fuer Tests: wartet auf den Reader-Thread des aktiven Jobs."""
        j = self._active
        if j and j.thread:
            j.thread.join(timeout)

    # --- intern --------------------------------------------------------------
    def _start_transcode(self, fid, profile, key, url, mcfg, duration):
        with self._lock:
            if self._active and self._active.proc:
                try:
                    self._active.proc.terminate()
                except Exception:
                    pass
            out_dir = cache_subdir(mcfg["cache_dir"], fid, profile)
            resume = None
            if mcfg.get("resume_enabled", True):
                resume = hls_resume_point(mcfg["cache_dir"], fid, profile)
            if resume:
                n_seg, resume_s = resume
                _cleanup_stray_segments(out_dir, n_seg)   # halb geschriebenes Segment weg
                cmd = build_ffmpeg_hls_cmd(
                    url, out_dir, mcfg["profiles"][profile], mcfg["nvenc_preset"],
                    mcfg["hls_time"], resume_seconds=resume_s, start_number=n_seg)
                offset = float(resume_s)
            else:
                _rmtree(out_dir)                      # fresh: stale Teil-Cache weg
                out_dir.mkdir(parents=True, exist_ok=True)
                cmd = build_ffmpeg_hls_cmd(
                    url, out_dir, mcfg["profiles"][profile], mcfg["nvenc_preset"], mcfg["hls_time"])
                offset = 0.0
            job = _Job(key, fid, profile, duration)
            job.resume_offset_s = offset
            job.proc = self._spawn(cmd)
            job.thread = threading.Thread(target=self._reader, args=(job, mcfg), daemon=True)
            self._active = job
            job.thread.start()

    def _reader(self, job, mcfg):
        off_us = int((getattr(job, "resume_offset_s", 0.0) or 0.0) * 1_000_000)
        try:
            for raw in (job.proc.stdout or []):
                us = parse_progress_line(raw if isinstance(raw, str) else raw.decode("utf-8", "ignore"))
                if us is not None:
                    job.pct = progress_pct(us + off_us, job.duration)
        except Exception:
            pass
        # Job zu Ende (oder gecancelt)
        complete = is_cache_complete(mcfg["cache_dir"], job.fid, job.profile)
        with self._lock:
            if self._active is job:
                self._active = None
            self._done[job.key] = "ready" if complete else "error"
        if complete:
            try:
                lru_evict(mcfg["cache_dir"], mcfg["cache_min_free_gb"],
                          shutil.disk_usage, protect={job.key})
            except Exception:
                pass


def passthrough_headers(upstream_headers):
    """Wählt Header für einen Passthrough-<video>-Stream.

    Reicht Content-Type, Content-Length, Content-Range und Accept-Ranges durch;
    setzt Accept-Ranges: bytes falls fehlend; lässt Content-Disposition weg
    (Inline-Wiedergabe, kein Download).
    """
    up = upstream_headers or {}
    out = {}
    for h in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
        if h in up:
            out[h] = up[h]
    out.setdefault("Accept-Ranges", "bytes")
    return out
