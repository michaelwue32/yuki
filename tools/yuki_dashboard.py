r"""
yuki_dashboard.py - Textual-TUI als reicherer Ersatz fuer start_yuki.ps1.

Startet die acht Yuki-Services (Ollama, Vision, Qwen3-TTS, SearXNG, Yuki,
Yuki-Stimme/Wyoming-TTS, Yuki-STT/Wyoming-Whisper, ComfyUI) in einer einzigen Konsole,
faengt deren stdout in eigenen Log-Tabs ein,
zeigt System-Stats (CPU/RAM/GPU/VRAM) + Yuki-Activity (Turns/Persona) live,
und animiert dazu eine kleine ASCII-Yuki je nach Last (sitzt/liest/laeuft/rennt).

Aufruf:
    .\.venv\Scripts\python.exe tools\yuki_dashboard.py

Hotkeys:
    q           Quit (alle Services sauber stoppen)
    r           Restart Yuki (NUR server.py, andere bleiben oben)
    c           Aktuellen Log-Tab leeren
    1..9        Direkt-Switch zu Service-Tab

Sonderfaelle:
- Ollama ist Tray-App: kein stdout, nur Health-Check + ggf. Tray-Start.
- SearXNG ist Docker-in-WSL2: 'docker compose up -d' startet, 'docker logs -f'
  liefert den Stream. Stop NICHT beim Quit (restart:unless-stopped + WSL).
- Yuki-Stimme (Wyoming-TTS, :10200) + Yuki-STT (Wyoming-Whisper, :10300): Popen wie
  die anderen, aber Wyoming-Protokoll statt HTTP -> Health via tcp://-Connect-Test
  (_tcp_ok). Starten nach dem Yuki-Server (callen dessen /tts bzw. /stt).
  Killbar/restartbar wie Qwen3-TTS; beim Quit mit gestoppt. Beide nur fuer HA-Voice noetig.
- Vision/Qwen3-TTS/Yuki: subprocess.Popen mit stdout=PIPE. PYTHONUNBUFFERED=1
  + python -u sorgt fuer line-buffering, sonst landet erst beim Crash was im Tab.
"""

import os
import re
import ssl
import sys
import json
import time
import signal
import socket
import asyncio
import textwrap
import threading
import subprocess
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import psutil
from rich.text import Text

# windows-toasts ist optional - das Dashboard laeuft auch ohne. Wenn nicht
# installiert: Toast-Funktion ist no-op (siehe _send_toast). Wir lazy-konstruieren
# den WindowsToaster erst beim ersten echten Send, damit der Modulimport selbst
# nicht teuer wird.
try:
    from windows_toasts import Toast as _WinToast, WindowsToaster as _WinToaster  # type: ignore
    _TOAST_AVAILABLE = True
except Exception:
    _TOAST_AVAILABLE = False

_toaster_instance = None

# Half-Block-Avatar: optionaler Modus, der statt der ASCII-Katze ein RGB-Bild
# (persona-passender Background) als Unicode-Halfblock pixelt. Pillow ist optional;
# render_halfblock liegt in tools/preview_halfblock.py (Single-Source-of-Truth,
# wird auch vom Standalone-Preview-Skript genutzt).
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from PIL import Image as _PILImage  # type: ignore
    from preview_halfblock import render_halfblock as _render_halfblock_ansi  # type: ignore
    # Sprite-Pipeline ist optional: wenn keine Sprites unter avatar/sprites/
    # liegen, faellt der Halfblock-Modus auf Background-only zurueck.
    from sprite_compose import render_sprite_halfblock as _render_sprite_halfblock  # type: ignore
    _HALFBLOCK_AVAILABLE = True
except Exception:
    _HALFBLOCK_AVAILABLE = False
    _render_sprite_halfblock = None  # type: ignore

# Persistierung des Avatar-Style-Toggles (Hotkey 'a'). Bewusst eigenes File,
# nicht config/settings.jsonc (das ist statisch-deklarativ, hier brauchen wir
# Live-Schreiben). memory/ existiert immer, deshalb landet's da neben den
# anderen yuki_*.json-Files.
_DASHBOARD_SETTINGS_PATH = Path(__file__).resolve().parent.parent / "memory" / "yuki_dashboard.json"


