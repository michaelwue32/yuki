r"""tools/benchmark_models.py  --  Modell-Benchmark gemma4:12b vs qwen3.6:27b

Read-only auf memory/, schreibt nur nach runtime/bench_<ts>/ und
archive/bench/<ts>/ (Sicherheits-Kopie). Monkey-patcht yc.chat_ollama lokal,
damit die ECHTEN Yuki-Helper (extract_habits/_facts/summarize_session/...)
verwendet werden -- gleiche Prompts wie im Live-Betrieb, vergleichbarer Output.

Aufrufe:
  .venv\Scripts\python.exe tools\benchmark_models.py
      --> Phase 1: 5090-Matrix (qwen3.6:27b vs gemma4:12b, je think on/off)
          alle 5 Tasks (habits/facts/consolidate/memory/reply)

  .venv\Scripts\python.exe tools\benchmark_models.py --bazzite
      --> Phase 2: gemma4:12b auf Bazzite, think on/off

  .venv\Scripts\python.exe tools\benchmark_models.py --skip-reply
      --> spart die ~6 Min Reply-Phase, wenn nur die Daten-Tasks interessieren

  .venv\Scripts\python.exe tools\benchmark_models.py --only habits,facts
      --> Komma-Liste der Phasen (habits|facts|consolidate|memory|reply)

Output: runtime/bench_<ts>/report.md (Tabelle + Volltext-Outputs nebeneinander)
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yuki_core as yc                  # noqa: E402
import yuki_history_db                  # noqa: E402


# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

# Format: (label, url, model, think)
CONFIGS_5090 = [
    ("5090 qwen3.6:27b no-think", "http://127.0.0.1:11434/api/chat", "qwen3.6:27b", False),
    ("5090 qwen3.6:27b think",    "http://127.0.0.1:11434/api/chat", "qwen3.6:27b", True),
    ("5090 gemma4:12b  no-think", "http://127.0.0.1:11434/api/chat", "gemma4:12b",  False),
    ("5090 gemma4:12b  think",    "http://127.0.0.1:11434/api/chat", "gemma4:12b",  True),
]

CONFIGS_BAZZITE = [
    ("bazzite gemma4:12b no-think", "http://127.0.0.1:11434/api/chat", "gemma4:12b", False),
    ("bazzite gemma4:12b think",    "http://127.0.0.1:11434/api/chat", "gemma4:12b", True),
]

CONFIGS_4070 = [
    ("4070 gemma4:12b no-think", "http://127.0.0.1:11434/api/chat", "gemma4:12b", False),
    ("4070 gemma4:12b think",    "http://127.0.0.1:11434/api/chat", "gemma4:12b", True),
]

# --gemma-qat (2026-07-05): loest gemma4:26b-a4b-it-qat gegen die aktuelle
# gemma4:26b-Decke ab? Beide auf der 5090. gemma4:26b = dense 25.8B Q4_K_M (18 GB
# disk, live geladen), die QAT-Variante = MoE mit ~4B aktiven Params (15.6 GB disk).
# Erwartung: kleinerer Footprint + deutlich schnellere Inferenz bei moeglichst
# gleicher Ausgabe-Qualitaet. Beide no-think = wie die gemma-Decke live faehrt.
# VRAM-Phase laeuft hier auch fuer die (remote) 5090 -- size_vram kommt ehrlich
# vom 5090-Ollama selbst (nvidia-smi wird fuer remote uebersprungen).
GEMMA_5090 = "http://127.0.0.1:11434/api/chat"
CONFIGS_GEMMA_QAT = [
    ("5090 gemma4:26b (dense, live)",        GEMMA_5090, "gemma4:26b",           False),
    ("5090 gemma4:26b-a4b-it-qat (MoE/QAT)", GEMMA_5090, "gemma4:26b-a4b-it-qat", False),
]

# --local-small (2026-06-13): Wie weit runter beim Notbetrieb-Tier (lokale 3060)?
# Alle Kandidaten laufen lokal auf der 3060 (= echtes Notbetrieb-Ziel), die
# gemma4:26b-"Decke" remote auf der 5090 als Qualitaets-Referenz ("die Stimme,
# die ich gewohnt bin"). Alle no-think — so faehrt der Notbetrieb live.
# Hinweis 12B-Schwelle: e4b=4 / e2b=2 / qwen=4|8 liegen ALLE unter der 12B-Grenze
# (yuki_core _strong_model_active / tools_active) -> selbes "Lite"-Regime wie
# heute qwen3:8b, keine Feature-Klippe zwischen den Kandidaten.
LOCAL_URL = "http://localhost:11434/api/chat"
CONFIGS_LOCAL_SMALL = [
    ("3060 gemma4:e4b",         LOCAL_URL, "gemma4:e4b", False),
    ("3060 gemma4:e2b",         LOCAL_URL, "gemma4:e2b", False),
    ("3060 qwen3.5:4b",         LOCAL_URL, "qwen3.5:4b", False),
    ("3060 qwen3:8b (Baseline)", LOCAL_URL, "qwen3:8b",  False),
    # Decke/Referenz — remote, NICHT in der VRAM-Phase (laeuft auf der 5090):
    ("5090 gemma4:26b (Decke)", "http://127.0.0.1:11434/api/chat", "gemma4:26b", False),
]
# EuroLLM-Wildcard wird erst angehaengt, wenn der Pull geklappt hat (siehe main()).
EUROLLM_CFG = ("3060 EuroLLM-9B (Wildcard)", LOCAL_URL, "eurollm:9b", False)

REPLY_RUNS_PER_CONFIG = 2     # Varianz; 2 statt 3 spart 33% Zeit
REPLY_CASES_TO_PICK = 3        # echte Michael-Turns aus History (versch. Personas)
HISTORY_CONTEXT_TURNS = 20     # wieviele Msgs vor dem Test-Turn als Kontext

# num_ctx-Override (2026-06-13): Live-chat_ollama setzt KEIN num_ctx und verlaesst
# sich auf Ollamas VRAM-Auto-Sizing. Auf der VRAM-knappen 3060 waehlt Ollama dann
# nur 4096 -> der ~8k-Token-Companion-Prompt wird abgeschnitten -> kleine Modelle
# liefern 1-3-Zeichen-Muell. Fuer eine FAIRE Reply-Qualitaetsmessung erzwingen wir
# hier ein grosses Fenster. None = Ollama-Auto (= reproduziert den Live-Bug).
BENCH_NUM_CTX = 12288


# ---------------------------------------------------------------------------
# Bench-Chat: HTTP-Wrapper, der yc.chat_ollama temporaer ersetzt
# ---------------------------------------------------------------------------

def make_bench_chat(url: str, model: str, think: bool, dump_dir: Path, call_log: list):
    """Liefert eine drop-in chat_ollama-Variante zum Monkey-Patchen.

    Wichtig: tools werden NICHT durchgereicht. Wir wollen den Vergleich rein
    auf Prompt-Verarbeitung, nicht Tool-Calling (das ist sowieso geparkt).
    """
    def bench_chat(messages, temperature=0.8, tools=None, purpose="misc"):  # noqa: ARG001
        opts = {"temperature": temperature}
        if BENCH_NUM_CTX:
            opts["num_ctx"] = BENCH_NUM_CTX
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "think": think,
            "options": opts,
        }
        t0 = time.time()
        err = None
        raw = ""
        try:
            resp = requests.post(url, json=payload, timeout=(5, 600))
            resp.raise_for_status()
            msg = resp.json().get("message", {}) or {}
            raw = msg.get("content", "") or ""
        except Exception as e:
            err = str(e)
        dt = time.time() - t0
        clean = yc._THINK_RE.sub("", raw).strip() if raw else ""
        call_log.append({
            "purpose": purpose,
            "elapsed": dt,
            "think": think,
            "model": model,
            "raw_chars": len(raw),
            "clean_chars": len(clean),
            "error": err,
            "preview": clean[:200].replace("\n", " "),
        })
        # Eigener Debug-Dump in cfg_dir -- runtime/last_llm_*.json (Live-Snapshot)
        # bleibt unangetastet, weil wir chat_ollama komplett ersetzt haben.
        try:
            fname = f"call_{int(time.time() * 1000)}_{purpose}.json"
            (dump_dir / fname).write_text(
                json.dumps({
                    "purpose": purpose,
                    "model": model,
                    "think": think,
                    "elapsed_sec": dt,
                    "error": err,
                    "payload": payload,
                    "raw_content": raw,
                    "clean_content": clean,
                }, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception:
            pass
        return clean
    return bench_chat


def with_chat_patched(bench_chat, fn):
    """Patcht yc.chat_ollama temporaer auf bench_chat, ruft fn() auf, restored."""
    orig = yc.chat_ollama
    yc.chat_ollama = bench_chat
    try:
        return fn()
    finally:
        yc.chat_ollama = orig


# ---------------------------------------------------------------------------
# Sicherheits-Kopien (Defense in Depth -- Skript ist eh read-only)
# ---------------------------------------------------------------------------

def backup_memory(target_dir: Path):
    target_dir.mkdir(parents=True, exist_ok=True)
    files = [
        "conversation.json", "yuki_facts.json", "yuki_episodes.json",
        "yuki_memory.json", "yuki_habits.sqlite", "yuki_history.sqlite",
        "yuki_heart.json",
    ]
    copied = []
    for f in files:
        src = yc.MEMORY_DIR / f
        if src.exists():
            shutil.copy2(src, target_dir / f)
            copied.append(f)
    return copied


# ---------------------------------------------------------------------------
# VRAM-Messung (2026-06-13) — wieviel frisst jeder Notbetrieb-Kandidat auf der
# 3060, und wieviel davon landet wirklich auf der GPU (Rest = CPU-Spill = lahm)?
# size_vram aus /api/ps ist die ehrliche "passt's"-Zahl, nvidia-smi der Cross-Check.
# ---------------------------------------------------------------------------

def _nvidia_free_mib() -> int | None:
    """Freier VRAM in MiB laut nvidia-smi (None wenn nicht verfuegbar)."""
    import subprocess
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            text=True, timeout=10)
        return int(out.strip().splitlines()[0].strip())
    except Exception:
        return None


def _ollama_base(url: str) -> str:
    """http://host:port/api/chat -> http://host:port"""
    return url.split("/api/")[0]


