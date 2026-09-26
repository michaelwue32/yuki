r"""tools/benchmark_tool_calling.py  --  Research-Tool-Calling Bench

Prueft, ob ein Modell die 7 RESEARCH_TOOLS_SPEC-Werkzeuge korrekt aufruft:
  - korrekter Tool-Name
  - korrekte Argumente (required Keys)
  - kein Tool wenn keins gebraucht wird (Smalltalk-Trap)
  - Mehrfach-Tool-Sequenz wenn 2 Sachen gefragt sind

Tools werden NICHT wirklich ausgefuehrt -- wir schicken Dummy-Results
zurueck und schauen, wie das Modell weitermacht.

Aufrufe:
  .venv\Scripts\python.exe tools\benchmark_tool_calling.py
      --> default 3 Configs: qwen3:14b@4070, gemma4:12b@4070, gemma4:12b@Bazzite

Output: runtime/bench_tools_<ts>/report.md
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yuki_core as yc                                                 # noqa: E402


# ---------------------------------------------------------------------------
# Configs + Test-Cases
# ---------------------------------------------------------------------------

# (label, url, model)
CONFIGS = [
    ("qwen3:14b @ 4070",     "http://127.0.0.1:11434/api/chat",  "qwen3:14b"),
    ("gemma4:12b @ 4070",    "http://127.0.0.1:11434/api/chat",  "gemma4:12b"),
    ("gemma4:12b @ Bazzite", "http://127.0.0.1:11434/api/chat", "gemma4:12b"),
]

# Erwartung:
#   expected_tool: Name des Tools das gerufen werden SOLL (oder None = "kein Tool")
#   alt_tools:     andere Tools die auch akzeptabel sind
#   arg_keywords:  Strings die in den Args vorkommen muessen (case-insensitive)
#   multi:         True wenn mehrere Tool-Calls erwartet werden (Liste expected_tools)
TEST_CASES = [
    {
        "id": 1,
        "label": "Wetter (weather_by_place)",
        "prompt": "Wie ist das Wetter morgen in Hamburg?",
        "expected_tool": "weather_by_place",
        "alt_tools": [],
        "arg_keywords": ["hamburg"],
    },
    {
        "id": 2,
        "label": "Wikipedia (wiki_summary)",
        "prompt": "Was sagt Wikipedia ueber Murakami Haruki?",
        "expected_tool": "wiki_summary",
        "alt_tools": ["web_search"],
        "arg_keywords": ["murakami"],
    },
    {
        "id": 3,
        "label": "Kalender (calendar_query)",
        "prompt": "Was steht morgen in meinem Kalender?",
        "expected_tool": "calendar_query",
        "alt_tools": [],
        "arg_keywords": [],   # date-range ist ok offen
    },
    {
        "id": 4,
        "label": "Wadoku (lookup_word)",
        "prompt": "Was heisst 図書館 auf Deutsch?",
        "expected_tool": "lookup_word",
        "alt_tools": [],
        "arg_keywords": ["図書館"],
    },
    {
        "id": 5,
        "label": "News (news_headlines / web_search)",
        "prompt": "Was sind die neuesten Nachrichten zu KI?",
        "expected_tool": "news_headlines",
        "alt_tools": ["web_search"],
        "arg_keywords": [],
    },
    {
        "id": 6,
        "label": "Smalltalk-Trap (KEIN Tool)",
        "prompt": "Hallo, wie geht's dir heute?",
        "expected_tool": None,     # kein Tool!
        "alt_tools": [],
        "arg_keywords": [],
    },
    {
        "id": 7,
        "label": "Multi-Tool (weather + calendar)",
        "prompt": "Wie ist das Wetter heute in Berlin und was steht heute in meinem Kalender?",
        "expected_tool": "weather_by_place",
        "alt_tools": [],
        "arg_keywords": ["berlin"],
        "multi_expected": ["weather_by_place", "calendar_query"],
    },
]

MAX_ROUNDS = 3        # Round 0 = initial, Round 1+ = nach Tool-Result(s)
TIMEOUT = (5, 300)


# ---------------------------------------------------------------------------
# Direct HTTP-Call mit Tool-Use (ohne yc.chat_ollama, weil wir den Server
# explizit waehlen und nichts cachen wollen)
# ---------------------------------------------------------------------------

def chat_with_tools(url: str, model: str, messages: list, tools: list,
                    think: bool = True) -> tuple[dict, float, str | None]:
    """Single round chat with tools. Returns (message_dict, elapsed_sec, err)."""
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": think,
        "options": {"temperature": 0.2},
        "tools": tools,
    }
    t0 = time.time()
    try:
        resp = requests.post(url, json=payload, timeout=TIMEOUT)
        resp.raise_for_status()
    except Exception as e:
        return ({}, time.time() - t0, str(e))
    return (resp.json().get("message", {}) or {}, time.time() - t0, None)


def dummy_tool_result(tool_name: str, args: dict) -> str:
    """Generic plausible result, damit das Modell sinnvoll weitermachen kann."""
    if tool_name == "weather_by_place":
        place = (args or {}).get("place", "unknown")
        return f"Weather for {place}: 18C, partly cloudy, light wind."
    if tool_name == "wiki_summary":
        topic = (args or {}).get("query") or (args or {}).get("topic") or "unknown"
        return f"Wikipedia summary for '{topic}': a brief 2-sentence factual summary."
    if tool_name == "calendar_query":
        return ("Today: 18:00 Summer Games Fest. Tomorrow: 10:00 dentist appointment.")
    if tool_name == "lookup_word":
        q = (args or {}).get("query", "?")
        return f"{q}: Bibliothek, library (n.)"
    if tool_name == "news_headlines":
        return ("Top headlines: 1) New AI model released. 2) Climate summit ends. "
                "3) Local sports victory.")
    if tool_name == "web_search":
        return "Top result: A snippet about the queried topic with a URL."
    if tool_name == "fetch_url":
        return "Page content: Title and 200 words of body text."
    return f"{tool_name}: ok"


def run_case_on_config(url: str, model: str, system_msg: str, case: dict,
                       tools: list, dump_dir: Path):
    """Eine Test-Case-Runde durchspielen. Liefert dict mit:
      tool_calls_round0: [{name, args}, ...]
      rounds: [{tool_calls, elapsed, content}]
      total_elapsed
      final_content
      errors
    """
    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user",   "content": case["prompt"]},
    ]
    rounds: list[dict] = []
    total_elapsed = 0.0
    errors: list[str] = []
    final_content = ""

    for r in range(MAX_ROUNDS):
        # Round 0 with think on (Yuki's normal pattern). Round 1+ ohne think -- nur antworten.
        think_now = (r == 0)
        msg, dt, err = chat_with_tools(url, model, messages, tools, think=think_now)
        total_elapsed += dt
        if err:
            errors.append(f"round {r}: {err}")
            break

        tool_calls = msg.get("tool_calls") or []
        content_raw = msg.get("content", "") or ""
        content_clean = yc._THINK_RE.sub("", content_raw).strip()

        # Normalize tool_calls to list-of-{name,args}
        norm_calls = []
        for tc in tool_calls:
            fn = (tc.get("function") or {})
            name = fn.get("name") or ""
            raw_args = fn.get("arguments")
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args)
                except Exception:
                    args = {"_raw": raw_args}
            elif isinstance(raw_args, dict):
                args = raw_args
            else:
                args = {}
            norm_calls.append({"name": name, "args": args})

        rounds.append({
            "round": r,
            "elapsed": dt,
            "tool_calls": norm_calls,
            "content_clean": content_clean,
            "raw_chars": len(content_raw),
        })

        # Stop conditions
        if not norm_calls:
            final_content = content_clean
            break

        # Append assistant message + tool-results, dann naechste Runde
        messages.append(msg)
        for tc_norm in norm_calls:
            tool_res = dummy_tool_result(tc_norm["name"], tc_norm["args"])
            messages.append({
                "role": "tool",
                "name": tc_norm["name"],
                "content": tool_res,
            })

    result = {
        "case_id": case["id"],
        "prompt": case["prompt"],
        "rounds": rounds,
        "total_elapsed": total_elapsed,
        "final_content": final_content,
        "errors": errors,
    }
    try:
        (dump_dir / f"case_{case['id']:02d}.json").write_text(
            json.dumps({"case": case, "messages_final": messages, "result": result},
                       ensure_ascii=False, indent=2),
            encoding="utf-8")
    except Exception:
        pass
    return result


# ---------------------------------------------------------------------------
# Auswertung pro Case
# ---------------------------------------------------------------------------

def score_case(case: dict, result: dict) -> tuple[str, str]:
    """Liefert (status, reason_string).  status in {'PASS','PARTIAL','FAIL','ERROR'}."""
    if result["errors"]:
        return ("ERROR", "; ".join(result["errors"]))

    rounds = result["rounds"]
    if not rounds:
        return ("ERROR", "keine Runden")

    all_calls = []
    for r in rounds:
        all_calls.extend(r["tool_calls"])

    expected_tool = case.get("expected_tool")
    alt_tools = set((case.get("alt_tools") or []))
    arg_keywords = [k.lower() for k in (case.get("arg_keywords") or [])]
    multi_expected = case.get("multi_expected")

    # --- Case ohne Tool-Erwartung (Smalltalk-Trap) ---
    if expected_tool is None:
        if not all_calls:
            return ("PASS", "korrekt kein Tool aufgerufen")
        return ("FAIL", f"unerwarteter Tool-Call: {all_calls[0]['name']}")

    # --- Multi-Tool-Case ---
    if multi_expected:
        called_names = [c["name"] for c in all_calls]
        missing = [n for n in multi_expected if n not in called_names]
        if not missing:
            return ("PASS", f"alle erwarteten Tools gerufen: {multi_expected}")
        if any(n in called_names for n in multi_expected):
            return ("PARTIAL", f"nur teilweise: gerufen={called_names}, fehlt={missing}")
        return ("FAIL", f"keiner der erwarteten Tools gerufen (got {called_names})")

    # --- Single-Tool-Case ---
    if not all_calls:
        return ("FAIL", f"kein Tool gerufen, erwartet '{expected_tool}'")

    first = all_calls[0]
    called_name = first["name"]
    args = first.get("args") or {}

    name_ok = (called_name == expected_tool) or (called_name in alt_tools)
    if not name_ok:
        return ("FAIL", f"falscher Tool-Name: '{called_name}' (erwartet '{expected_tool}')")

    # Args-Keywords pruefen (alle muessen in den arg-values vorkommen)
    if arg_keywords:
        args_blob = json.dumps(args, ensure_ascii=False).lower()
        missing = [k for k in arg_keywords if k not in args_blob]
        if missing:
            return ("PARTIAL", f"Tool ok ({called_name}), aber Args ohne {missing}: {args}")

    used_alt = called_name in alt_tools and called_name != expected_tool
    note = f" (alt-Tool {called_name} statt {expected_tool})" if used_alt else ""
    return ("PASS", f"Tool ok ({called_name}){note}, Args ok ({args})")


# ---------------------------------------------------------------------------
# Markdown-Report
# ---------------------------------------------------------------------------

STATUS_EMOJI = {"PASS": "OK", "PARTIAL": "~", "FAIL": "X", "ERROR": "!"}


def build_report(all_results, configs) -> str:
    lines: list[str] = []
    lines.append(f"# Yuki Tool-Calling Bench — {time.strftime('%Y-%m-%d %H:%M')}")
    lines.append("")
    lines.append("**Configs:**")
    lines.append("")
    for label, url, model in configs:
        lines.append(f"- `{label}` — `{url}` / model=`{model}`")
    lines.append("")
    lines.append("**Tools im Test (RESEARCH_TOOLS_SPEC):**")
    lines.append("")
    for spec in yc.RESEARCH_TOOLS_SPEC:
        fn = spec.get("function", {})
        lines.append(f"- `{fn.get('name')}`")
    lines.append("")

    # --- Summary-Tabelle ---
    lines.append("## Summary (PASS / PARTIAL=~ / FAIL=X / ERROR=!)")
    lines.append("")
    case_headers = " | ".join(f"#{c['id']}" for c in TEST_CASES)
    lines.append(f"| Config | {case_headers} | Score | Ø Latenz |")
    lines.append("| --- | " + " | ".join(":---:" for _ in TEST_CASES) + " | ---: | ---: |")
    for label, _, _ in configs:
        cells = []
        passes = 0
        partials = 0
        latencies = []
        for case in TEST_CASES:
            r = all_results.get((label, case["id"]))
            if not r:
                cells.append("?")
                continue
            status, _ = score_case(case, r)
            cells.append(STATUS_EMOJI.get(status, "?"))
            if status == "PASS":
                passes += 1
            elif status == "PARTIAL":
                partials += 1
            latencies.append(r["total_elapsed"])
        score = f"{passes}P+{partials}~/{len(TEST_CASES)}"
        avg_lat = sum(latencies) / len(latencies) if latencies else 0
        lines.append(f"| {label} | " + " | ".join(cells) + f" | {score} | {avg_lat:.1f}s |")
    lines.append("")

    # --- Per-Case-Details ---
    for case in TEST_CASES:
        lines.append(f"## Case #{case['id']} — {case['label']}")
        lines.append("")
        lines.append(f"**Prompt:** _{case['prompt']}_")
        lines.append("")
        expected = case.get("expected_tool")
        if case.get("multi_expected"):
            lines.append(f"**Erwartung:** Multi-Tool {case['multi_expected']}")
        elif expected is None:
            lines.append("**Erwartung:** KEIN Tool (Smalltalk)")
        else:
            alt = f" oder {case.get('alt_tools')}" if case.get("alt_tools") else ""
            lines.append(f"**Erwartung:** `{expected}`{alt}, Args enthalten {case.get('arg_keywords') or '(beliebig)'}")
        lines.append("")
        for label, _, _ in configs:
            r = all_results.get((label, case["id"]))
            if not r:
                continue
            status, reason = score_case(case, r)
            emoji = STATUS_EMOJI.get(status, "?")
            lines.append(f"### {emoji} {label} ({status}, {r['total_elapsed']:.1f}s)")
            lines.append("")
            lines.append(f"_{reason}_")
            lines.append("")
            for rd in r["rounds"]:
                tc_str = ", ".join(f"{c['name']}({json.dumps(c['args'], ensure_ascii=False)})"
                                   for c in rd["tool_calls"]) or "—"
                lines.append(f"- Round {rd['round']} ({rd['elapsed']:.1f}s): "
                             f"tool_calls=[{tc_str}]")
                if rd["content_clean"]:
                    snippet = rd["content_clean"][:200].replace("\n", " ")
                    lines.append(f"  - content: `{snippet}`")
            if r["final_content"]:
                lines.append("")
                lines.append("**Final-Antwort:**")
                lines.append("")
                lines.append("> " + r["final_content"][:600].replace("\n", "\n> "))
            if r["errors"]:
                lines.append("")
                lines.append(f"**Errors:** {r['errors']}")
            lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", action="append", default=None,
                    help="Nur diese Config-Labels (kann mehrfach). Default: alle.")
    ap.add_argument("--case", action="append", type=int, default=None,
                    help="Nur diese Case-IDs (kann mehrfach). Default: alle.")
    args = ap.parse_args()

    configs = CONFIGS
    if args.config:
        wanted = set(args.config)
        configs = [c for c in CONFIGS if c[0] in wanted]
        if not configs:
            print(f"[!] Keine Config-Labels matched. Verfuegbar: {[c[0] for c in CONFIGS]}")
            sys.exit(2)

    cases = TEST_CASES
    if args.case:
        wanted_ids = set(args.case)
        cases = [c for c in TEST_CASES if c["id"] in wanted_ids]

    ts = time.strftime("%Y%m%d_%H%M%S")
    bench_root = ROOT / "runtime" / f"bench_tools_{ts}"
    bench_root.mkdir(parents=True, exist_ok=True)

    system_msg = yc.build_research_system_msg(persona_before="smalltalk")
    tools = yc.RESEARCH_TOOLS_SPEC

    print()
    print("=" * 70)
    print(f"  Yuki Tool-Calling Bench  ({time.strftime('%Y-%m-%d %H:%M')})")
    print("=" * 70)
    print(f"  Configs:  {len(configs)}")
    for label, url, model in configs:
        print(f"    - {label}  (url={url}  model={model})")
    print(f"  Cases:    {len(cases)}")
    print(f"  Tools:    {len(tools)}")
    print(f"  Output:   {bench_root}")
    print("=" * 70)
    print()

    all_results: dict[tuple, dict] = {}

    for label, url, model in configs:
        print(f"== {label} ==")
        cfg_dir = bench_root / label.replace(" ", "_").replace("@", "at").replace(":", "-").replace("/", "-")
        cfg_dir.mkdir(parents=True, exist_ok=True)
        for case in cases:
            print(f"  Case #{case['id']} ({case['label']}) ...", end=" ", flush=True)
            t0 = time.time()
            result = run_case_on_config(url, model, system_msg, case, tools, cfg_dir)
            status, reason = score_case(case, result)
            emoji = STATUS_EMOJI.get(status, "?")
            print(f"{emoji} {status}  ({result['total_elapsed']:.1f}s)")
            print(f"     {reason}")
            all_results[(label, case["id"])] = result
        print()

    # Filter configs/cases-Liste fuer Report (damit Sub-Run-Reports passen)
    report = build_report(all_results, configs)
    (bench_root / "report.md").write_text(report, encoding="utf-8")
    print(f"== Report: {bench_root / 'report.md'}")


if __name__ == "__main__":
    main()
