"""
compress_facts.py – yuki_facts.json verdichten.
===============================================
Standard (sicher, KEIN LLM noetig): deterministische Near-Duplicate-Bereinigung – clustert
pro subject lexikalisch fast identische Fakten (z.B. die 'enjoys retro game X'-Explosion ->
'enjoys retro games') und behaelt je Cluster den kuerzesten. Distinkte Fakten (Eigennamen,
Aussehens-Attribute) bleiben erhalten, weil ihre Wortmengen kaum ueberlappen.

Mit  --llm : zusaetzlich semantisches Mergen per Ollama (pro subject). NUR mit einem starken
Modell empfehlenswert (>= 26B, z.B. qwen3.6:27b oder qwen3:32b) – schwache Modelle
droppen dabei Fakten.

Vor dem Schreiben wird immer ein Backup mit Zeitstempel unter `archive/facts/` abgelegt
(`archive/facts/yuki_facts.bak.YYYY-MM-DD_HH-MM-SS.json`), damit jeder Lauf rekonstruierbar bleibt.
Aufruf:  .\\.venv\\Scripts\\python.exe tools\\compress_facts.py            # sicher, deterministisch
         .\\.venv\\Scripts\\python.exe tools\\compress_facts.py --llm      # + semantisches Mergen
"""
import sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")

# Skript lebt in tools/, yuki_core liegt im Projekt-Root - manuell in sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yuki_core as yc


def main():
    use_llm = "--llm" in sys.argv[1:]
    if use_llm:
        if yc.select_ollama_server(verbose=True) is None:
            print("Kein Ollama-Server erreichbar – --llm nicht moeglich."); return
        print("(LLM-Mergen aktiv – nur mit starkem Modell verlustfrei!)")
    else:
        print("(Sicherer Modus: nur deterministische Near-Dup-Bereinigung, kein LLM.)")

    before = len(yc.load_facts())
    print(f"\nFakten aktuell: {before}")
    if before < 2:
        print("Zu wenige Fakten zum Verdichten."); return
    b, a = yc.compress_facts_file(verbose=True, use_llm=use_llm)
    if a < b:
        print(f"\nFertig: {b} -> {a} Fakten. (Original in archive/facts/yuki_facts.bak.<timestamp>.json gesichert)")
        print("\nNeuer Canon:")
        print(yc._facts_block(yc.load_facts()))
    else:
        print("\nKeine Reduktion – Datei unveraendert.")


if __name__ == "__main__":
    main()