def _ollama_ps(base: str) -> list[dict]:
    try:
        r = requests.get(f"{base}/api/ps", timeout=10)
        r.raise_for_status()
        return r.json().get("models", []) or []
    except Exception:
        return []


def _load_model(base: str, model: str) -> str | None:
    """Laedt model in den Speicher (leerer /api/generate-Call). Fehler-String od. None."""
    try:
        r = requests.post(f"{base}/api/generate",
                          json={"model": model, "keep_alive": "5m"}, timeout=300)
        r.raise_for_status()
        return None
    except Exception as e:
        return str(e)


def _unload_model(base: str, model: str):
    try:
        requests.post(f"{base}/api/generate",
                      json={"model": model, "keep_alive": 0}, timeout=30)
    except Exception:
        pass


def measure_vram(local_configs) -> list[dict]:
    """Pro lokalem Modell: alle anderen entladen, frei-VRAM messen, laden,
    /api/ps + frei-VRAM erneut. Liefert Liste von Mess-Dicts."""
    print("== VRAM-Phase (lokale Kandidaten, isoliert geladen) ==")
    results = []
    # Erst alle Kandidaten-Modelle entladen (sauberer Startzustand)
    for label, url, model, _ in local_configs:
        _unload_model(_ollama_base(url), model)
    time.sleep(1.0)

    for label, url, model, _ in local_configs:
        base = _ollama_base(url)
        # nvidia-smi laeuft LOKAL auf diesem Rechner (3060) -- bei einer remoten
        # Config (z.B. 5090) wuerde es die falsche GPU messen. Dann nur /api/ps
        # (size/size_vram kommt ehrlich vom Ollama-Host selbst) verwenden.
        is_local = ("localhost" in url) or ("127.0.0.1" in url)
        free_before = _nvidia_free_mib() if is_local else None
        print(f"  [{model}] laden ...", end=" ", flush=True)
        err = _load_model(base, model)
        time.sleep(1.5)  # GPU-Allokation settlen lassen
        free_after = _nvidia_free_mib() if is_local else None
        ps = _ollama_ps(base)
        entry = next((m for m in ps if m.get("name") == model
                      or m.get("model") == model), None)
        size = entry.get("size") if entry else None
        size_vram = entry.get("size_vram") if entry else None
        proc = None
        if size and size_vram is not None:
            gpu_pct = round(100 * size_vram / size) if size else 0
            proc = f"{gpu_pct}% GPU / {100 - gpu_pct}% CPU"
        rec = {
            "label": label, "model": model, "error": err,
            "size_bytes": size, "size_vram_bytes": size_vram,
            "processor": proc,
            "nvidia_free_before_mib": free_before,
            "nvidia_free_after_mib": free_after,
            "nvidia_delta_mib": (free_before - free_after)
                                if (free_before is not None and free_after is not None) else None,
        }
        results.append(rec)
        if err:
            print(f"FEHLER: {err}")
        else:
            sz = f"{size / 1e9:.1f} GB" if size else "?"
            vr = f"{size_vram / 1e9:.1f} GB" if size_vram else "?"
            print(f"footprint {sz}, davon GPU {vr}  ({proc or '?'})")
        _unload_model(base, model)
        time.sleep(1.0)
    print()
    return results


