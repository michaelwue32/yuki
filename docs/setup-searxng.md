# Setup: SearXNG (Meta-Suchmaschine, lokal)

Yukis Recherche-Tool. Schickt Web-Such-Queries an SearXNG, das im Hintergrund DDG /
Bing / Wikipedia / Reddit parallel anfragt, dedupliziert und JSON zurueckliefert.
Kein API-Key, kein Tracking, keine Cloud-Abhaengigkeit. Yuki ruft das **nur** wenn
der Gehirn-Toggle aktiv ist (oder die Auto-Trigger-Heuristik gegriffen hat).

**Strategie:** Docker-Container in WSL2. Docker Desktop bewusst NICHT — der hat
fuer kommerziell genutzte Rechner Lizenz-Issues, und der Tray-Daemon waere fuer
einen Service der die meiste Zeit idle ist Overkill.

Vom Anfang zur laufenden Suche: **etwa 30 Minuten**, davon ~10 Min Image-Download.
Wenn WSL2 noch nicht installiert ist, kommt ein **einmaliger Windows-Reboot** dazu.

---

## Was du am Ende hast

```
WSL2 Ubuntu-Distribution
  ├─ docker.io + docker compose v2
  └─ ~/searxng/
      ├─ docker-compose.yml
      └─ settings.yml
Service auf 127.0.0.1:8888 erreichbar (HTML-UI + JSON-API)
```

Smoke-Tests:
```powershell
curl.exe -s http://127.0.0.1:8888/healthz                          # → "OK"
curl.exe -s "http://127.0.0.1:8888/search?q=test&format=json"      # → JSON
```

Idle-Footprint: ~150 MB RAM, 0% CPU. Peak bei Suchen: ~300 MB, kurze CPU-Spike.

---

## Voraussetzungen

- **Windows 10/11** mit Admin-Rechten (fuer WSL-Install ggf. einmaliger Reboot)
- **~3 GB Plattenplatz** (WSL2-Distro + Docker + SearXNG-Image)
- **~300-500 MB RAM** zur Laufzeit

---

## 1. WSL2 installieren (einmalig)

Pruefen ob schon da:
```powershell
wsl --status
```

Wenn nicht: in einer **Admin-PowerShell**:
```powershell
wsl --install
```
Installiert WSL2-Kernel + Ubuntu-Default-Distro. **Hier kommt der Reboot.**
Nach Boot: erstes WSL-Start fragt nach Linux-Username + Passwort.

Wenn WSL schon da war: weiter zu Schritt 2.

---

## 2. Docker Engine in WSL2

Start → „Ubuntu" oeffnen (oder PowerShell: `wsl`). Dann:
```bash
sudo apt update && sudo apt install -y docker.io docker-compose-v2
sudo usermod -aG docker $USER
exit                          # WSL verlassen, damit Group-Membership greift
```

Zurueck rein (in PowerShell):
```powershell
wsl
docker --version              # sollte "Docker version 26.x" o.ae. zeigen
```

**Wichtig:** WSL2 hat per Default kein systemd → Docker startet nicht automatisch.
Empfohlene Loesung: systemd in WSL2 einschalten:
```bash
sudo sh -c 'printf "[boot]\nsystemd=true\n" > /etc/wsl.conf'
exit
```
In PowerShell:
```powershell
wsl --shutdown
wsl
```
Ab jetzt faehrt Docker automatisch hoch sobald die WSL-Instanz startet.

Test:
```bash
docker ps                     # leere Liste, kein Error -> Daemon laeuft
```

Falls Docker nicht reagiert: `sudo service docker start` (alter Init-Style).

---

## 3. SearXNG-Container vorbereiten

In WSL:
```bash
mkdir -p ~/searxng && cd ~/searxng

# Secret-Key generieren (lokal, aber Container verlangt einen):
SECRET=$(openssl rand -hex 32)
echo "Generated key: $SECRET"
```

`docker-compose.yml`:
```bash
cat > docker-compose.yml <<'EOF'
services:
  searxng:
    image: searxng/searxng:latest
    container_name: searxng
    restart: unless-stopped
    ports:
      - "127.0.0.1:8888:8080"
    volumes:
      - ./settings.yml:/etc/searxng/settings.yml:rw
    environment:
      - SEARXNG_BASE_URL=http://localhost:8888/
EOF
```