def _load_dashboard_settings() -> dict:
    try:
        with open(_DASHBOARD_SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_dashboard_settings(data: dict) -> None:
    try:
        _DASHBOARD_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_DASHBOARD_SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def _get_persona_background(persona: str) -> Optional[Path]:
    """avatar/backgrounds/<persona>.<ext> falls vorhanden, sonst None. Probiert die
    Endungen in derselben Reihenfolge wie der Web-Loader (jpg zuerst - die Backgrounds
    sind seit 2026-06-20 JPEG statt PNG). Auch interne '_'-Personas haben kein BG."""
    if not persona or persona in ("?", ""):
        return None
    base = Path(__file__).resolve().parent.parent / "avatar" / "backgrounds"
    for ext in ("jpg", "jpeg", "png", "webp"):
        p = base / f"{persona}.{ext}"
        if p.is_file():
            return p
    return None


def _send_toast(title: str, body: str) -> None:
    """Fire-and-forget Windows-Toast. Schluckt jeden Fehler still - ein
    schiefgegangener Toast soll NIE das Dashboard runterreissen."""
    global _toaster_instance
    if not _TOAST_AVAILABLE:
        return
    try:
        if _toaster_instance is None:
            _toaster_instance = _WinToaster("Yuki Dashboard")
        _toaster_instance.show_toast(_WinToast([title, body]))
    except Exception:
        pass
from rich.console import RenderableType
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Footer, Header, Label, RichLog, Sparkline, Static, TabbedContent, TabPane

PROJ = Path(__file__).resolve().parent.parent  # D:\Projects\yuki
MEMORY_DIR = PROJ / "memory"

# imagegen liegt im Projekt-Root (nicht tools/) - fuer den ComfyUI-Multi-Server-
# Health-Check (is_comfyui_reachable prueft ALLE konfigurierten Server). Defensiv:
# fehlt/kaputt -> _imagegen=None, comfy faellt auf den einzelnen health_url zurueck.
sys.path.insert(0, str(PROJ))
try:
    import imagegen as _imagegen
except Exception:
    _imagegen = None

# Self-signed Yuki-Cert: HTTPS-Check ignoriert Verifizierung.
_SSL_NOVERIFY = ssl.create_default_context()
_SSL_NOVERIFY.check_hostname = False
_SSL_NOVERIFY.verify_mode = ssl.CERT_NONE


# ---------------------------------------------------------------------------
# Service-Konfigurationen
# ---------------------------------------------------------------------------

@dataclass
class ServiceConfig:
    key: str
    name: str
    port: int
    health_url: str
    cmd: list
    cwd: Optional[str] = None
    env: dict = field(default_factory=dict)
    start_timeout: int = 60
    is_tray: bool = False       # Ollama-Sonderfall: kein PIPE, nur Health-Check
    # display_only: rein anzeigender Remote-Dienst (ComfyUI auf der 4070). Das
    # Dashboard besitzt keinen Prozess -> NIE start-/beendbar, nur Health-Anzeige
    # (LED gruen=erreichbar / rot=aus). Strenger als is_tray: Ollama-Down ist noch
    # klickbar (Tray-Neustart), display_only bleibt immer stumm.
    display_only: bool = False
    # health_fn: optionaler eigener Health-Check statt HTTP/TCP auf health_url.
    # Fuer ComfyUI-Multi-Server: imagegen.is_comfyui_reachable (prueft ALLE
    # konfigurierten Server). None -> normaler http_ok(health_url)-Pfad.
    health_fn: Optional[Callable[[], bool]] = None
    # Regex-Strings; Log-Zeilen die matchen werden im Tab + All-Aggregator
    # unterdrueckt (z.B. der 15s-Health-Ping, der als Access-Log-Zeile spammt).
    suppress_patterns: list = field(default_factory=list)


def load_services() -> list:
    return [
        ServiceConfig(
            key="ollama", name="Ollama", port=11434,
            health_url="http://127.0.0.1:11434/",
            cmd=[os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama\ollama app.exe")],
            is_tray=True,
            start_timeout=30,
        ),
        ServiceConfig(
            key="vision", name="Vision (LFM2.5-VL)", port=8081,
            health_url="http://127.0.0.1:8081/health",
            cmd=[r"D:\Server\llama.cpp\llama-server.exe",
                 "-m", r"D:\Server\llama.cpp\models\lfm2-vl-1.6b.gguf",
                 "--mmproj", r"D:\Server\llama.cpp\models\mmproj.gguf",
                 "--host", "0.0.0.0", "--port", "8081",
                 "-ngl", "99", "-c", "4096"],
            start_timeout=60,
        ),
        # Qwen3-TTS (faster-qwen3-tts) - EINZIGE TTS-Engine fuer DE+EN+JA. SoVITS + F5
        # sind mit dem Cutover (2026-09-06) komplett raus (Dashboard, Code, Config).
        # Eigene venv D:\Server\qwen3-tts\venv, CUDAGraph-Warmup -> hoher timeout.
        ServiceConfig(
            key="qwen", name="Qwen3-TTS", port=5006,
            health_url="http://127.0.0.1:5006/health",
            cmd=[r"D:\Server\qwen3-tts\venv\Scripts\python.exe", "-u", str(PROJ / "qwen_server.py")],
            env={"HF_HOME": r"D:\Server\qwen3-tts\hf-cache", "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
            start_timeout=120,
        ),
        # Whisper-STT-Microservice (stt_server.py). Seit dem Core-Umzug (2026-09-10)
        # laeuft der Yuki-Server auf dem Linux-Notebook .103; dessen Max-Q-GPU ist
        # firmware auf 10W gedeckelt (boostet nie) -> STT ausgelagert auf DIESE 3060.
        # Der Core postet Audio an :5007. WICHTIG: stt.remote_url in DIESER
        # settings.jsonc MUSS leer sein, sonst riefe stt_server sich selbst auf.
        ServiceConfig(
            key="stt", name="Whisper-STT (:5007)", port=5007,
            health_url="http://127.0.0.1:5007/health",
            cmd=[str(PROJ / ".venv" / "Scripts" / "python.exe"), "-u",
                 str(PROJ / "stt_server.py"), "--host", "0.0.0.0", "--port", "5007"],
            cwd=str(PROJ),
            env={"PYTHONUNBUFFERED": "1"},
            start_timeout=90,
        ),
        # Capture-Frame-Dienst (capture_server.py :5008): liefert lokale dshow-Quellen
        # (HDMI-Capture 'gamecap') als JPEG ueber HTTP. Der Core .103 (Linux) kann das
        # dshow-Geraet nicht selbst grabben -> holt die Frames als http_snap von hier.
        ServiceConfig(
            key="capture", name="Capture-Frame (:5008)", port=5008,
            health_url="http://127.0.0.1:5008/health",
            cmd=[str(PROJ / ".venv" / "Scripts" / "python.exe"), "-u",
                 str(PROJ / "capture_server.py"), "--host", "0.0.0.0", "--port", "5008"],
            cwd=str(PROJ),
            env={"PYTHONUNBUFFERED": "1"},
            start_timeout=30,
        ),
        # SearXNG ist mit dem Umzug als Docker-Container auf den Core .103 gewandert
        # (nicht mehr WSL hier) -> nur noch Status-Anzeige, kein Start von dieser Box.
        ServiceConfig(
            key="searx", name="SearXNG (.103)", port=8888,
            health_url="http://127.0.0.1:8888/healthz",
            display_only=True,
            # 'compose up -d' (returnt sofort) + 'exec docker logs -f' als
            # langlebige Foreground-Pipe (gleichzeitig der Log-Stream im Tab).
            # Der eigentliche WSL-Keep-Alive ('sleep infinity') wird von
            # start_yuki.ps1 EINMAL beim Start als hidden background-Prozess
            # in die Distro injiziert - getrennt vom Dashboard-Lifecycle, damit
            # ein SearXNG-Restart-Klick nicht jedes Mal einen neuen orphaned
            # Sleep ansammelt. Siehe github.com/microsoft/WSL Issue #13291 fuer
            # den Hintergrund (vmIdleTimeout=-1 ist in WSL 2.5.7+ unzuverlaessig).
            cmd=["wsl", "-d", "Ubuntu", "--", "bash", "-c",
                 "cd ~/searxng && docker compose up -d && "
                 "exec docker logs -f --tail 20 searxng"],
            start_timeout=30,
        ),
        # Yuki-Core laeuft seit dem Umzug (2026-09-10) via systemd auf dem Linux-
        # Notebook .103 (nicht mehr hier). display_only=True -> das Dashboard startet
        # ihn NICHT, zeigt nur den Health-Status der .103-Box an.
        ServiceConfig(
            key="yuki", name="Yuki-Server (.103)", port=8443,
            health_url="https://127.0.0.1:8443/health",
            display_only=True,
            cmd=[str(PROJ / ".venv" / "Scripts" / "python.exe"), "-u", str(PROJ / "server.py")],
            cwd=str(PROJ),
            env={"PYTHONUNBUFFERED": "1"},
            start_timeout=60,
            # Routine-Recall-/Verdichtungs-Telemetrie raus: die Memory-Schichten
            # funktionieren stabil, ihre Pro-Turn-Logzeilen sind nur noch Rauschen.
            # Bewusst PRAEZISE (matchen den Routine-Erfolgsfall, NICHT die Fehler-
            # Varianten wie '...gescheitert/uebersprungen/fehlgeschlagen' - die
            # bleiben sichtbar). Standalone-Konsole sieht die prints weiter (Filter
            # ist nur die Dashboard-Ansicht). _on_line nutzt re.search.
            suppress_patterns=[
                r"\[Recall[: ]",            # [Recall kw -> n hits] + [Recall: keine Keywords...]
                r"\[Episodes-Recall ",
                r"\[People-Recall ",
                r"\[Lore-Recall ",
                r"\[Affinity-Recall ",
                r"\[Threads-Surface:",
                r"\[Fakten-Komprimierung: ",  # 'keine Reduktion'; Fehler-Variante hat kein ':' direkt dahinter
                r"-> Fakten verdichtet:",
            ],
        ),
        ServiceConfig(
            key="wytts", name="Yuki-Stimme (TTS)", port=10200,
            # Wyoming-Protokoll, kein HTTP -> tcp://-Health = reiner Connect-Test.
            # HA-Voice-Bridge: lauscht auf .101:10200, callt Yukis /tts auf dem Core .103
            # (verify=False in der Bridge -> self-signed .103-Cert ok). HA-Wyoming-TTS
            # zeigt weiterhin auf .101:10200.
            health_url="tcp://127.0.0.1:10200",
            cmd=[str(PROJ / ".venv" / "Scripts" / "python.exe"), "-u",
                 str(PROJ / "wyoming_tts_yuki.py"),
                 "--uri", "tcp://0.0.0.0:10200",
                 "--yuki-url", "https://127.0.0.1:8443/tts"],
            cwd=str(PROJ),
            env={"PYTHONUNBUFFERED": "1"},
            start_timeout=20,
        ),
        ServiceConfig(
            key="wystt", name="Yuki-STT (Whisper)", port=10300,
            # Wie die TTS-Bridge: Wyoming-Protokoll -> tcp://-Health. HA-Voice-Bridge:
            # lauscht auf .101:10300, callt Yukis /stt auf dem Core .103 (verify=False
            # -> self-signed ok). HA-Wyoming-STT zeigt weiterhin auf .101:10300.
            health_url="tcp://127.0.0.1:10300",
            cmd=[str(PROJ / ".venv" / "Scripts" / "python.exe"), "-u",
                 str(PROJ / "wyoming_stt_yuki.py"),
                 "--uri", "tcp://0.0.0.0:10300",
                 "--yuki-url", "https://127.0.0.1:8443/stt"],
            cwd=str(PROJ),
            env={"PYTHONUNBUFFERED": "1"},
            start_timeout=20,
        ),
        ServiceConfig(
            key="comfy", name="ComfyUI (Gedankenbild)", port=8000,
            # Remote auf der 4070 (config/imagegen.json server_url, Stand 2026-07-19
            # http://127.0.0.1:8000). Health via GET /system_stats (200 mit JSON).
            # Michael faehrt die Kiste manuell hoch -> das Dashboard startet/killt
            # sie NICHT (kein Popen-Handle, Remote-Host), zeigt nur ob sie lebt.
            # cmd bleibt leer: display_only ueberspringt jeden Start.
            health_url="http://127.0.0.1:8000/system_stats",
            cmd=[],
            display_only=True,
            # Multi-Server: prueft die ganze server_urls-Liste aus config/imagegen.json
            # (gruen, wenn IRGENDEIN Server laeuft). Fallback = einzelner health_url oben.
            health_fn=(_imagegen.is_comfyui_reachable if _imagegen else None),
            start_timeout=5,
        ),
    ]


# ---------------------------------------------------------------------------
# Health-Check (Threadsafe, ohne Textual-Loop)
# ---------------------------------------------------------------------------

def _tcp_ok(url: str, timeout: float = 2.0) -> bool:
    """Reiner TCP-Connect-Test fuer `tcp://host:port`-Health-URLs. Die Wyoming-TTS-
    Bridge spricht kein HTTP (eigenes Wyoming-Protokoll) - ein erfolgreicher Connect
    auf den Listen-Port = lebt."""
    hostport = url[len("tcp://"):]
    host, _, port = hostport.partition(":")
    try:
        with socket.create_connection((host or "127.0.0.1", int(port or 0)), timeout=timeout):
            return True
    except Exception:
        return False


def http_ok(url: str, timeout: float = 2.0) -> bool:
    """200-499 = lebt (auch 404 = Server antwortet). Self-signed wird akzeptiert.
    `tcp://`-URLs werden als reiner Connect-Test behandelt (Wyoming-Bridge).

    X-Forwarded-For + X-Real-IP werden mitgeschickt, weil SearXNG's Bot-Detection
    sonst jeden 'header-less' Healthz-Ping als verdaechtig markiert und den
    Granian-Worker beendet ('ERROR:searx.botdetection: X-Forwarded-For nor X-Real-IP
    header is set' im Container-Log). Restart-Policy startet den Container neu,
    Cycle wiederholt sich -> LED flackert rot/gruen. Andere Services ignorieren
    die Header (kosten nichts)."""
    if url.startswith("tcp://"):
        return _tcp_ok(url, timeout)
    try:
        ctx = _SSL_NOVERIFY if url.startswith("https") else None
        req = urllib.request.Request(url, headers={
            "User-Agent": "yuki-dashboard",
            "X-Forwarded-For": "127.0.0.1",
            "X-Real-IP": "127.0.0.1",
        })
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return 200 <= r.status < 500
    except urllib.error.HTTPError as e:
        return 400 <= e.code < 500  # 4xx = Server lebt
    except Exception:
        return False


def _service_health(cfg: "ServiceConfig", timeout: float = 2.0) -> bool:
    """Health eines Service. Nutzt cfg.health_fn falls gesetzt (z.B. ComfyUI-Multi-
    Server via imagegen.is_comfyui_reachable, prueft alle Server), sonst den HTTP/TCP-
    Check auf cfg.health_url. Fehler im health_fn -> down (nie Crash)."""
    if cfg.health_fn is not None:
        try:
            return bool(cfg.health_fn())
        except Exception:
            return False
    return http_ok(cfg.health_url, timeout)


# ---------------------------------------------------------------------------
# Subprocess-Wrapper: startet Service, liest stdout in Thread, callback pro Zeile
# ---------------------------------------------------------------------------

class ServiceProcess:
    def __init__(self, cfg: ServiceConfig, on_line: Callable[[str, str], None]):
        self.cfg = cfg
        self.on_line = on_line
        self.proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None

    def start(self) -> None:
        cfg = self.cfg
        if cfg.is_tray:
            try:
                subprocess.Popen(
                    cfg.cmd,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
                )
                self.on_line(cfg.key, "[Tray-App gestartet - kein Log-Stream verfuegbar]")
            except FileNotFoundError:
                self.on_line(cfg.key, f"[Tray-Binary nicht gefunden: {cfg.cmd[0]}]")
            except Exception as e:
                self.on_line(cfg.key, f"[Start fehlgeschlagen: {e}]")
            return

        env = os.environ.copy()
        env.update(cfg.env)
        try:
            self.proc = subprocess.Popen(
                cfg.cmd,
                cwd=cfg.cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        except FileNotFoundError:
            self.on_line(cfg.key, f"[Binary nicht gefunden: {cfg.cmd[0]}]")
            return
        except Exception as e:
            self.on_line(cfg.key, f"[Start fehlgeschlagen: {e}]")
            return

        self._reader = threading.Thread(
            target=self._read_stdout, args=(self.proc,),
            daemon=True, name=f"read-{cfg.key}",
        )
        self._reader.start()

    def _read_stdout(self, proc: subprocess.Popen) -> None:
        assert proc.stdout
        try:
            for line in proc.stdout:
                line = line.rstrip("\r\n")
                if line:
                    self.on_line(self.cfg.key, line)
        except Exception as e:
            self.on_line(self.cfg.key, f"[Reader-Fehler: {e}]")
        if proc is self.proc:
            self.on_line(self.cfg.key, f"[Prozess beendet (exit={proc.returncode})]")

    def stop(self, timeout: float = 3.0) -> None:
        for p in (self.proc,):
            if not p or p.poll() is not None:
                continue
            try:
                # CTRL_BREAK_EVENT geht an die Process-Group (CREATE_NEW_PROCESS_GROUP oben).
                p.send_signal(signal.CTRL_BREAK_EVENT)
                p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    p.kill()
                    p.wait(timeout=2.0)
                except Exception:
                    pass
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

    @property
    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


# ---------------------------------------------------------------------------
# GPU-Stats: nvidia-smi parsen (kein extra Dep)
# ---------------------------------------------------------------------------

def get_gpu_stats() -> Optional[tuple]:
    """(util_pct, vram_used_mb, vram_total_mb) oder None falls nicht verfuegbar."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi",
             "--query-gpu=utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL, timeout=2,
            creationflags=subprocess.CREATE_NO_WINDOW,
        ).decode("utf-8", errors="replace").strip().splitlines()
        if not out:
            return None
        u, mu, mt = [int(x.strip()) for x in out[0].split(",")]
        return u, mu, mt
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Yuki-Activity aus memory/ ziehen
# ---------------------------------------------------------------------------

# Spiegelt yuki_core HISTORY_CONSOLIDATE_AT (Default 30): die Runtime-Verdichtung
# triggert bei DIESER Anzahl NACHRICHTEN in conversation.json (user+assistant je 1,
# also ~2 pro Austausch -> ~15 echte Turns). Bei Config-Aenderung hier mitziehen.
_CONSOLIDATE_AT_MSGS = 30


def read_yuki_activity() -> tuple:
    """(turn_count, persona_key, last_turn_ts, msg_count) - alle Felder optional.
    turn_count = User-Nachrichten im Puffer; msg_count = alle Nachrichten (die
    Verdichtung triggert auf der NACHRICHTEN-Zahl, daher fuers Dashboard gebraucht)."""
    turns = 0
    persona = "?"
    last_ts = 0.0
    nmsgs = 0
    try:
        with open(MEMORY_DIR / "conversation.json", "r", encoding="utf-8") as f:
            conv = json.load(f)
        msgs = conv.get("messages", []) if isinstance(conv, dict) else conv
        if isinstance(msgs, list):
            nmsgs = len(msgs)
            turns = sum(1 for m in msgs if isinstance(m, dict) and m.get("role") == "user")
        last_ts = (MEMORY_DIR / "conversation.json").stat().st_mtime
    except Exception:
        pass
    try:
        with open(MEMORY_DIR / "yuki_persona.json", "r", encoding="utf-8") as f:
            data = json.load(f)
        persona = data.get("persona") or data.get("current") or "?"
    except Exception:
        pass
    return turns, persona, last_ts, nmsgs


# ---------------------------------------------------------------------------
# ASCII-Yuki-Katze: pro State eine Frame-Liste beliebiger Laenge.
# tick_frame iteriert via `% len(frames)` durch - jeder State kann unabhaengig
# 2, 4, 8 oder mehr Frames haben. Person-ASCII (Arme/Beine) saugt; Katze laesst
# sich mit /\_/\ Kopf + (")(") Pfoten robust niedlich halten.
# ---------------------------------------------------------------------------

def _F(s: str) -> str:
    """ASCII-Frame mit Code-Einrueckung im Quelltext lesbar halten.
    textwrap.dedent entfernt die gemeinsame fuehrende Einrueckung aller Zeilen.
    Wir entfernen NUR den ersten Zeilenumbruch (Triple-Quote-Artefakt) +
    trailing whitespace - bewusste leere Zeilen am Anfang BLEIBEN erhalten,
    damit alle States die gleiche Frame-Hoehe haben (sonst zappelt das Panel
    beim State-Wechsel zwischen 5 und 6 Zeilen). Raw-String r'...' damit '\\'
    in den ASCII-Strichen literal bleibt."""
    s = textwrap.dedent(s).rstrip()
    if s.startswith("\n"):
        s = s[1:]
    return s


YUKI_FRAMES = {
    "sleeping": [
        _F(r"""
                       z
              /\___/\
             (  -.-  )    z
              >     <
             (")___(")   Z
              ~~~~~~~
        """),
        _F(r"""
                     z z
              /\___/\
             (  u.u  )   Z
              >     <
             (")___(")    z
              ~~~~~~~
        """),
        _F(r"""
                    Z z z
              /\___/\
             (  -.-  )  z
              >     <
             (")___(")
              ~~~~~~~      Z
        """),
        _F(r"""
                   z Z z
              /\___/\    z
             (  u.u  )
              >     <    Z
             (")___(")
              ~~~~~~~
        """),
    ],
    "idle": [
        _F(r"""

              /\___/\
             (  o.o  )
              >  ^  <
             (")___(")
              /     \
        """),
        _F(r"""

              /\___/\
             (  -.-  )
              >  ^  <
             (")___(")
              /     \
        """),
        _F(r"""

              /\___/\
             (  o.o  )
              >  w  <
             (")___(")
              \     /
        """),
        _F(r"""

              /\___/\
             (  ^.^  )
              >  ^  <
             (")___(")
              /     \
        """),
    ],
    "thinking": [
        _F(r"""
                        ?
              /\___/\
             (  o.o  )
              >  -  <      o
             (")___(")
              /     \
        """),
        _F(r"""
                      ? ?
              /\___/\
             (  -.o  )
              >  -  <    o O
             (")___(")
              /     \
        """),
        _F(r"""
                    ? ? ?
              /\___/\
             (  o.-  )
              >  -  <   O o .
             (")___(")
              /     \
        """),
        _F(r"""
                       !
              /\___/\
             (  O.O  )
              >  o  <      *
             (")___(")
              /     \
        """),
    ],
    "speaking": [
        _F(r"""
                       ~
              /\___/\
             (  ^.^  )   /
              >  o  <
             (")___(")
              /     \
        """),
        _F(r"""
                     ~ J
              /\___/\
             (  ^.^  )    J
              >  O  <
             (")___(")
              /     \
        """),
        _F(r"""
                    ~ J ~
              /\___/\
             (  ^.^  )  J ~
              >  o  <
             (")___(")
              /     \
        """),
        _F(r"""
                     J ~
              /\___/\
             (  ^.^  )   ~
              >  O  <
             (")___(")
              /     \
        """),
    ],
    "working": [
        _F(r"""

              /\___/\
             (  o.o  )   >>
              >  ^  <  clack
             (")___(")
              /     \
        """),
        _F(r"""

              /\___/\
             (  -.o  )  >>>
              >  ^  <   clack
             (")___(")
              /     \
        """),
        _F(r"""

              /\___/\
             (  o.-  )    >>
              >  ^  <  tap
             (")___(")
              /     \
        """),
        _F(r"""

              /\___/\
             (  o.o  )   >
              >  ^  <    tap
             (")___(")
              /     \
        """),
    ],
    "busy": [
        _F(r"""
                      ! !
              /\___/\
             (  @.@  )  >>>
              >  o  <
             (")___(")  vrr
              =/=/=/=
        """),
        _F(r"""
                     !!!
              /\___/\   >>>
             (  >.<  )
              >  O  <    !!
             (")___(")
              =\=\=\=  vrr
        """),
        _F(r"""
                    ! !!
              /\___/\
             (  O.O  ) >>>>
              >  o  <
             (")___(")  vrr
              =/=/=/=    !
        """),
        _F(r"""
                     !!
              /\___/\    >>>
             (  @.@  )
              >  O  <  !!!
             (")___(")
              =\=\=\=   vrr
        """),
    ],
}


def pick_activity(cpu: float, gpu: Optional[int],
                  yuki_speaking: bool, yuki_thinking: bool,
                  idle_sec: float) -> str:
    if yuki_speaking:
        return "speaking"
    if yuki_thinking:
        return "thinking"
    if idle_sec > 300:
        return "sleeping"
    high_gpu = (gpu or 0) >= 60
    high_cpu = cpu >= 50
    if high_gpu or high_cpu:
        return "busy"
    med_gpu = (gpu or 0) >= 20
    med_cpu = cpu >= 15
    if med_gpu or med_cpu:
        return "working"
    return "idle"


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------

class ServiceRow(Widget):
    """Eine Zeile in der ServicePanel-Liste. Klickbar bei status == down (= Start)
    UND bei status == up wenn killbar (= Stop). Bewusst NICHT bei starting/pending,
    damit man waehrend des Startups nicht aus Versehen einen Service anschubst oder
    abwuergt (Popen ueber Popen = Port-Konflikt; Kill mitten im Modell-Laden = Muell).

    killable=False (= is_tray, aktuell nur Ollama) bleibt im up-State unklickbar:
    Die Tray-App wird detached gestartet, das Dashboard haelt kein Popen-Handle, ein
    Kill-Klick waere ein No-Op. Down bleibt fuer ALLE klickbar (Neustart-Versuch).

    Implementiert als Widget mit eigenem render() statt Static-Subklasse - Static
    hat in Textual 8.x eine Lifecycle-Ordnung die crasht wenn man ohne initialen
    Renderable durch den ersten Paint geht. Widget mit render() ist robust dafuer."""

    class Restart(Message):
        """Bubble nach oben: 'App, starte mir Service <key> neu'."""
        def __init__(self, key: str) -> None:
            super().__init__()
            self.key = key

    class Kill(Message):
        """Bubble nach oben: 'App, beende mir den laufenden Service <key>'
        (App haengt eine Ja/Nein-Bestaetigung davor)."""
        def __init__(self, key: str) -> None:
            super().__init__()
            self.key = key

    class Reload(Message):
        """Bubble nach oben: 'App, beende+starte den laufenden Service <key> neu'
        (= stop + start in einem Rutsch, OHNE Bestaetigung - der Service kommt
        ja sofort wieder hoch, anders als beim reinen Kill)."""
        def __init__(self, key: str) -> None:
            super().__init__()
            self.key = key

    DEFAULT_CSS = """
    ServiceRow {
        height: 1;
        padding: 0 1;
    }
    /* Nur klickbare Rows (rot=Start / gruen-killbar=Stop) reagieren auf die Maus:
       beim Drueberfahren ein dezenter Hintergrund-Boost + Fettung, damit man sieht,
       dass die Zeile anklickbar ist. starting/pending/Ollama-up bleiben optisch
       stumm (kein .-clickable -> kein Hover-Effekt). Der '(klick=...)'-Text bleibt
       der Dauer-Hinweis. */
    ServiceRow.-clickable:hover {
        background: $boost;
        text-style: bold;
    }
    """

    status = reactive("pending", layout=False)

    def __init__(self, key: str, name: str, port: int, killable: bool = True,
                 display_only: bool = False) -> None:
        # NICHT self._name/self._key nutzen - Widget hat eigene _name-Property,
        # super().__init__ wuerde sie ueberschreiben. _svc_* ist eindeutig.
        super().__init__(id=f"row-{key}")
        self._svc_key = key
        self._svc_name = name
        self._svc_port = port
        self._killable = killable
        self._display_only = display_only

    def _is_clickable(self, status: str) -> bool:
        if self._display_only:
            return False             # Remote-Status-Anzeige: nie start-/killbar
        if status == "down":
            return True              # Start-Versuch fuer alle
        if status == "up":
            return self._killable    # Stop nur fuer killbare (nicht Ollama-Tray)
        return False                 # starting/pending/skip: Finger weg

    def watch_status(self, _old: str, new: str) -> None:
        if self._is_clickable(new):
            self.add_class("-clickable")
        else:
            self.remove_class("-clickable")
        self.refresh()

    def set_status(self, status: str) -> None:
        # via reactive -> triggert watch_status -> refresh
        self.status = status

    def render(self) -> RenderableType:
        from rich.text import Text
        from rich.style import Style
        leds = {
            "pending":  ("○", "yellow"),   # noch nicht angefasst (Sequenz kommt)
            "starting": ("●", "yellow"),   # Popen lief, warte auf health
            "up":       ("●", "green"),    # health OK
            "down":     ("○", "red"),      # nicht erreichbar
            "skip":     ("○", "blue"),     # bewusst ausgelassen
        }
        glyph, color = leds.get(self.status, ("?", "dim"))
        # Feste Spalten: Name auf 18 (laenger -> mit … abgeschnitten), Port
        # rechtsbuendig auf 5. So stehen die Ports sauber untereinander und die
        # Klick-Ziele [X]/[↻] dahinter immer an derselben x-Position - ein langer
        # Name kann sie nicht mehr aus der Panelbreite schieben. (Vorher war
        # ':<18' nur eine Mindestbreite und schnitt NICHT ab -> die Wyoming-Namen
        # sprengten die Spalte und der Whisper-Kill-Button rutschte aus dem Bild.)
        name = self._svc_name if len(self._svc_name) <= 18 else self._svc_name[:17] + "…"
        t = Text()
        t.append(glyph, style=color)
        t.append(f"  {name:<18}  {self._svc_port:>5}")
        if self._display_only:
            # Reiner Remote-Status: kein Klick-Ziel, nur ein dezenter Hinweis
            # dass dieser Dienst bewusst nicht start-/killbar ist.
            t.append("  (extern)", style="dim")
        elif self.status == "down":
            t.append("  (klick=start)", style="dim")
        elif self.status == "up" and self._killable:
            # Zwei getrennte Klick-Ziele: [X] = nur beenden (rote LED danach),
            # [↻] = beenden UND sofort wieder starten. Die Zuordnung laeuft ueber
            # Style-meta ('btn') statt Spaltenrechnerei - der Klick traegt die
            # Style unter dem Cursor (event.style.meta), unabhaengig vom Padding.
            t.append("  ")
            t.append("[X]", style=Style(color="red", meta={"btn": "kill"}))
            t.append(" ")
            t.append("[↻]", style=Style(color="cyan", meta={"btn": "reload"}))
        return t

    def on_click(self, event) -> None:
        if self.status == "down":
            self.post_message(self.Restart(self._svc_key))
            return
        if self.status == "up" and self._killable:
            # Im up-State ist NUR der getroffene Button aktiv (Klick daneben =
            # No-Op). Welcher: aus der Style-meta unter dem Cursor.
            meta = event.style.meta if event.style else {}
            btn = meta.get("btn")
            if btn == "kill":
                self.post_message(self.Kill(self._svc_key))
            elif btn == "reload":
                self.post_message(self.Reload(self._svc_key))


class ServicePanel(Vertical):
    """Container fuer ServiceRows. Wird im App.compose mit 'with'-Block befuellt
    (dort werden die Rows angelegt + via register_row registriert). API: set_status,
    get_status, beide ueber die registrierte Row."""

    DEFAULT_CSS = """
    ServicePanel {
        height: auto;
    }
    """

    def __init__(self) -> None:
        super().__init__(id="services")
        self._rows: dict = {}

    def register_row(self, key: str, row: "ServiceRow") -> None:
        self._rows[key] = row

    def set_status(self, key: str, status: str) -> None:
        if key in self._rows:
            self._rows[key].set_status(status)

    def get_status(self, key: str) -> str:
        return self._rows[key].status if key in self._rows else "down"


# Mini-Balken-Renderer fuer RAM/VRAM-Auslastung. Volle Bloecke = belegt (green),
# leere Bloecke = frei (dim). Wird in der Stats-Zeile rechtsbuendig ans Panel-Ende
# gepaddet, damit das Auge die Auslastung in einem Sweep erfasst.
def _render_meter_bar(fraction: float, width: int = 10) -> str:
    """Rich-Markup-String der Form '[████░░░░░░]' fuer einen Mini-Balken.
    fraction: 0.0..1.0 (geclamped), width: sichtbare Glyphen zwischen den Brackets.
    Nur die OEFFNENDE Bracket muss escaped sein (\\[) - Rich erkennt ']' allein
    als literal, parsed nur '[' als Markup-Trigger. \\] waere ein Bug (wuerde
    als '\\' + ']' visible gerendert).

    Farben:
    - green       fuer belegt (terminal-default-gruen, passt sich Theme an)
    - rgb(60,60,60) fuer frei: explizit gedimmtes Grau (deutlich dunkler als
      Rich's 'dim' das je nach Terminal-Theme ~50% grau ist). Hochpegeln auf
      80-90 wuerde es heller machen; runter auf 30-40 fast schwarz."""
    f = max(0.0, min(1.0, fraction))
    filled = int(round(f * width))
    free = width - filled
    return (
        f"\\[[green]{'█' * filled}[/green]"
        f"[rgb(60,60,60)]{'░' * free}[/rgb(60,60,60)]]"
    )


# Innenbreite des linken Panels. 48 (CSS #left.width) minus Border (2)
# minus Padding (0 1 = 2). Hier zentralisiert, damit der Rechtsbuendig-Pad
# in update_stats das gleiche Mass nutzt.
_PANEL_INNER_WIDTH = 44
_METER_BAR_WIDTH = 10


# --- Fixed-Range-Sparkline (Y-Achse hartgepinnt 0..100) -----------------------
# Textual's built-in Sparkline skaliert die Y-Achse auto an min/max der Daten -
# bei reinen Niedrig-Last-Phasen (CPU=2..3%) sieht das aus wie eine voll-
# ausgelastete Spitzenkurve. Wir wollen 0..100% fest, damit man auf einen
# Blick sieht ob es Idle oder Volllast war. Dafuer eine eigene Renderable
# (kleiner Clone von textual.renderables.sparkline.Sparkline.__rich_console__
# mit ueberschriebenem minimum/maximum) + ein duenner Sparkline-Wrapper der
# sie statt der Original-Renderable instanziert.
from fractions import Fraction as _Fraction
from rich.segment import Segment as _Segment
from rich.style import Style as _RichStyle
from textual.renderables.sparkline import blend_colors as _blend_colors


class _FixedRangeSparklineRenderable:
    BARS = "▁▂▃▄▅▆▇█"

    def __init__(self, data, *, width, height, min_color, max_color,
                 summary_function=max, value_min: float = 0.0, value_max: float = 100.0):
        self.data = data
        self.width = width
        self.height = height
        self.min_color = _RichStyle.from_color(min_color)
        self.max_color = _RichStyle.from_color(max_color)
        self.summary_function = summary_function
        self.value_min = value_min
        self.value_max = value_max

    @classmethod
    def _buckets(cls, data, num_buckets):
        bucket_step = _Fraction(len(data), num_buckets)
        for bucket_no in range(num_buckets):
            start = int(bucket_step * bucket_no)
            end = int(bucket_step * (bucket_no + 1))
            partition = data[start:end]
            if partition:
                yield partition

    def __rich_console__(self, console, options):
        width = self.width or options.max_width
        height = self.height or 1
        len_data = len(self.data)
        if len_data == 0:
            for _ in range(height - 1):
                yield _Segment.line()
            yield _Segment("▁" * width, self.min_color)
            return
        bar_line_segments = len(self.BARS)
        bar_segments = bar_line_segments * height - 1
        # << OVERRIDE: feste Range statt min(data)/max(data). Werte werden
        # auf [value_min, value_max] geclamped damit Outlier (z.B. CPU bei
        # 105% durch psutil-Rundung) nicht den letzten Balken-Slot exploden.
        minimum = self.value_min
        maximum = self.value_max
        extent = (maximum - minimum) or 1
        min_color, max_color = self.min_color.color, self.max_color.color
        buckets = tuple(self._buckets(list(self.data), num_buckets=width))
        if not buckets:
            for _ in range(height - 1):
                yield _Segment.line()
            yield _Segment("▁" * width, self.min_color)
            return
        for i in reversed(range(height)):
            current_bar_part_low = i * bar_line_segments
            current_bar_part_high = (i + 1) * bar_line_segments
            bucket_index = 0.0
            bars_rendered = 0
            step = len(buckets) / width
            while bars_rendered < width:
                idx = min(int(bucket_index), len(buckets) - 1)
                partition = buckets[idx]
                partition_summary = self.summary_function(partition)
                clamped = max(minimum, min(maximum, partition_summary))
                height_ratio = (clamped - minimum) / extent
                bar_index = int(height_ratio * bar_segments)
                if bar_index < current_bar_part_low:
                    bar = " "
                    with_color = False
                elif bar_index >= current_bar_part_high:
                    bar = "█"
                    with_color = True
                else:
                    bar = self.BARS[bar_index % bar_line_segments]
                    with_color = True
                if with_color:
                    bar_color = _blend_colors(min_color, max_color, height_ratio)
                    style = _RichStyle.from_color(bar_color)
                else:
                    style = None
                bars_rendered += 1
                bucket_index += step
                yield _Segment(bar, style)
            if i > 0:
                yield _Segment.line()


class FixedRangeSparkline(Sparkline):
    """Sparkline mit hartgepinnter Y-Achse (Default 0..100). Sonst identisch
    zur Textual-Sparkline (data ist reactive, gleiche CSS-Component-Classes
    sparkline--min-color / sparkline--max-color)."""

    VALUE_MIN: float = 0.0
    VALUE_MAX: float = 100.0

    def render(self):
        data = self.data or []
        _, base = self.background_colors
        min_color = base + (
            self.get_component_styles("sparkline--min-color").color
            if self.min_color is None else self.min_color
        )
        max_color = base + (
            self.get_component_styles("sparkline--max-color").color
            if self.max_color is None else self.max_color
        )
        return _FixedRangeSparklineRenderable(
            data,
            width=self.size.width,
            height=self.size.height,
            min_color=min_color.rich_color,
            max_color=max_color.rich_color,
            summary_function=self.summary_function,
            value_min=self.VALUE_MIN,
            value_max=self.VALUE_MAX,
        )


class StatsPanel(Vertical):
    """CPU/RAM/GPU/VRAM + Yuki-Persona/Turns.

    Layout: 2 Sparklines (CPU + GPU) mit aktuellem Wert in der Label-Zeile drueber,
    dann RAM/VRAM/Yuki-Stats als Text-Block. Sparkline-Daten als rolling deque
    (40 Werte = 40s bei 1s-Tick). data=[0] init, sonst rendert Sparkline gar
    nichts und das Widget kollabiert auf 0px Hoehe waehrend des ersten Frames."""

    DEFAULT_CSS = """
    StatsPanel { height: auto; margin-bottom: 1; }
    StatsPanel Static { height: auto; }
    /* height: 2 verdoppelt die Sparkline-Aufloesung (8 -> 16 Hoehenstufen pro
       Spalte), kleine Lastunterschiede werden besser sichtbar. height: 3 ginge
       auch, frisst aber Layout-Platz; 2 ist der gute Mittelweg. */
    StatsPanel Sparkline { height: 2; margin: 0; }
    StatsPanel Sparkline > .sparkline--max-color { color: $accent; }
    StatsPanel Sparkline > .sparkline--min-color { color: $accent 40%; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._cpu_hist: deque = deque([0], maxlen=40)
        self._gpu_hist: deque = deque([0], maxlen=40)
        # System-Header als Abschnitts-Ueberschrift ueber CPU/GPU.
        self._sys_header = Static("[b]System[/b]", id="stats-sys-header")
        # CPU-Zeile traegt RAM inline (rechts neben Wert), analog zu GPU+VRAM.
        self._cpu_label = Static("CPU  --%   RAM  --/-- G", id="stats-cpu-label")
        # FixedRangeSparkline statt Sparkline: Y-Achse hartgepinnt 0..100, sonst
        # waere bei 3%-Last-Phasen alles auf max gestreckt und es saehe nach
        # Volllast aus. CPU + GPU sind beide Prozentwerte -> beide nutzen 0..100.
        self._cpu_spark = FixedRangeSparkline([0], summary_function=max, id="stats-cpu-spark")
        self._gpu_label = Static("GPU  --%   VRAM --/-- G", id="stats-gpu-label")
        self._gpu_spark = FixedRangeSparkline([0], summary_function=max, id="stats-gpu-spark")
        # Text-Block reduziert: nur noch Yuki-Subsection (Turns/Persona).
        self._text = Static("loading...", id="stats-text")

    def compose(self) -> ComposeResult:
        yield self._sys_header
        yield self._cpu_label
        yield self._cpu_spark
        yield self._gpu_label
        yield self._gpu_spark
        yield self._text

    def update_stats(self, cpu: float, ram_used: float, ram_total: float,
                     gpu: Optional[tuple], turns: int, persona: str,
                     nmsgs: int = 0) -> None:
        self._cpu_hist.append(cpu)
        # CPU-Zeile: 'CPU 42%   RAM 18.3/64.0G                 [████░░░░░░]'
        # Wichtig: visible_text fuer den Pad-Calc, NICHT der markup-String, sonst
        # zaehlen [b]/[/b] mit und der Balken rutscht nach links.
        cpu_text = f"[b]CPU[/b]  {cpu:>3.0f}%   RAM {ram_used:.1f}/{ram_total:.1f}G"
        cpu_visible = f"CPU  {cpu:>3.0f}%   RAM {ram_used:.1f}/{ram_total:.1f}G"
        cpu_bar = _render_meter_bar(ram_used / max(ram_total, 0.001), _METER_BAR_WIDTH)
        # +2 fuer die sichtbaren Brackets \[ \], die im Markup escaped sind
        # aber gerendert je 1 char belegen.
        pad = " " * max(1, _PANEL_INNER_WIDTH - len(cpu_visible) - _METER_BAR_WIDTH - 2)
        self._cpu_label.update(f"{cpu_text}{pad}{cpu_bar}")
        self._cpu_spark.data = list(self._cpu_hist)

        if gpu:
            u, mu, mt = gpu
            self._gpu_hist.append(float(u))
            gpu_text = f"[b]GPU[/b]  {u:>3}%   VRAM {mu/1024:.1f}/{mt/1024:.1f}G"
            gpu_visible = f"GPU  {u:>3}%   VRAM {mu/1024:.1f}/{mt/1024:.1f}G"
            gpu_bar = _render_meter_bar(mu / max(mt, 1), _METER_BAR_WIDTH)
            pad = " " * max(1, _PANEL_INNER_WIDTH - len(gpu_visible) - _METER_BAR_WIDTH - 2)
            self._gpu_label.update(f"{gpu_text}{pad}{gpu_bar}")
        else:
            self._gpu_hist.append(0.0)
            self._gpu_label.update("[b]GPU[/b]  N/A")
        self._gpu_spark.data = list(self._gpu_hist)

        # Verdichtung triggert bei _CONSOLIDATE_AT_MSGS NACHRICHTEN (~2 pro Austausch).
        # Daher zusaetzlich zur (oszillierenden) Turn-Zahl: Fortschritt zur naechsten
        # Verdichtung + grobe Turn-Restschaetzung, damit klar ist worauf es zulaeuft.
        to_go = max(0, _CONSOLIDATE_AT_MSGS - nmsgs)
        turns_to_go = (to_go + 1) // 2          # ~2 Nachrichten/Turn, aufgerundet
        verd = (f"{nmsgs}/{_CONSOLIDATE_AT_MSGS}  "
                + ("(jetzt)" if turns_to_go <= 0 else f"(in ~{turns_to_go} Turns)"))
        self._text.update(
            "\n[b]Yuki[/b]\n"
            f" Turns       {turns}\n"
            f" Verdichtung {verd}\n"
            f" Persona  {persona}"
        )


class AvatarPanel(Static):
    """Yuki-Visual mit zwei Stilen, umschaltbar via Hotkey 'a' oder --avatar-style:

    - 'ascii'    : animierte Katzen-Frames pro Activity-State (tick_frame zaehlt
                   alle 0.6s rauf), 6 States x 4 Frames = der bisherige Look.
    - 'halfblock': persona-passender Background als RGB-Pixelart via Pillow
                   + Unicode-Halfblock + ANSI 24-bit Truecolor. Kein Frame-Loop,
                   re-render nur bei Persona-/State-/Style-Wechsel (gecached).
                   Fallback auf ASCII wenn Pillow fehlt oder Background nicht da."""

    state = reactive("idle")
    _frame_idx = reactive(0)
    avatar_style = reactive("ascii")  # 'ascii' | 'halfblock'

    # Half-Block-Breite (= Pixel-Spalten). Rechnet sich aus der Panel-Breite:
    # linkes Panel ist 48 chars breit, Border zieht 2 ab, Padding (0 1) noch 2,
    # Innenbreite = 44. Hoehe folgt Aspect-Ratio des PNGs (~22 Cells bei
    # quadratischen 2688x2688-Backgrounds - belegt eine deutliche Flaeche).
    # Wenn du das Panel mal aenderst: hier nachziehen.
    HALFBLOCK_WIDTH = 44

    # Frames pro Halfblock-State. Sollte zur Anzahl screenshoteter Yuki-Sprites
    # in avatar/sprites/Yuki_<state>_<n>.png passen (n=1..N).
    HALFBLOCK_FRAMES_PER_STATE = 4

    def __init__(self) -> None:
        super().__init__()
        self._persona = "?"
        # Halfblock-Cache: 2-stufig. (a) Background-only fuer Personas ohne
        # Sprites; (b) Sprite-Composite pro (state, frame, persona, width).
        # rich.Text-Objekte, weil Textual die direkt an self.update() schluckt.
        self._cached_hb_bg = None              # (Text, key) Background-Fallback
        self._cached_hb_bg_key: tuple = ()
        self._cached_hb_sprites: dict = {}     # {(state, frame, persona, width): Text}

    def watch_state(self, _new: str) -> None:
        self._frame_idx = 0
        self._refresh()

    def watch_avatar_style(self, _old: str, _new: str) -> None:
        self._refresh()

    def tick_frame(self) -> None:
        # Frame-Animation in BEIDEN Modi: ASCII rotiert die Katze, Halfblock
        # rotiert die 4 gerenderten Sprites (gecached -> kein PIL-Aufruf pro Tick).
        if self.avatar_style == "halfblock":
            self._frame_idx = (self._frame_idx + 1) % self.HALFBLOCK_FRAMES_PER_STATE
            self._refresh()
            return
        frames = YUKI_FRAMES.get(self.state, YUKI_FRAMES["idle"])
        self._frame_idx = (self._frame_idx + 1) % max(1, len(frames))
        self._refresh()

    def update_persona(self, persona: str) -> None:
        """Vom _tick_stats getriggert. Invalidiert den BG-Cache; Sprite-Cache
        ist persona-keyed und nutzt automatisch den richtigen Eintrag."""
        if persona != self._persona:
            self._persona = persona
            self._cached_hb_bg = None
            if self.avatar_style == "halfblock":
                self._refresh()

    def _refresh(self) -> None:
        if self.avatar_style == "halfblock":
            self._render_halfblock()
        else:
            self._render_ascii()

    def _render_ascii(self) -> None:
        frames = YUKI_FRAMES.get(self.state, YUKI_FRAMES["idle"])
        idx = self._frame_idx % len(frames)
        label = self.state.upper()
        self.update(f"[b dim]Yuki: {label}[/b dim]\n{frames[idx]}")

    def _render_halfblock(self) -> None:
        # Fallback-Kette: Pillow fehlt -> ASCII. Sprite vorhanden -> Sprite-
        # Composite mit BG. Sprite weg -> BG-only. BG weg -> ASCII.
        if not _HALFBLOCK_AVAILABLE:
            self._render_ascii()
            return
        from rich.text import Text as _RT
        text = self._get_halfblock_sprite() or self._get_halfblock_bg_only()
        if text is None:
            self._render_ascii()
            return
        frame_num = (self._frame_idx % self.HALFBLOCK_FRAMES_PER_STATE) + 1
        header = _RT.from_markup(
            f"[b dim]Yuki: {self.state.upper()}  ({self._persona})  "
            f"f{frame_num}[/b dim]\n"
        )
        header.append(text)
        self.update(header)

    def _get_halfblock_sprite(self):
        """Sprite-Composite (state-spezifisch, animiert). None wenn Sprite fehlt."""
        if _render_sprite_halfblock is None:
            return None
        frame = (self._frame_idx % self.HALFBLOCK_FRAMES_PER_STATE) + 1
        key = (self.state, frame, self._persona, self.HALFBLOCK_WIDTH)
        cached = self._cached_hb_sprites.get(key)
        if cached is not None:
            return cached
        try:
            ansi = _render_sprite_halfblock(
                self.state, frame, self._persona, width=self.HALFBLOCK_WIDTH
            )
        except Exception:
            ansi = None
        if ansi is None:
            return None
        from rich.text import Text as _RT
        text = _RT.from_ansi(ansi)
        self._cached_hb_sprites[key] = text
        return text

    def _get_halfblock_bg_only(self):
        """Persona-Background ohne Sprite. Fallback wenn keine Sprites da."""
        key = (self._persona, self.HALFBLOCK_WIDTH)
        if self._cached_hb_bg is not None and key == self._cached_hb_bg_key:
            return self._cached_hb_bg
        bg_path = _get_persona_background(self._persona)
        if bg_path is None:
            return None
        try:
            img = _PILImage.open(bg_path)
            ansi = _render_halfblock_ansi(img, width=self.HALFBLOCK_WIDTH)
        except Exception:
            return None
        from rich.text import Text as _RT
        self._cached_hb_bg = _RT.from_ansi(ansi)
        self._cached_hb_bg_key = key
        return self._cached_hb_bg


# ---------------------------------------------------------------------------
# Bestaetigungs-Overlay (Service wirklich beenden?)
# ---------------------------------------------------------------------------

class ConfirmKill(ModalScreen[bool]):
    """Kleines Ja/Nein-Overlay vor jedem Service-Kill. dismiss(True)=beenden,
    dismiss(False)=abbrechen. Esc/Aussenklick = abbrechen (sicher per Default)."""

    DEFAULT_CSS = """
    ConfirmKill {
        align: center middle;
    }
    ConfirmKill > #confirm-box {
        width: 56;
        height: auto;
        padding: 1 2;
        border: round $warning;
        background: $surface;
    }
    ConfirmKill #confirm-msg {
        width: 100%;
        content-align: center middle;
        margin-bottom: 1;
    }
    ConfirmKill #confirm-btns {
        height: auto;
        align: center middle;
    }
    ConfirmKill Button {
        margin: 0 1;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Abbrechen", show=False)]

    def __init__(self, svc_name: str) -> None:
        super().__init__()
        self._svc_name = svc_name

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Label(f"Service '{self._svc_name}' wirklich beenden?", id="confirm-msg")
            with Horizontal(id="confirm-btns"):
                yield Button("Ja, beenden", variant="error", id="yes")
                yield Button("Abbrechen", variant="primary", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_cancel(self) -> None:
        self.dismiss(False)


# ---------------------------------------------------------------------------
# Yuki-Tab Sub-Filter: der Yuki-Server ist die einzige In-Process-Pipeline und
# spuckt vermischte Logpfade aus (HTTP-Access, Whisper/STT, Core-Memory-Komprimie-
# rung, Rest). Damit der Yuki-Tab nach Logpfad filterbar wird, klassifiziert
# classify_yuki_log() jede Zeile in GENAU eine Kategorie. Reihenfolge der Checks
# = Prioritaet (HTTP zuerst, weil Access-Logs am eindeutigsten sind). "Alle" zeigt
# alles (Firehose, bewusst inkl. dem in c1ecd11 nur fuer die Tab-Ansicht
# entrauschten Recall-/Komprimierungs-Geschwafel - das lebt jetzt unter 'Memory').
YUKI_LOG_FILTERS = [
    ("all",    "Alle"),
    ("http",   "HTTP"),
    ("stt",    "Whisper"),
    ("memory", "Memory"),
    ("rest",   "Rest"),
]
# werkzeug-Access-Log: '... "GET /pfad HTTP/1.1" 200 -' (deckt geladene Dateien ab).
_YUKI_CAT_HTTP = re.compile(r'"(GET|POST|PUT|DELETE|HEAD|OPTIONS|PATCH) .*? HTTP/\d')
# Whisper/STT: die [STT-raw ...]-Diagnosezeile + Whisper-Modell-Lade-/Warmup-Logs.
_YUKI_CAT_STT = re.compile(r'\[STT-raw|[Ww]hisper')
# Core-Memory: Recall-Telemetrie aller Tiers + Komprimierungs-/Decay-/Supersession-
# Ausgaben. Bracket-Praefixe + deutsche Komprimierungs-Worte.
_YUKI_CAT_MEM = re.compile(
    r'\[(Recall|Episodes-Recall|People-Recall|Lore-Recall|Affinity-Recall|'
    r'Threads-Surface|Heart|Episode|Habit)|'
    r'[Kk]omprim|verdichtet|[Dd]ecay|[Ss]upersess')


def classify_yuki_log(line: str) -> str:
    """Yuki-Logzeile -> Kategorie-Key ('http'/'stt'/'memory'/'rest') fuer den
    Tab-Sub-Filter. Erste passende Regel gewinnt; ohne Treffer -> 'rest'."""
    if _YUKI_CAT_HTTP.search(line):
        return "http"
    if _YUKI_CAT_STT.search(line):
        return "stt"
    if _YUKI_CAT_MEM.search(line):
        return "memory"
    return "rest"


# ---------------------------------------------------------------------------
# Haupt-App
# ---------------------------------------------------------------------------

class YukiDashboard(App):

    CSS = """
    Screen {
        background: $surface;
    }
    #left {
        width: 48;
        border: solid $primary;
        padding: 0 1;
    }
    #right {
        /* 1fr: fuellt EXAKT den Rest neben #left (width:48). Ohne das Pin waechst
           TabbedContent mit der laengsten wrap:false-Logzeile ueber den rechten
           Terminalrand hinaus - dabei rutscht der vertikale Scrollbalken aus dem
           Bild. Mit 1fr scrollt der RichLog stattdessen INTERN (horizontal +
           vertikal), beide Scrollbalken bleiben sichtbar. */
        width: 1fr;
        border: solid $primary;
    }
    #services {
        height: auto;
        margin-bottom: 1;
    }
    StatsPanel {
        height: auto;
        margin-bottom: 1;
    }
    AvatarPanel {
        height: auto;
        color: $accent;
    }
    RichLog {
        background: $boost;
        /* Tab fuellen statt an der laengsten Zeile auszurichten - sonst koennte der
           RichLog die TabPane breiter als #right ziehen (overflow-x scrollt intern). */
        width: 1fr;
        height: 1fr;
    }
    TabbedContent {
        height: 1fr;
    }
    /* Yuki-Tab Sub-Filter-Leiste: eine Zeile hoch, Buttons kompakt + randlos,
       aktiver Filter via .-active hervorgehoben. */
    #yuki-filterbar {
        height: 1;
        width: 1fr;
        background: $panel;
    }
    .yfilt {
        height: 1;
        min-width: 0;
        width: auto;
        border: none;
        padding: 0 1;
        margin: 0 1 0 0;
        background: $boost;
        color: $text-muted;
    }
    .yfilt.-active {
        background: $primary;
        color: $text;
        text-style: bold;
    }
    #bottombar {
        dock: bottom;
        height: 1;
    }
    #bottombar Footer {
        dock: none;
        width: 1fr;
        height: 1;
    }
    #select-hint {
        width: auto;
        height: 1;
        color: $text-muted;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "restart_yuki", "Restart Yuki"),
        Binding("a", "toggle_avatar_style", "Avatar-Style"),
        Binding("y", "copy_log", "Log kopieren"),
        Binding("c", "clear_log", "Clear Log"),
        Binding("f", "cycle_yuki_filter", "Filter (Yuki)"),
        Binding("1", "switch_tab('all')", "All", show=False),
        Binding("2", "switch_tab('ollama')", "Ollama", show=False),
        Binding("3", "switch_tab('vision')", "Vision", show=False),
        Binding("4", "switch_tab('qwen')", "Qwen3-TTS", show=False),
        Binding("5", "switch_tab('searx')", "SearXNG", show=False),
        Binding("6", "switch_tab('yuki')", "Yuki", show=False),
        Binding("7", "switch_tab('wytts')", "Wyoming-TTS", show=False),
        Binding("8", "switch_tab('wystt')", "Wyoming-STT", show=False),
        Binding("0", "switch_tab('comfy')", "ComfyUI", show=False),
    ]

    def __init__(self, dry_run: bool = False, avatar_style: Optional[str] = None) -> None:
        super().__init__()
        self._dry_run = dry_run
        self._services_cfg = load_services()
        self._procs: dict = {}
        for cfg in self._services_cfg:
            self._procs[cfg.key] = ServiceProcess(cfg, self._on_line_threadsafe)
        # Pro Service vorab kompilierte Suppress-Regexes (Log-Rauschen filtern).
        self._suppress: dict = {
            cfg.key: [re.compile(p) for p in cfg.suppress_patterns]
            for cfg in self._services_cfg
        }
        # State fuer Activity-Detection
        self._last_yuki_turn_ts = 0.0
        self._yuki_speaking_until = 0.0
        self._yuki_thinking_until = 0.0
        # Yuki-Tab Sub-Filter: Ring-Buffer aller klassifizierten Yuki-Zeilen
        # (cat, line) + aktive Kategorie. Buffer ueberlebt Filter-Wechsel, damit
        # _apply_yuki_filter die Ansicht ohne Datenverlust neu rendern kann. Cap
        # == max_lines des RichLogs (2000), sonst zeigt "Alle" mehr als der Tab.
        self._yuki_log_buf: deque = deque(maxlen=2000)
        self._yuki_filter = "all"
        # Caches fuer geteilte, blockierende Polls (nvidia-smi/psutil). _tick_stats
        # (1s) befuellt sie in einem Thread; _tick_activity (2s) liest nur noch den
        # Cache, statt selbst nvidia-smi zu spawnen -> kein doppelter Subprocess
        # mehr UND nichts Blockierendes auf dem Textual-Event-Loop.
        self._cpu_cache: float = 0.0
        self._gpu_cache: Optional[tuple] = None
        # RAM total nur einmal lesen
        self._ram_total_gb = psutil.virtual_memory().total / 1024**3
        # Avatar-Style: CLI-Flag schlaegt Persistierung, sonst aus
        # memory/yuki_dashboard.json oder Default 'ascii'.
        settings = _load_dashboard_settings()
        chosen = avatar_style or settings.get("avatar_style") or "ascii"
        if chosen not in ("ascii", "halfblock"):
            chosen = "ascii"
        self._initial_avatar_style = chosen

    # --- Compose ---

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True, name="Yuki Dashboard")
        with Horizontal():
            with Vertical(id="left"):
                self.services_panel = ServicePanel()
                with self.services_panel:
                    yield Static("[b]Services[/b]")
                    for s in self._services_cfg:
                        # killable = nicht-Tray: Ollama (is_tray) wird detached
                        # gestartet, kein Popen-Handle -> im up-State unklickbar.
                        row = ServiceRow(s.key, s.name, s.port,
                                         killable=not s.is_tray,
                                         display_only=s.display_only)
                        self.services_panel.register_row(s.key, row)
                        yield row
                self.stats_panel = StatsPanel()
                yield self.stats_panel
                self.avatar_panel = AvatarPanel()
                # Initial-Style aus __init__ uebernehmen (vor compose gesetzt
                # geht nicht - reactive triggert sonst watch_avatar_style auf
                # noch nicht gemountetem Widget).
                self.avatar_panel.avatar_style = self._initial_avatar_style
                yield self.avatar_panel
            with TabbedContent(id="right", initial="tab-all"):
                # max_lines: ohne Cap kann ein lange laufender Yuki/Vision-Tab
                # zigtausend Zeilen sammeln; Tab-Wechsel braucht dann 3-5s zum
                # Re-Rendern. 2000/5000 ist genug History fuer Diagnose, schnell genug.
                yield TabPane("All", RichLog(id="log-all", wrap=False, markup=False,
                                              auto_scroll=True, max_lines=5000), id="tab-all")
                for cfg in self._services_cfg:
                    log = RichLog(id=f"log-{cfg.key}", wrap=False, markup=False,
                                  auto_scroll=True, max_lines=2000)
                    if cfg.key == "yuki":
                        # Yuki = einzige In-Process-Pipeline -> Sub-Filter-Leiste
                        # ueber dem Log. Buttons cyclen auch per Hotkey 'f'.
                        filterbar = Horizontal(
                            *[Button(label, id=f"yfilt-{fk}",
                                     classes="yfilt -active" if fk == "all" else "yfilt")
                              for fk, label in YUKI_LOG_FILTERS],
                            id="yuki-filterbar",
                        )
                        yield TabPane(cfg.name, filterbar, log, id=f"tab-{cfg.key}")
                    else:
                        yield TabPane(cfg.name, log, id=f"tab-{cfg.key}")
        # Bottom-Bar: Footer (Tastenkuerzel, linksbuendig) + Selektions-Hinweis
        # rechtsbuendig. Footer un-docked (dock:none), damit beide im Horizontal
        # nebeneinander liegen statt uebereinander zu stapeln.
        with Horizontal(id="bottombar"):
            yield Footer()
            yield Static("Shift+Alt+Drag = Text markieren", id="select-hint")

    # --- Lifecycle ---

    def on_mount(self) -> None:
        # ServiceRows rendern sich selbst in ihrem on_mount; refresh_render entfaellt.
        self._post_intro_lines()
        # Polls. Health-Intervall absichtlich gross: Yuki loggt jeden Hit
        # als 400er/200er-Werkzeug-Access-Line - bei 3s waren das ~20 Spam-Zeilen
        # pro Minute pro Service. 15s reicht; wenn man auf Service-Up wartet,
        # macht das die _startup_sequence ohnehin eng (1s-Steps).
        self.set_interval(1.0, self._tick_stats)
        self.set_interval(15.0, self._tick_health)
        self.set_interval(0.6, self._tick_avatar_frame)
        # Im Dry-Run: NICHT die Activity-Detection laufen lassen (wuerde unseren
        # Demo-Cycle ueberschreiben), sondern stattdessen alle States nacheinander
        # vorzeigen damit man jeden Frame jedes States 1-2x sieht.
        if self._dry_run:
            self.run_worker(self._dry_run_demo(), exclusive=False)
        else:
            self.set_interval(2.0, self._tick_activity)
        # Services in serieller Reihenfolge starten (analog start_yuki.ps1)
        self.run_worker(self._startup_sequence(), exclusive=False)

    def _post_intro_lines(self) -> None:
        log = self.query_one("#log-all", RichLog)
        mode = "Dry-Run (Services nicht starten)" if self._dry_run else "Startup-Sequenz laeuft..."
        log.write(Text(f"Yuki Dashboard - {mode}", style="bold cyan"))
        log.write("q=Quit  r=Restart Yuki  y=Log kopieren  c=Clear Log  1-9=Tab")

    async def _dry_run_demo(self) -> None:
        """Demo-Loop fuer --dry-run: cyclet durch alle States, jeden lang genug
        dass die Frame-Animation 1.5x durchlaeuft (jeder Frame ist mindestens
        einmal zu sehen). Postet State-Name in den 'All'-Log, damit man weiss
        was gerade durchlaeuft."""
        states = ["sleeping", "idle", "thinking", "speaking", "working", "busy"]
        TICK = 0.6  # passend zu _tick_avatar_frame-Intervall
        while True:
            for state in states:
                self.avatar_panel.state = state
                self._on_line("yuki", f"[demo: state -> {state}]")
                n = len(YUKI_FRAMES.get(state, [])) or 2
                # 1.5 Frame-Loops + 0.6s Atempause am Ende jedes States
                await asyncio.sleep(n * TICK * 1.5 + 0.6)

    async def _startup_sequence(self) -> None:
        if self._dry_run:
            self._on_line("yuki", "[dry-run: ueberspringe Service-Start]")
            return
        order = ["ollama", "vision", "qwen", "stt", "capture", "searx", "yuki", "wytts", "wystt", "comfy"]
        for key in order:
            cfg = next(c for c in self._services_cfg if c.key == key)
            # Rein anzeigende Remote-Dienste (ComfyUI): NIE starten, nur einmal
            # Health pruefen + LED setzen. Danach haelt _tick_health die LED aktuell
            # (up=erreichbar / down=aus). Kein Popen, kein 'starting'-Zwischenschritt.
            if cfg.display_only:
                up = await asyncio.to_thread(_service_health, cfg, 2.0)
                self.services_panel.set_status(key, "up" if up else "down")
                self._on_line(key, f"[{cfg.name} "
                                   f"{'erreichbar' if up else 'nicht erreichbar'}"
                                   f" - nur Status, kein Start/Stop]")
                continue
            # http_ok ist synchroner urlopen - in einen Thread, sonst friert die UI
            # (Tab-Wechsel, Sparklines, Avatar) waehrend des Checks ein. Genau hier
            # entstand das 2-3s-Startup-Ruckeln: ein Service hat den Port schon
            # gebunden, laedt aber noch Modelle -> urlopen haengt bis zum Timeout.
            if await asyncio.to_thread(http_ok, cfg.health_url, 2.0):
                self.services_panel.set_status(key, "up")
                self._on_line(key, f"[{cfg.name} laeuft schon - kein Neustart]")
                continue
            self.services_panel.set_status(key, "starting")
            self._on_line(key, f"[Starte {cfg.name}...]")
            self._procs[key].start()
            # Auf Health warten (bis start_timeout, in 1s-Steps)
            deadline = time.monotonic() + cfg.start_timeout
            while time.monotonic() < deadline:
                await asyncio.sleep(1.0)
                if await asyncio.to_thread(http_ok, cfg.health_url, 1.5):
                    self.services_panel.set_status(key, "up")
                    self._on_line(key, f"[{cfg.name} ist oben]")
                    break
            else:
                self.services_panel.set_status(key, "down")
                self._on_line(key, f"[WARNUNG: {cfg.name} antwortet nach {cfg.start_timeout}s nicht]")

    # --- Callbacks aus Reader-Threads ---

    def _on_line_threadsafe(self, key: str, line: str) -> None:
        # call_from_thread schedulet die UI-Update auf der Textual-Loop.
        try:
            self.call_from_thread(self._on_line, key, line)
        except Exception:
            pass  # App schon zu

    def _on_line(self, key: str, line: str) -> None:
        suppressed = any(pat.search(line) for pat in self._suppress.get(key, ()))
        if key == "yuki":
            # Yuki-Tab = Firehose mit Sub-Filter: NICHT droppen, sondern jede Zeile
            # klassifizieren + buffern. Das Aufraeumen macht der Filter (Alle/HTTP/
            # Whisper/Memory/Rest), nicht mehr ein globaler Suppress. Geschrieben
            # wird nur, wenn die Zeile zur aktiven Kategorie passt.
            cat = classify_yuki_log(line)
            self._yuki_log_buf.append((cat, line))
            if self._yuki_filter == "all" or self._yuki_filter == cat:
                try:
                    self.query_one("#log-yuki", RichLog).write(line)
                except Exception:
                    pass
        elif not suppressed:
            # Andere Services: Suppress droppt weiter (z.B. Health-Poll-Noise).
            try:
                self.query_one(f"#log-{key}", RichLog).write(line)
            except Exception:
                pass
        # "All"-Aggregator bleibt cross-service entrauscht (Suppress gilt dort
        # weiter, auch fuer Yuki) - das ist die Uebersichts-Ansicht, nicht der
        # Yuki-Deep-Dive.
        if not suppressed:
            try:
                self.query_one("#log-all", RichLog).write(f"[{key}] {line}")
            except Exception:
                pass
        # Yuki-Activity-Detection: einfache Pattern aus server.py-stdout
        if key == "yuki":
            low = line.lower()
            now = time.time()
            if "/respond" in low or "chat_ollama" in low or "generate_reply" in low:
                self._yuki_thinking_until = now + 4.0
                self._last_yuki_turn_ts = now
            if "tts_stream" in low or "/tts" in low or "[tts" in low:
                self._yuki_speaking_until = now + 5.0

    # --- Polls ---

    async def _tick_stats(self) -> None:
        # psutil-Reads sind instant (cpu_percent(interval=None) misst seit dem
        # letzten Aufruf, virtual_memory ist ein Syscall) -> bleiben auf dem Loop.
        # nvidia-smi (Subprocess, kann unter GPU-Last 100ms-1s+ kosten) und der
        # JSON-Read von conversation.json wandern in einen Thread, damit der
        # Event-Loop (Tab-Wechsel, Sparklines, Avatar) waehrenddessen frei bleibt.
        cpu = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory()
        ram_used = ram.used / 1024**3
        gpu, (turns, persona, last_mtime, nmsgs) = await asyncio.gather(
            asyncio.to_thread(get_gpu_stats),
            asyncio.to_thread(read_yuki_activity),
        )
        # Caches fuer _tick_activity (liest nur, spawnt kein eigenes nvidia-smi).
        self._cpu_cache = cpu
        self._gpu_cache = gpu
        if last_mtime and last_mtime > self._last_yuki_turn_ts:
            self._last_yuki_turn_ts = last_mtime
        self.stats_panel.update_stats(cpu, ram_used, self._ram_total_gb, gpu, turns, persona, nmsgs)
        # Persona an AvatarPanel - im Half-Block-Modus wird dadurch das
        # passende Background-PNG neu geladen.
        self.avatar_panel.update_persona(persona)

    async def _tick_health(self) -> None:
        # pending: Startup-Sequenz ist noch nicht durch -> nicht eigenmaechtig auf
        # 'down' setzen, sonst flackert die LED zwischen pending und down hin und
        # her solange die Sequenz noch andere Services bearbeitet.
        targets = [
            (cfg, current)
            for cfg in self._services_cfg
            if (current := self.services_panel.get_status(cfg.key)) != "pending"
        ]
        if not targets:
            return
        # Alle Health-Checks parallel in Threads - der synchrone urlopen kann pro
        # Service bis zum Timeout (1s) haengen wenn ein Port gebunden aber noch
        # busy ist. Frueher summierte sich das nacheinander auf dem Loop zu bis zu
        # 6s Freeze; jetzt blockt gar nichts und die Wandzeit ist max. ~1s parallel.
        ups = await asyncio.gather(*[
            asyncio.to_thread(_service_health, cfg, 1.0) for cfg, _ in targets
        ])
        for (cfg, current), up in zip(targets, ups):
            if up:
                new = "up"
            elif self._procs[cfg.key].is_alive:
                new = "starting"
            else:
                new = "down"
            if new != current:
                # Toast NUR beim Uebergang up->down (nicht starting->down etc.),
                # sonst kriegt man beim ersten Startup-Versuch oder beim
                # manuellen Restart unnoetig Spam.
                if current == "up" and new == "down":
                    self._on_line(cfg.key, f"[!] {cfg.name} ist down - Toast wird gesendet")
                    _send_toast(
                        f"Yuki: {cfg.name} ist down",
                        f"Port {cfg.port} antwortet nicht mehr. Klick auf die LED im Dashboard zum Restart.",
                    )
                self.services_panel.set_status(cfg.key, new)

    def _tick_avatar_frame(self) -> None:
        self.avatar_panel.tick_frame()

    def _tick_activity(self) -> None:
        # Liest nur die von _tick_stats (1s) gefuellten Caches - hoechstens 1s alt,
        # voellig ausreichend fuer die Activity-Heuristik. Kein eigener nvidia-smi-
        # Subprocess mehr und damit nichts Blockierendes auf dem Loop.
        cpu = self._cpu_cache
        gpu = self._gpu_cache
        gpu_util = gpu[0] if gpu else None
        now = time.time()
        speaking = now < self._yuki_speaking_until
        thinking = now < self._yuki_thinking_until
        idle_sec = max(0.0, now - self._last_yuki_turn_ts) if self._last_yuki_turn_ts else 9999
        new_state = pick_activity(cpu, gpu_util, speaking, thinking, idle_sec)
        if new_state != self.avatar_panel.state:
            self.avatar_panel.state = new_state

    # --- Actions ---

    def action_quit(self) -> None:
        self._on_line("yuki", "[Dashboard wird beendet - stoppe Services...]")
        for key in ("wystt", "wytts", "yuki", "qwen", "vision", "stt", "capture"):  # alle Owned-Procs
            try:
                self._procs[key].stop(timeout=3.0)
            except Exception:
                pass
        # SearXNG laeuft im Container weiter (restart:unless-stopped).
        # Ollama Tray-App auch.
        self.exit()

    def action_restart_yuki(self) -> None:
        self._restart_service("yuki", reason="Hotkey r")

    def on_service_row_restart(self, message: ServiceRow.Restart) -> None:
        """Klick auf eine rote ServiceRow -> Service neu starten."""
        self._restart_service(message.key, reason="Klick")

    def on_service_row_kill(self, message: ServiceRow.Kill) -> None:
        """Klick auf eine gruene (laufende) ServiceRow -> erst Ja/Nein-Overlay,
        bei Ja den Service beenden. Health-Tick faerbt die LED danach rot
        (= wieder klickbar fuer einen Neustart)."""
        cfg = next((c for c in self._services_cfg if c.key == message.key), None)
        if cfg is None:
            return

        def _after(confirmed) -> None:
            if confirmed:
                self._kill_service(message.key)

        self.push_screen(ConfirmKill(cfg.name), _after)

    def on_service_row_reload(self, message: ServiceRow.Reload) -> None:
        """Klick auf das ↻ einer gruenen (laufenden) ServiceRow -> Service
        beenden UND sofort wieder starten (stop+start). OHNE Ja/Nein-Overlay,
        anders als der reine Kill - der Service kommt ja gleich wieder hoch.
        _restart_service macht exakt das (Popen.stop -> kurze Pause -> start)."""
        self._restart_service(message.key, reason="Reload-Klick")

    def _kill_service(self, key: str) -> None:
        cfg = next((c for c in self._services_cfg if c.key == key), None)
        if cfg is None:
            return
        self._on_line(key, f"[Beende {cfg.name} via Klick...]")
        # SearXNG-Sonderfall: das Dashboard besitzt nur die 'docker logs -f'-Pipe,
        # nicht den Container. restart:unless-stopped wuerde ihn sofort wieder
        # hochziehen -> erst 'docker compose stop' im WSL-Container, DANN die Pipe.
        if key == "searx":
            try:
                subprocess.Popen(
                    ["wsl", "-d", "Ubuntu", "--", "bash", "-c",
                     "cd ~/searxng && docker compose stop"],
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except Exception as e:
                self._on_line(key, f"[SearXNG-Stop-Fehler: {e}]")
        # Owned Popen-Handle beenden (bei searx = die Log-Pipe).
        self._procs[key].stop(timeout=3.0)
        # Sofort optisch auf down; der Health-Tick bestaetigt es ohnehin gleich.
        self.services_panel.set_status(key, "down")

    def _restart_service(self, key: str, reason: str = "") -> None:
        cfg = next((c for c in self._services_cfg if c.key == key), None)
        if cfg is None:
            return
        proc = self._procs[key]
        tag = f"[Restart {cfg.name}" + (f" via {reason}" if reason else "") + "...]"
        self._on_line(key, tag)
        self.services_panel.set_status(key, "starting")
        proc.stop(timeout=3.0)
        time.sleep(0.3)
        proc.start()

    def action_toggle_avatar_style(self) -> None:
        cur = self.avatar_panel.avatar_style
        new = "halfblock" if cur == "ascii" else "ascii"
        # Wenn Half-Block angefragt aber Pillow fehlt: User informieren + abbrechen.
        if new == "halfblock" and not _HALFBLOCK_AVAILABLE:
            self._on_line("yuki", "[avatar] Half-Block braucht Pillow - bleibe bei ASCII")
            return
        self.avatar_panel.avatar_style = new
        self._on_line("yuki", f"[avatar-style -> {new}]")
        # Persistieren, damit der naechste Start die Wahl uebernimmt.
        settings = _load_dashboard_settings()
        settings["avatar_style"] = new
        _save_dashboard_settings(settings)

    def action_copy_log(self) -> None:
        # Kompletten Inhalt des aktiven Log-Tabs in die Zwischenablage (via OSC 52).
        # RichLog kann Textual-Maus-Selektion nicht (rendert ueber Strips, nicht als
        # Text/Content-Visual) - darum ganzen Tab am Stueck kopieren statt markieren.
        # self.lines haelt die gerenderten Strips; Strip.text gibt den Klartext zurueck.
        try:
            tabs = self.query_one("#right", TabbedContent)
            active = tabs.active or "tab-all"
            key = active.removeprefix("tab-")
            log = self.query_one(f"#log-{key}", RichLog)
            text = "\n".join(strip.text.rstrip() for strip in log.lines)
            self.copy_to_clipboard(text)
            self.notify(f"{len(log.lines)} Zeilen aus '{key}' kopiert", timeout=2)
        except Exception as exc:
            self.notify(f"Kopieren fehlgeschlagen: {exc}", severity="error", timeout=3)

    def action_clear_log(self) -> None:
        # Aktiven Tab leeren
        try:
            tabs = self.query_one("#right", TabbedContent)
            active = tabs.active or "tab-all"
            key = active.removeprefix("tab-")
            log = self.query_one(f"#log-{key}", RichLog)
            log.clear()
            # Yuki: auch den Sub-Filter-Buffer leeren, sonst holt ein Filter-
            # Wechsel die gerade geleerten Zeilen direkt wieder zurueck.
            if key == "yuki":
                self._yuki_log_buf.clear()
        except Exception:
            pass

    def action_switch_tab(self, key: str) -> None:
        try:
            tabs = self.query_one("#right", TabbedContent)
            tabs.active = f"tab-{key}"
        except Exception:
            pass

    # --- Yuki-Tab Sub-Filter ---

    def on_button_pressed(self, event: Button.Pressed) -> None:
        # Nur die Filter-Buttons der Yuki-Tab-Leiste; alles andere ignorieren.
        bid = event.button.id or ""
        if bid.startswith("yfilt-"):
            self._apply_yuki_filter(bid.removeprefix("yfilt-"))
            event.stop()

    def _apply_yuki_filter(self, filt: str) -> None:
        """Aktive Kategorie setzen + Yuki-RichLog aus dem Buffer neu rendern.
        Buffer bleibt unberuehrt -> verlustfreies Umschalten."""
        if filt not in {fk for fk, _ in YUKI_LOG_FILTERS}:
            return
        self._yuki_filter = filt
        try:
            log = self.query_one("#log-yuki", RichLog)
            log.clear()
            for cat, line in self._yuki_log_buf:
                if filt == "all" or filt == cat:
                    log.write(line)
        except Exception:
            pass
        # Aktiv-Markierung an den Buttons nachziehen.
        for fk, _ in YUKI_LOG_FILTERS:
            try:
                self.query_one(f"#yfilt-{fk}", Button).set_class(fk == filt, "-active")
            except Exception:
                pass

    def action_cycle_yuki_filter(self) -> None:
        # Erst auf den Yuki-Tab springen (Discovery), erst der naechste Druck cyclet.
        try:
            tabs = self.query_one("#right", TabbedContent)
            if (tabs.active or "") != "tab-yuki":
                tabs.active = "tab-yuki"
                return
            keys = [fk for fk, _ in YUKI_LOG_FILTERS]
            i = keys.index(self._yuki_filter) if self._yuki_filter in keys else 0
            self._apply_yuki_filter(keys[(i + 1) % len(keys)])
        except Exception:
            pass


def main() -> None:
    # cp1252-Konsolen-Bug entschaerfen (Stolperfalle 8 aus CLAUDE.md)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    import argparse
    ap = argparse.ArgumentParser(description="Yuki Dashboard (TUI)")
    ap.add_argument("--dry-run", action="store_true",
                    help="UI ohne Service-Start zeigen (Layout-Testing)")
    ap.add_argument("--avatar-style", choices=["ascii", "halfblock"], default=None,
                    help="Avatar-Stil: 'ascii' (animierte Katze) oder 'halfblock' "
                         "(persona-passendes Pixel-Background). Default: letzte Wahl "
                         "aus memory/yuki_dashboard.json oder 'ascii'.")
    args = ap.parse_args()
    YukiDashboard(dry_run=args.dry_run, avatar_style=args.avatar_style).run()


if __name__ == "__main__":
    main()