# ---------------------------------------------------------------------------
# Test-Daten aus echten Yuki-Files ziehen
# ---------------------------------------------------------------------------

def get_session_for_tasks() -> list[dict]:
    """Letzte ~30 Turns aus conversation.json -- realistischer Input fuer
    habits/facts/memory_summary."""
    hist = yc.load_history()
    if not hist:
        return []
    return hist[-60:]


def get_facts_consolidate_case() -> tuple[str | None, list[str]]:
    """Sucht eine subject-Gruppe mit >=4 Eintraegen -- damit der LLM was zu
    mergen hat. Bevorzugt 'michael' oder 'yuki' (haben i.d.R. die meisten)."""
    facts = yc.load_facts()
    by_subj: dict[str, list[str]] = {}
    by_subj_orig: dict[str, str] = {}        # lower -> Original-Schreibweise
    for f in facts:
        s = (f.get("subject") or "").strip()
        text = (f.get("text") or "").strip()
        if not s or not text:
            continue
        key = s.lower()
        by_subj.setdefault(key, []).append(text)
        by_subj_orig.setdefault(key, s)
    # Sortiert nach Eintragsanzahl absteigend
    cands = sorted(by_subj.items(), key=lambda kv: -len(kv[1]))
    for key, texts in cands:
        if len(texts) >= 4:
            return by_subj_orig[key], texts
    return None, []