`settings.yml` (Minimal-Config mit JSON-API enabled):
```bash
cat > settings.yml <<EOF
use_default_settings: true

server:
  secret_key: "$SECRET"
  limiter: false           # kein Rate-Limiter (sonst blockt die JSON-API)
  image_proxy: false
  bind_address: "0.0.0.0"
  port: 8080

search:
  formats:
    - html
    - json                 # WICHTIG: ohne diesen Eintrag → HTTP 403 auf /search?format=json
  default_lang: ""
  safe_search: 0
  autocomplete: ""
EOF
```

Wenn du spezifische Engines an/aus willst (Default: alle gaengigen aktiv): die
Default-Engine-Liste laesst sich per `engines:`-Block ueberschreiben. Fuer den
Start: lass es bei `use_default_settings: true`, das ist gut genug.

---

## 4. Starten + Smoke-Test

```bash
cd ~/searxng
docker compose up -d
# erstes Mal: ~500 MB Image-Download, kann 2-5 Min dauern
```

Logs checken:
```bash
docker logs searxng --tail 30
# erwartet ungefaehr: "Listening on 0.0.0.0:8080" + "Application startup complete"
```

Smoke-Test in WSL:
```bash
curl -s http://127.0.0.1:8888/healthz
curl -s "http://127.0.0.1:8888/search?q=hello&format=json" | head -c 300
```

Smoke-Test vom Windows-Host aus (WSL2 forwarded 127.0.0.1 automatisch):
```powershell
curl.exe -s http://127.0.0.1:8888/healthz
curl.exe -s "http://127.0.0.1:8888/search?q=hello&format=json" | Select-Object -First 1
```

Wenn beides antwortet → Service laeuft, du bist fertig.

---

## 5. In `start_yuki.ps1` einbinden (optional, aber empfohlen)

Damit SearXNG bei jedem Yuki-Start mitkommt — nur, wenn WSL+Container schon
existieren (anlegen muss man's vorher, siehe oben):
```powershell
# In start_yuki.ps1 vor server.py:
wsl -d Ubuntu -- bash -c "cd ~/searxng && docker compose up -d" | Out-Null
```

Mit systemd-WSL2 ist das fast schon ein No-op (Container faehrt selbst hoch dank
`restart: unless-stopped` sobald Docker laeuft), aber schadet nicht als
Safety-Net.

---

## 6. Stoppen / Starten / Logs

```bash
docker compose stop                 # SearXNG anhalten
docker compose start                # wieder hoch
docker compose down                 # Container entfernen
docker compose pull && docker compose up -d   # auf neueste Version updaten
docker logs searxng -f              # Live-Logs
docker stats searxng --no-stream    # einmaliger CPU/RAM-Snapshot
```

---

## 7. Wenn's nicht laeuft (Fallback)

**Yuki crasht NICHT, wenn SearXNG weg ist.** Das `web_search`-Tool faengt
Connection-Errors ab und liefert `(search service unreachable)` als Tool-Result
zurueck. Yuki ist via Persona-Prompt instruiert, dann hoeflich zu sagen „dazu
finde ich gerade nichts handfestes" statt zu halluzinieren. Andere Tools
(`weather_by_place`, `wiki_summary`, ...) sind davon unabhaengig — die treffen
ihre eigenen APIs.

**Diagnose:**
```bash
docker ps -a | grep searxng         # Container existiert? Status running?
docker logs searxng --tail 50       # was sagt der Container?
curl -s http://127.0.0.1:8888/      # aus WSL: kommt das HTML-UI?
```

Von Windows aus:
```powershell
Test-NetConnection 127.0.0.1 -Port 8888
```

**WSL-Port-Forward kaputt** (gelegentlich nach Windows-Sleep):
```powershell
wsl --shutdown
wsl                                 # WSL neu hochfahren - Forward wird neu gesetzt
```

**Container in Endlosschleife crashend:**
```bash
docker compose down
docker compose up -d
# Wenn das nicht hilft: docker logs ansehen, oft Config-Tippfehler in settings.yml
```

**Komplett wegblasen + neu:**
```bash
cd ~/searxng
docker compose down -v              # auch volumes entfernen
docker rmi searxng/searxng:latest
docker compose up -d                # frischer Pull
```

---

## Bezugsquellen / Doku

- SearXNG: <https://docs.searxng.org/admin/installation-docker.html>
- WSL2: <https://learn.microsoft.com/en-us/windows/wsl/install>
- Docker in WSL2 (ohne Desktop): <https://nickjanetakis.com/blog/install-docker-in-wsl-2-without-docker-desktop>