def get_reply_cases() -> list[dict]:
    """Holt N Michael-Turns aus yuki_history.sqlite, gespreizt ueber versch.
    Personas. Pro Case: history-Kontext (letzte HISTORY_CONTEXT_TURNS davor) +
    Original-Yuki-Reply zur Vergleichbarkeit."""
    all_msgs = yuki_history_db.get_messages(limit=3000)
    if not all_msgs:
        return []
    cases: list[dict] = []
    seen_personas: set[str] = set()
    # Von hinten (neueste zuerst) durchgehen
    for i in range(len(all_msgs) - 1, -1, -1):
        row = all_msgs[i]
        if row["speaker"] != "michael":
            continue
        content = (row.get("content") or "").strip()
        # Skip zu kurze (Acks) und Wahrnehmungs-Marker
        if len(content) < 15 or content.startswith("["):
            continue
        # Naechste yuki-Antwort im 5er-Fenster
        reply = None
        for j in range(i + 1, min(i + 5, len(all_msgs))):
            if all_msgs[j]["speaker"] == "yuki":
                reply = all_msgs[j]
                break
        if reply is None:
            continue
        persona = (row.get("persona") or "").strip().lower() or "smalltalk"
        # Persona-Spreizung: jede Persona max 1x
        if persona in seen_personas:
            continue
        # Persona muss im aktuellen PERSONAS-Dict existieren, sonst wird's beim
        # Reply-Bauen ein Fallback und der Vergleich verzerrt
        if persona not in yc.PERSONAS:
            continue
        seen_personas.add(persona)
        # Kontext vor dem Turn -> HISTORY-Format
        ctx_rows = all_msgs[max(0, i - HISTORY_CONTEXT_TURNS):i]
        history = []
        for m in ctx_rows:
            sp = m["speaker"]
            role = {"michael": "user", "yuki": "assistant"}.get(sp)
            if role is None:
                continue
            history.append({"role": role, "content": m.get("content", "")})
        # Frischen User-Turn anhaengen (generate_reply erwartet History mit
        # letzter User-Msg am Ende -- siehe yuki_core.generate_reply Docstring).
        history.append({"role": "user", "content": content})
        cases.append({
            "persona": persona,
            "mood": row.get("mood") or "",
            "user_msg": content,
            "original_reply": reply.get("content", ""),
            "original_ts": reply.get("ts", ""),
            "history": history,
        })
        if len(cases) >= REPLY_CASES_TO_PICK:
            break
    # Wenn wir nicht genug verschiedene Personas finden, mit Wiederholungen
    # auffuellen (kann passieren wenn History dominant von einer Persona ist)
    if not cases:
        return []
    return cases


# ---------------------------------------------------------------------------
# Task-Runner (1 LLM-Call pro Task, Yukis echte Helper)
# ---------------------------------------------------------------------------

def get_synthetic_jp_cases() -> list[dict]:
    """Hand-gebaute JP-Faelle, weil History kaum tutor und KEIN kyoto hergibt.
    Testet gezielt: kann das kleine Modell JP UND haelt es die Marker
    ([furigana:]/[vocab:] bei tutor, [de:]-Untertitel bei kyoto)?"""
    cases = []
    if "tutor" in yc.PERSONAS:
        cases.append({
            "persona": "tutor", "mood": "",
            "user_msg": "Wie sage ich 'Guten Morgen' auf Japanisch? "
                        "Und kannst du mir das Wort erklaeren?",
            "original_reply": "(synthetisch — keine History-Referenz)",
            "original_ts": "synthetic",
            "history": [
                {"role": "assistant", "content": "Klar, lass uns ein bisschen Japanisch ueben! Worauf hast du Lust?"},
                {"role": "user", "content": "Wie sage ich 'Guten Morgen' auf Japanisch? "
                                            "Und kannst du mir das Wort erklaeren?"},
            ],
        })
    if "kyoto" in yc.PERSONAS:
        cases.append({
            "persona": "kyoto", "mood": "",
            "user_msg": "おはよう！今日はいい天気だね。",
            "original_reply": "(synthetisch — keine History-Referenz)",
            "original_ts": "synthetic",
            "history": [
                {"role": "user", "content": "おはよう！今日はいい天気だね。"},
            ],
        })
    return cases


def run_reply_case(case: dict) -> str:
    persona = case["persona"]
    memory = yc.load_memory()
    sys_msg = yc.build_system_msg(memory, persona)
    fewshot = yc.persona_fewshot(persona)
    reminder = yc.persona_reminder(persona)
    return yc.generate_reply(case["history"], sys_msg,
                             fewshot=fewshot, reminder=reminder)


# ---------------------------------------------------------------------------
# Main-Bench
# ---------------------------------------------------------------------------

def run_bench(configs, with_reply: bool, phases: tuple[str, ...], vram_configs=None,
              extra_jp: bool = False):
    ts = time.strftime("%Y%m%d_%H%M%S")
    bench_root = ROOT / "runtime" / f"bench_{ts}"
    bench_root.mkdir(parents=True, exist_ok=True)
    archive_dir = ROOT / "archive" / "bench" / ts
    copied = backup_memory(archive_dir)
    print(f"  [Backup nach {archive_dir} -- {len(copied)} Dateien]")
    print(f"  [Output  nach {bench_root}]")
    print()

    # --- VRAM-Phase (explizit uebergebene Configs, lokal ODER remote) ---
    vram_results = []
    if vram_configs:
        vram_results = measure_vram(vram_configs)

    # --- Inputs einmal holen ---
    print("== Test-Daten laden ==")
    session = get_session_for_tasks() if any(p in phases for p in ("habits", "facts", "memory")) else []
    facts_old = yc.load_facts() if "facts" in phases else []
    subj, subj_texts = get_facts_consolidate_case() if "consolidate" in phases else (None, [])
    old_memory = yc.load_memory() if "memory" in phases else ""
    reply_cases = get_reply_cases() if with_reply else []
    if with_reply and extra_jp:
        reply_cases = reply_cases + get_synthetic_jp_cases()
    print(f"  Session-Turns:        {len(session)}")
    print(f"  Old-Facts geladen:    {len(facts_old)}")
    if subj:
        print(f"  Consolidate-Case:     subject='{subj}' mit {len(subj_texts)} Eintraegen")
    else:
        print("  Consolidate-Case:     (keine subject-Gruppe >=4 Eintraege gefunden)")
    print(f"  Reply-Cases:          {len(reply_cases)}")
    if reply_cases:
        for c in reply_cases:
            print(f"     - persona={c['persona']}, ts={c['original_ts']}, "
                  f"msg='{c['user_msg'][:60].replace(chr(10),' ')}...'")
    print()

    results: dict[str, dict] = {}

    for cfg in configs:
        label, url, model, think = cfg
        print(f"== Config: {label} ==")
        cfg_dir = bench_root / _safe_dir(label)
        cfg_dir.mkdir(parents=True, exist_ok=True)
        call_log: list[dict] = []
        bench_chat = make_bench_chat(url, model, think, cfg_dir, call_log)
        res: dict = {}

        if "habits" in phases and session:
            print("  [habits_extract]    ...", end=" ", flush=True)
            t0 = time.time()
            try:
                out = with_chat_patched(
                    bench_chat,
                    lambda: yc.extract_habits(session, known_keys=[]),
                )
            except Exception as e:
                out = []
                print(f"FEHLER: {e}")
            dt = time.time() - t0
            res["habits"] = {"elapsed": dt, "parsed": out, "count": len(out)}
            print(f"{dt:6.1f}s -> {len(out)} Occurrences")

        if "facts" in phases and session:
            print("  [facts_extract]     ...", end=" ", flush=True)
            t0 = time.time()
            try:
                out = with_chat_patched(
                    bench_chat,
                    lambda: yc.extract_facts(facts_old, session),
                )
            except Exception as e:
                out = []
                print(f"FEHLER: {e}")
            dt = time.time() - t0
            res["facts"] = {"elapsed": dt, "parsed": out, "count": len(out)}
            print(f"{dt:6.1f}s -> {len(out)} neue Fakten")

        if "consolidate" in phases and subj and subj_texts:
            print("  [facts_consolidate] ...", end=" ", flush=True)
            t0 = time.time()
            try:
                out = with_chat_patched(
                    bench_chat,
                    lambda: yc._consolidate_subject(subj, list(subj_texts)),
                )
            except Exception as e:
                out = list(subj_texts)
                print(f"FEHLER: {e}")
            dt = time.time() - t0
            res["consolidate"] = {
                "elapsed": dt,
                "subject": subj,
                "input": list(subj_texts),
                "input_n": len(subj_texts),
                "output": out,
                "output_n": len(out),
            }
            print(f"{dt:6.1f}s -> {len(subj_texts)} in -> {len(out)} out")

        if "memory" in phases and session:
            print("  [memory_summary]    ...", end=" ", flush=True)
            t0 = time.time()
            try:
                out = with_chat_patched(
                    bench_chat,
                    lambda: yc.summarize_session(old_memory, session),
                )
            except Exception as e:
                out = ""
                print(f"FEHLER: {e}")
            dt = time.time() - t0
            res["memory"] = {"elapsed": dt, "summary": out, "chars": len(out)}
            print(f"{dt:6.1f}s -> {len(out)} Zeichen")

        if with_reply and reply_cases:
            res["reply"] = []
            for idx, case in enumerate(reply_cases):
                runs = []
                for r in range(REPLY_RUNS_PER_CONFIG):
                    print(f"  [reply #{idx + 1}/{len(reply_cases)} run "
                          f"{r + 1}/{REPLY_RUNS_PER_CONFIG}] ...",
                          end=" ", flush=True)
                    t0 = time.time()
                    try:
                        out = with_chat_patched(
                            bench_chat,
                            lambda c=case: run_reply_case(c),
                        )
                    except Exception as e:
                        out = f"[FEHLER: {e}]"
                    dt = time.time() - t0
                    runs.append({"elapsed": dt, "reply": out, "chars": len(out)})
                    print(f"{dt:6.1f}s -> {len(out)} Zeichen")
                res["reply"].append({
                    "persona": case["persona"],
                    "user_msg": case["user_msg"],
                    "original_reply": case["original_reply"],
                    "original_ts": case["original_ts"],
                    "runs": runs,
                })

        (cfg_dir / "call_log.json").write_text(
            json.dumps(call_log, ensure_ascii=False, indent=2),
            encoding="utf-8")
        results[label] = res
        print()

    # --- Report ---
    report = build_report(results, configs, reply_cases, phases, with_reply, vram_results)
    report_path = bench_root / "report.md"
    report_path.write_text(report, encoding="utf-8")
    raw_path = bench_root / "results.json"
    raw_path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    print(f"== Report: {report_path}")
    print(f"== Raw:    {raw_path}")


def _safe_dir(label: str) -> str:
    return (label.replace("@", "_at_")
                 .replace(" ", "_")
                 .replace(":", "-")
                 .replace("/", "-"))


# ---------------------------------------------------------------------------
# Markdown-Report
# ---------------------------------------------------------------------------

def _md_blockquote(text: str, max_chars: int = 2000) -> str:
    if not text:
        return "_(leer)_"
    snippet = text[:max_chars]
    return "\n".join("> " + line for line in snippet.splitlines())


def build_report(results, configs, reply_cases, phases, with_reply, vram_results=None) -> str:
    lines: list[str] = []
    lines.append(f"# Yuki Model-Benchmark — {time.strftime('%Y-%m-%d %H:%M')}")
    lines.append("")
    lines.append("**Configs:**")
    lines.append("")
    for label, url, model, think in configs:
        lines.append(f"- `{label}` — `{url}` / model=`{model}` / think={'on' if think else 'off'}")
    lines.append("")

    # --- VRAM-Uebersicht ---
    if vram_results:
        lines.append("## VRAM-Footprint (lokale 3060, je Modell isoliert geladen)")
        lines.append("")
        lines.append("_`Footprint` = Gesamt-Speicher des Modells (`/api/ps` size). "
                     "`davon GPU` = `size_vram`; Rest spillt auf CPU (= lahm). "
                     "`nvidia frei davor` zeigt, wieviel die laufenden Yuki-Dienste "
                     "(Whisper/F5/Vision/Browser) schon belegen — das ist die "
                     "realistische Notbetrieb-Enge._")
        lines.append("")
        lines.append("| Modell | Footprint | davon GPU | Split | nvidia frei (davor→danach) |")
        lines.append("| --- | ---: | ---: | --- | --- |")
        for r in vram_results:
            if r.get("error"):
                lines.append(f"| `{r['model']}` | — | — | FEHLER | {r['error'][:40]} |")
                continue
            sz = f"{r['size_bytes'] / 1e9:.1f} GB" if r.get("size_bytes") else "?"
            vr = f"{r['size_vram_bytes'] / 1e9:.1f} GB" if r.get("size_vram_bytes") else "?"
            fb = r.get("nvidia_free_before_mib")
            fa = r.get("nvidia_free_after_mib")
            nv = f"{fb}→{fa} MiB" if (fb is not None and fa is not None) else "?"
            lines.append(f"| `{r['model']}` | {sz} | {vr} | {r.get('processor') or '?'} | {nv} |")
        lines.append("")

    # --- Latenz-Tabelle ---
    tasks_order = [t for t in ("habits", "facts", "consolidate", "memory") if t in phases]
    if with_reply:
        tasks_order.append("reply")
    lines.append("## Latenz-Uebersicht (Wall-Time pro Task)")
    lines.append("")
    header = "| Config | " + " | ".join(tasks_order) + " |"
    sep = "| --- | " + " | ".join("---:" for _ in tasks_order) + " |"
    lines.append(header)
    lines.append(sep)
    for label, _, _, _ in configs:
        row = [label]
        r = results.get(label, {})
        for t in tasks_order:
            entry = r.get(t)
            if entry is None:
                row.append("—")
            elif t == "reply" and isinstance(entry, list):
                runs = [run["elapsed"] for case in entry for run in case["runs"]]
                if runs:
                    avg = sum(runs) / len(runs)
                    lo, hi = min(runs), max(runs)
                    row.append(f"Ø{avg:.1f}s ({lo:.1f}–{hi:.1f})")
                else:
                    row.append("—")
            else:
                row.append(f"{entry['elapsed']:.1f}s")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    # --- Habits ---
    if "habits" in phases:
        lines.append("## Habits-Extraction (Qualitaet)")
        lines.append("")
        lines.append("_Erwartet: snake_case habit_keys, subject michael|yuki, "
                     "1 Vorkommen pro Zeile. Duplikate / Plaene-ohne-Vollzug "
                     "sind Bugs._")
        lines.append("")
        for label, _, _, _ in configs:
            r = results.get(label, {}).get("habits")
            if not r:
                continue
            lines.append(f"### {label}")
            lines.append(f"_{r['elapsed']:.1f}s — {r['count']} Occurrences_")
            lines.append("")
            if r["parsed"]:
                for h in r["parsed"]:
                    lines.append(f"- `{h.get('habit_key', '?')}` | "
                                 f"{h.get('subject', '?')} | "
                                 f"{h.get('date', '?')} — "
                                 f"{h.get('context', '')}")
            else:
                lines.append("_(leer / NONE)_")
            lines.append("")

    # --- Facts ---
    if "facts" in phases:
        lines.append("## Facts-Extraction (Qualitaet)")
        lines.append("")
        lines.append("_Erwartet: 'subject | fact' pro Zeile, max ~5 Worte, EN, "
                     "keine Stimmungen/Tagesereignisse. NONE wenn nichts Neues._")
        lines.append("")
        for label, _, _, _ in configs:
            r = results.get(label, {}).get("facts")
            if not r:
                continue
            lines.append(f"### {label}")
            lines.append(f"_{r['elapsed']:.1f}s — {r['count']} neue Fakten_")
            lines.append("")
            if r["parsed"]:
                for f in r["parsed"]:
                    lines.append(f"- {f.get('subject', '?')}: {f.get('text', '?')}")
            else:
                lines.append("_(leer / NONE)_")
            lines.append("")

    # --- Consolidate ---
    if "consolidate" in phases:
        lines.append("## Facts-Consolidate (Qualitaet)")
        lines.append("")
        any_r = next((results[lbl].get("consolidate") for lbl, *_ in configs
                      if results.get(lbl, {}).get("consolidate")), None)
        if any_r:
            lines.append(f"**Subject:** `{any_r['subject']}`  "
                         f"**Input ({any_r['input_n']} Eintraege):**")
            lines.append("")
            for txt in any_r.get("input", []) or []:
                lines.append(f"- {txt}")
            lines.append("")
        lines.append("_Erwartet: aggressives Mergen von Variationen, KEIN Datenverlust "
                     "bei distinkten Eintraegen (Eigennamen, spezifische Traits)._")
        lines.append("")
        for label, _, _, _ in configs:
            r = results.get(label, {}).get("consolidate")
            if not r:
                continue
            lines.append(f"### {label}")
            lines.append(f"_{r['elapsed']:.1f}s — {r['input_n']} in → {r['output_n']} out_")
            lines.append("")
            for txt in r["output"]:
                lines.append(f"- {txt}")
            lines.append("")

    # --- Memory ---
    if "memory" in phases:
        lines.append("## Memory-Summary (Qualitaet)")
        lines.append("")
        lines.append("_Erwartet: kompakte 3rd-person-Notizen ueber Michael, max ~120 Worte, "
                     "EN, keine japanische Schrift, keine Persona-Replys._")
        lines.append("")
        for label, _, _, _ in configs:
            r = results.get(label, {}).get("memory")
            if not r:
                continue
            lines.append(f"### {label}")
            lines.append(f"_{r['elapsed']:.1f}s — {r['chars']} Zeichen_")
            lines.append("")
            lines.append(_md_blockquote(r["summary"], 3000))
            lines.append("")

    # --- Reply ---
    if with_reply and reply_cases:
        lines.append("## Reply-Latenz + Qualitaet")
        lines.append("")
        lines.append("_Vergleich gegen die historische Original-Antwort. Latenz ist der "
                     "Hauptfokus; Stimme/Stil sekundaer (durch Auge)._")
        lines.append("")
        for idx, case in enumerate(reply_cases):
            lines.append(f"### Case #{idx + 1} — persona=`{case['persona']}`  "
                         f"(Original ts: {case['original_ts']})")
            lines.append("")
            lines.append("**Michael fragte/sagte:**")
            lines.append("")
            lines.append(_md_blockquote(case["user_msg"], 800))
            lines.append("")
            lines.append("**Original-Yuki (Referenz, war damals OK):**")
            lines.append("")
            lines.append(_md_blockquote(case["original_reply"], 1500))
            lines.append("")
            lines.append("---")
            lines.append("")
            for label, _, _, _ in configs:
                r = results.get(label, {}).get("reply")
                if not r or len(r) <= idx:
                    continue
                case_data = r[idx]
                runs = case_data["runs"]
                if not runs:
                    continue
                avg = sum(run["elapsed"] for run in runs) / len(runs)
                lines.append(f"**{label}** — Ø {avg:.1f}s über {len(runs)} Runs")
                lines.append("")
                for ri, run in enumerate(runs):
                    lines.append(f"_Run {ri + 1} ({run['elapsed']:.1f}s, "
                                 f"{run['chars']} Zeichen):_")
                    lines.append("")
                    lines.append(_md_blockquote(run["reply"], 1500))
                    lines.append("")
            lines.append("---")
            lines.append("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

ALL_PHASES = ("habits", "facts", "consolidate", "memory", "reply")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bazzite", action="store_true",
                    help="Nur Bazzite-Configs laufen lassen (gemma4:12b auf .102)")
    ap.add_argument("--4070", dest="four070", action="store_true",
                    help="Nur 4070-Configs laufen lassen (gemma4:12b auf .10)")
    ap.add_argument("--gemma-qat", dest="gemma_qat", action="store_true",
                    help="A/B: gemma4:26b (dense, live) vs gemma4:26b-a4b-it-qat "
                         "(MoE/QAT) auf der 5090; inkl. VRAM-Phase")
    ap.add_argument("--also-bazzite", action="store_true",
                    help="5090-Matrix UND Bazzite an einem Stueck")
    ap.add_argument("--local-small", action="store_true",
                    help="Notbetrieb-Tier: kleine lokale Modelle (3060) + gemma4:26b-Decke; "
                         "inkl. VRAM-Phase")
    ap.add_argument("--with-eurollm", action="store_true",
                    help="EuroLLM-9B-Wildcard mit in --local-small aufnehmen")
    ap.add_argument("--no-vram", action="store_true",
                    help="VRAM-Phase auslassen (nur bei --local-small relevant)")
    ap.add_argument("--skip-reply", action="store_true",
                    help="Reply-Phase auslassen (spart ~6 Min)")
    ap.add_argument("--only", default="",
                    help="Komma-Liste: habits,facts,consolidate,memory,reply (Default: alle)")
    args = ap.parse_args()

    vram_configs = None
    if args.local_small:
        configs = list(CONFIGS_LOCAL_SMALL)
        if args.with_eurollm:
            # EuroLLM vor die Decke einsortieren (lokaler Kandidat)
            configs.insert(-1, EUROLLM_CFG)
        # Nur die lokalen 3060-Kandidaten in die VRAM-Phase (die 5090-Decke bleibt
        # bewusst draussen -- sonst wuerde sie beim local-small-Lauf ent-/neuladen).
        if not args.no_vram:
            vram_configs = [c for c in configs
                            if "localhost" in c[1] or "127.0.0.1" in c[1]]
    elif args.gemma_qat:
        configs = CONFIGS_GEMMA_QAT
        if not args.no_vram:
            vram_configs = list(configs)   # beide 5090-Modelle isoliert messen
    elif args.bazzite:
        configs = CONFIGS_BAZZITE
    elif args.four070:
        configs = CONFIGS_4070
    elif args.also_bazzite:
        configs = CONFIGS_5090 + CONFIGS_BAZZITE
    else:
        configs = CONFIGS_5090

    if args.only:
        requested = tuple(p.strip().lower() for p in args.only.split(",") if p.strip())
        invalid = [p for p in requested if p not in ALL_PHASES]
        if invalid:
            print(f"  [Unbekannte Phasen: {invalid}; gueltig: {ALL_PHASES}]")
            sys.exit(2)
        phases = tuple(p for p in requested if p != "reply")
        with_reply = "reply" in requested
    else:
        phases = ("habits", "facts", "consolidate", "memory")
        with_reply = not args.skip_reply

    print()
    print("=" * 70)
    print(f"  Yuki Model-Benchmark  ({time.strftime('%Y-%m-%d %H:%M')})")
    print("=" * 70)
    print(f"  Configs ({len(configs)}):")
    for label, url, model, think in configs:
        print(f"    - {label}  ({url}  model={model}  think={'on' if think else 'off'})")
    print(f"  Phasen:  {list(phases) + (['reply'] if with_reply else [])}")
    print("=" * 70)
    print()

    run_bench(configs, with_reply=with_reply, phases=phases, vram_configs=vram_configs,
              extra_jp=args.local_small)


if __name__ == "__main__":
    main()
