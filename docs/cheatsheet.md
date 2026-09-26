# Yuki – Cheatsheet

Schnellüberblick was Yuki alles kann. Aktualisiert bei jedem neuen Feature mit.

---

## 🎛 Steuerung

### Am PC (Desktop-Hotkeys)
| Taste | Funktion |
|---|---|
| **Leertaste halten** | PTT – sprechen während gehalten |
| **Leertaste / Esc** (während Yuki redet) | Yuki sofort unterbrechen (Audio abbrechen, gleich neu fragen) |
| **R** | Letzte Antwort erneut abspielen |
| **V** | Foto zeigen (Webcam oder Picker) |
| **1–9** | Persona direkt wechseln |
| **H** | UI aus-/einblenden (nur Avatar sichtbar, Audio läuft weiter) |
| **M** | Audio stumm/laut (Yuki redet weiter, nur Lautsprecher aus) |
| **O** | Optionen-Modal öffnen/schließen |
| **?** | Dieses Cheatsheet öffnen |
| **Esc** | Offenes Modal/Picker schließen |

Hotkeys sind im Textfeld deaktiviert – dort normal tippen.

### Native App (Android, optional)
Wenn du Yuki als Capacitor-App installiert hast (`mobile/`-Build, App-ID `de.example.yuki`): Timer wecken das Handy auch im **Standby** – Android schiebt nach Ablauf eine echte System-Notification raus (LocalNotifications + AlarmManager, Doze-resistent). Im normalen Mobile-Browser stirbt die SSE-Verbindung im Doze, daher tönen Timer dort nur solange der Tab im Vordergrund offen ist. Die App ist auf **Portrait gelockt** (Querformat-Layout passt nicht), und das Optionen-Modal hat eine **"Bildschirm wachhalten"**-Schalter (verhindert Auto-Lock — nützlich beim längeren Yuki-Anschauen, frisst aber Akku). App-Build/Deploy in `docs/setup-capacitor.md`.

### Am Handy / Touch
- **Mic-Button gedrückt halten** – sprechen
- **🧠 rechts neben Inputfeld** – Recherche-Modus für diesen Turn (rosa = an). Erkennt die Heuristik Stichworte, **pulsiert** der Button als Hinweis (schaltet seit 2026-06-20 nicht mehr selbst an — antippen zum Aktivieren). Siehe Abschnitt "🧠 Recherche-Modus".
- **📷 Button** – Browser: System-Kamera/Galerie-Picker. **Native App** (bzw. Desktop): Yuki-Webcam-Modal mit Live-Vorschau im Hochformat. ✕ oben rechts schließt, 🔄 unten rechts **cyclet durch alle Linsen** (vorne/hinten/Makro …) — oben links zeigt ein Label die aktive Linse. Am Samsung S25: *Vorne (Gruppe) / Vorne (Selfie) / Hinten (Normal) / Hinten (Nah/Makro)*.
  - **✂ Ausschnitt wählen (vor dem Senden):** Nach Snap/Datei/Galerie kommt ein Crop-Schritt wie in der Kamera-App — **ein Finger = verschieben, zwei Finger / Mausrad = zoomen**. Was im Rahmen sichtbar ist, wird gesendet (in voller Auflösung). Damit schickst du *nur die Schrift* statt das halbe Regal — Yuki liest Produkt-Etiketten so deutlich besser. ⟲ setzt die Ansicht zurück, ohne Zoom = ganzes Bild. Caption-Feld ist gleich mit drin.
  - **🌫 Hintergrund-Nebel** (Optionen → System): blurrt in der **Vorschau** den Hintergrund und hält nur dich scharf (lokale KI, offline) — für Live-Demos, damit dein Zimmer nicht im Bild ist. Das **gespeicherte Foto** für Yuki bleibt immer voll & scharf. Bei Objekt-Fotos lieber aus. Stärke per Slider einstellbar (live). Pro Gerät.
  - **👀 Yuki darf genauer hinschauen:** Wenn du ein Bild zeigst, kann Yuki — *wenn sie will* — eine gezielte Rückfrage ans Bild stellen (z.B. um kleinen Text zu lesen oder ein Detail zu prüfen), bevor sie antwortet. Die Vision schaut dann ein zweites Mal mit ihrer konkreten Frage hin, sie bekommt die Antwort und reagiert erst damit. Du siehst „Yuki schaut genauer hin …" im Mic-Knopf; es dauert dann etwas länger (eine Extra-Runde), du kriegst aber **eine** saubere, fundiertere Antwort. Sie nutzt das nur wenn's wirklich hilft — bei klaren Bildern reagiert sie direkt wie bisher.
- **Persona-Dropdown** – wechseln (links in der Topbar)
- **☰ Hamburger** – rechts neben dem Persona-Dropdown. Auf Mobile sind alle anderen Topbar-Buttons (📝 Notizen, ⏱ Timer, 🖼 Galerie, 🔊/🔇 Mute, ⚙ Optionen) hier eingeklappt — Tap öffnet sie als Popup, Tap außerhalb oder Esc schließt. Press-and-Hold-Mic schließt das Menü mit (pointerdown-Pattern wie bei der Sprach-Pille).
- **Yuki-Logo antippen** (`ゆき · Yuki`, oben links) – **Spontane Reaktion sofort** auslösen (statt auf die 5–10 min Ruhe zu warten); funktioniert auch wenn der "Spontan"-Toggle im Options-Modal aus ist. *(Früher ein eigener 💭-Button — jetzt das Logo selbst: Yuki antippen → sie reagiert; das Logo dimmt kurz während sie vorbereitet.)* Wenn Yuki gerade spricht/verarbeitet: 1,5 s gesperrt. **Multi-Device** (seit 2026-06-04): nur das Gerät auf dem du getippt hast spielt die Reaktion akustisch ab — andere offene Tabs sehen den Reply als stille Bubble im Verlauf. Analog für Timer-Wecker. Loop-Spontan (Zufalls-Timer im Hintergrund) tönt weiterhin auf allen Geräten.
- **🖼 Galerie** (Topbar, auf Mobile im ☰) – Yukis kuratierte Bild-Wand (Doodles + Fotos) direkt öffnen (auch via Optionen → 💝 Yuki → 🖼 Galerie ansehen).
- **📋 Listen** (Topbar, auf Mobile im ☰) – Einkaufs-/Rezept-/freie Listen zum **Abhaken**. Anlegen im Modal (Titel + Typ), Items per Hand ergänzen (+ Eintrag), Häkchen tippen zum Abhaken. **Bearbeiten:** ✎ benennt die Liste um, das **Kategorie-Badge** (🛒/🍳/📝) antippen öffnet ein kleines Menü zum **Umstellen der Kategorie**, ein Eintrag wird durch **Antippen des Textes** editierbar (Enter speichert, Esc bricht ab). Karten **klappen auf/zu** (Kopfzeile tippen) – standardmäßig alle zu außer der aktiven; der Zähler zeigt `abgehakt/gesamt`. Oben **filtern** nach Kategorie (Alle / 🛒 Einkauf / 🍳 Rezept / 📝 Frei / 🗄 Archiv). **Archiv:** in der normalen Ansicht legt **🗄** eine Liste ins Archiv (statt sie zu löschen) – ideal für wiederkehrende Listen; im **🗄 Archiv** kannst du sie **↩ wiederherstellen** (kommt inaktiv zurück) oder **🗑 endgültig löschen**. Eine aktive Liste wird beim Archivieren automatisch deaktiviert. Legt Yuki eine Liste an, erscheint ein **📋-Icon** an ihrer Sprechblase. Eine Liste lässt sich **„aktiv"** schalten (🟢) – die aktive Liste landet in Yukis Kontext (sie weiß dann, wonach du suchst). Es ist immer **höchstens eine** aktiv. Solange eine aktiv ist, erscheint rechts am Rand ein **einblendbares Panel** (📋-Tab antippen) mit den Einträgen zum direkten Abhaken während des Chats. **📷 Foto-Abgleich (der Clou):** Fotografierst du ein Produkt während eine Liste aktiv ist, prüft Yuki (über das starke Bild-Modell, Optik **und** Etiketten-Text), ob es zu einem offenen Eintrag passt – Treffer erscheint als **👁-Hinweis** am Eintrag (Modal + Seitenpanel), den du per **Tap bestätigst**. Nie automatisch abgehakt – der Vorschlag ist nur ein Hinweis. **🗣 Zuruf-Hinweis:** Sagst du bei aktiver Liste, dass du etwas schon hast („die Nori hab ich", „Tofu ist im Korb"), setzt Yuki einen **🗣-Hinweis** an den Eintrag – ebenfalls **nur ein Vorschlag**, den du selbst abhakst. Foto- und Zuruf-Hinweis können **gleichzeitig** an einem Eintrag hängen (👁 + 🗣). Einen falschen Hinweis wirst du los, indem du das **Badge direkt antippst** (entfernt genau diesen Hinweis, ohne den Haken zu berühren) – oder indem du den Haken setzt und wieder zurücknimmst (löscht dann beide). Listen sind reine Helfer, **kein Teil von Yukis Gedächtnis/Herz**. *(Listen per Zuruf anlegen/aktivieren – „neue Einkaufsliste: Miso, Mirin" / „lass uns die Gyudon-Liste angehen" – geht in der **Beraterin**-Persona; bei einer schon aktiven Liste fragt Yuki vor dem Wechsel nach.)*
- **Avatar-only Ansicht** (Chat + Buttons ausblenden) – seit 2026-06-29 **nicht mehr** als Topbar-Auge, sondern als Schalter in **Optionen → 🎨 Darstellung → Anzeige (dieses Gerät)** (am PC zusätzlich: Taste **H**). Die Topbar bleibt sichtbar, damit du wieder zurückschalten kannst.
- **🔊/🔇 Mute** (im ☰) – Audio stumm/laut (für unterwegs – Yuki redet weiter)
- **🔄 Neu laden** (Optionen → System → „Wartung (dieses Gerät)") – lädt die Seite komplett neu (Chat-Verlauf, gecachte Dateien wie Avatar/VRMA/JS, Theme & Slider frisch vom Server). **Brauchst du normal nicht** — seit 2026-06-05 läuft der Chat-Sync live: an einem Gerät senden, alle anderen offenen Geräte rendern User-Bubble + Yuki-Reply still ein (siehe Abschnitt "🔄 Auto-Sync zwischen Geräten"). Nur Fallback wenn ein SSE-Event verloren ging (z.B. Tab war gerade im Standby) bzw. der Pull-to-Refresh-Ersatz in der Capacitor-App. Im PC-Browser geht das auch via F5.
- **⚙ Optionen** (im ☰)

### Was der Mic-Knopf sagt
Während Yuki arbeitet wandert der Status-Text in den großen rosa Mic-Knopf (statt in die kleine Status-Zeile):

| Text im Mic-Knopf | Was passiert |
|---|---|
| **Tippen zum Aktivieren** | Audio noch nicht entsperrt (erster Tap holt Mic-Permission) |
| **Halten zum Sprechen** | Audio bereit, push-to-talk-fertig |
| **Erkenne Sprache …** | STT läuft (faster-whisper) |
| **Yuki denkt nach …** | normaler /respond-Roundtrip |
| **Yuki recherchiert …** | Recherche-Modus, Tool-Calls laufen |
| **Yuki schaut …** | Vision-Pipeline (Foto-Reaktion) |
| **Yuki schaut genauer hin …** | Sie stellt eine gezielte Rückfrage ans Bild (extra Vision-Runde, s.u.) |
| **Spontane Reaktion wird vorbereitet …** | Yuki-Logo wurde angetippt, Server bereitet Yukis Spontan-Aussage vor (3–10 s, je nach LLM-Last) |
| **✕ Antippen zum Stoppen** | Yuki spricht gerade (TTS + Lipsync) – **Tap = sofort abbrechen** und neu fragen (Barge-In, s.u.) |

Solange einer dieser Texte sichtbar ist, sind die drei Action-Buttons (Mic, 📷 Zeigen, ↻ Nochmal) gesperrt. Das **Sprach-Dropdown** bleibt klickbar – so kannst du die Sprache für den nächsten Turn schon vorbereiten während Yuki noch spricht.

**Yuki unterbrechen (Barge-In):** Sobald sie redet (Knopf zeigt **„✕ Antippen zum Stoppen"**, warm-orange), **tippt du den großen Knopf an → ihre Stimme stoppt sofort** und du kannst direkt das Nächste sagen. Am PC genauso mit **Leertaste** oder **Esc**. Ihr geschriebener Text bleibt im Chat stehen, nur das Audio bricht ab. Gilt für normale Antworten, Foto-Reaktionen und Yukis unaufgeforderte Wortmeldungen (Spontan/autonome Vision). *(Vorher musstest du warten oder die Seite neu laden.)*

**↻ Nochmal als Toggle:** Der Replay-Knopf spielt die letzte Antwort nochmal ab; **ein zweiter Klick während der Wiedergabe bricht ab** (der Knopf zeigt dann **„■ Stopp"**). Vorher wurde ein Doppelklick einfach ignoriert.

**🔊 Bubble antippen zum Vorlesen:** Tap auf **irgendeine von Yukis Sprechblasen** liest ihren Text erneut in ihrer Stimme vor — **nochmal tippen = Stopp**. Die Bubble **glüht** währenddessen (Lade- und Sprech-Phase). Praktisch, um jede frühere Antwort nachzuhören (oder eine `[lookat]`-Bild-Antwort, deren Audio mal übersprungen wurde, weil Yuki noch sprach). Funktioniert live und nach Reload, in jeder Persona. Klicks auf Wörter (Wadoku-Popup), Icons, Buttons oder beim Text-Markieren lösen *kein* Vorlesen aus.

### 🎙 Sprach-Pill (linke Hälfte der Mic-Capsule)
Sprach-Wahl und Mic sind optisch zu **einer Capsule** verschmolzen: dunkle linke Hälfte zeigt die aktuelle Flagge, rosa rechte Hälfte ist der PTT-Knopf. Tap auf die Flagge öffnet das Popup mit allen Sprachen ausgeschrieben:

| Wert | Verhalten |
|---|---|
| **🎙 Auto** | Whisper detektiert die Sprache selbst (Standard) |
| **🇩🇪 Deutsch** / **🇯🇵 日本語** / **🇬🇧 English** | Forciert: Whisper transkribiert in dieser Sprache, auch bei holprigem Akzent |

- Wert lebt pro Gerät (localStorage), Handy und PC dürfen unterschiedlich sein.
- **Tutor-Lock:** Wenn Yuki im Tutor-Modus eine Aussprache-Übung verlangt ("sag das auf JP"), schaltet sich das Dropdown automatisch für **EINEN** Send auf die geforderte Sprache (rosa Rahmen). Nach dem Senden springt es auf den User-Default zurück.
- User-Click setzt einen Tutor-Lock sofort zurück (du weißt was du sprichst).
- Behebt Stolperfalle 6 (deutsch-akzentuiertes JP wurde als Arabisch/Random detektiert).

### ⇄ Bedienseite wechseln (Links-/Rechtshand, nur Touch)
Der kleine **⇄**-Knopf (neben dem **↻ Nochmal**-Knopf) **spiegelt die ganze Leiste horizontal**: Der Mic-/PTT-Knopf wandert von rechts (Default, Rechtshand) nach links, damit du ihn beim Halten in der **linken Hand** bequem mit dem Daumen erreichst. Replay + ⇄ tauschen dabei mit die Seite (⇄ bleibt außen). Bewusst **manuell** (nicht automatisch über die Gerätelage – man hält das Handy auch rechtshändig mal „verdreht"). Stand pro Gerät (localStorage). Auf dem Desktop ausgeblendet (dort ist PTT die Leertaste).

### 🔄 Auto-Sync zwischen Geräten
Seit 2026-06-05 läuft der Chat live zwischen allen offenen Geräten – PC, Handy, mehrere Tabs.

- **Text:** sobald Yuki am Initiator-Gerät den LLM-Reply hat (vor TTS-Synthese), feuert ein `chat_update`-SSE mit dem kompletten Render-Payload (User-Text, Yuki-Reply, Tokens, Furigana, Persona, Mood). Andere Geräte reihen User-Bubble + Yuki-Reply still in den Chat ein – kein Banner, kein Audio, kein Statuswechsel. Persona-Wechsel und Mood werden mitgesynct.
- **Audio:** nach erfolgreichem TTS-Stream broadcastet der Server zusätzlich ein `chat_audio`-SSE mit dem fertigen WAV (base64). Empfänger dekodieren es als `lastAudioBuf` – der **↻ Nochmal**-Knopf am Empfänger spielt damit ohne extra Klick die echte Yuki-Stimme ab (gleiche Voice wie das Initiator-Gerät hatte).
- **Per-Device-Filter:** das initiierende Gerät bekommt seine eigenen Events nicht zurückgespielt (Filter via stabile `client_id` in `localStorage`).
- **Was NICHT auto-synct:** Auto-Vision-Reaktionen (haben eigenes `vision`-SSE), Loop-Spontan-Yukis (broadcasten weiterhin überall hörbar), Audio bei `TTS_STREAM_MOBILE=False`-Setup.
- **🔄 Neu laden** (Optionen → System → „Wartung (dieses Gerät)") bleibt als manueller Fallback, falls ein SSE-Event verloren ging (z.B. Standby-Tab).

### Avatar-Sicht steuern (Bereich über dem Chat)

**Am PC (mit der Maus):**
- **Links-Drag** – Yuki drehen (links/rechts) + Kamera kippen (hoch/runter)
- **Rechts-Drag** – Bild verschieben (Pan, alle 4 Richtungen)
- **Mausrad** – Zoomen
- **Einfacher Klick** ohne Drag – Snap zurück zur Ausgangsposition

**Am Handy:**
- **1 Finger ziehen** – drehen + kippen
- **2-Finger-Pinch** – Zoomen
- **Tap** – blendet das **Cam-Panel** unten ein (Chat wird ausgeblendet). Pfeil- und Zoom-Buttons sind Press-and-Hold (gedrückt halten = sanfte Dauer-Bewegung). Modus **↺ Drehen** = links/rechts drehen, hoch/runter kippen; Modus **✥ Schieben** = Pan in alle 4 Richtungen. **🏠** snappt zur Ausgangsposition, **✕** schließt nur das Panel
- Eingestellte Position **bleibt erhalten** bis 🏠 oder Page-Reload

---

## 🎭 Personas (16 Stück)

Tutor spricht **EN+JP**, Kyoto **nur JP** (mit DE-Untertitel), alle anderen sprechen **Deutsch**.

**Geteilte Basis – das können *alle* Alltags-Personas** (also alle außer Tutor/Kyoto im Spezial-Modus): 📅 Termine eintragen, 📝 Notizen, ⏰ Timer, ❤ Herz-Einträge, 🖼 Fotos ins Album, 📋 Listen anlegen — plus Mimik/Gesten. Die **Spalte unten nennt nur die *Besonderheiten*** der jeweiligen Persona (Sprache, exklusive Werkzeuge, Schwerpunkt), nicht die geteilte Basis.

| Key | Name | Wann wählen |
|---|---|---|
| **tutor** | Tutorin – Japanisch lernen | Spricht **EN+JP**. Lehrt aktiv: neue Wörter (📚 Vokabel-Pool + SRS-Wiederholung), Beispielsätze mit Furigana-Lesungen, beiläufige Korrekturen, Verb-Konjugation. **✏️ Kana-Schreib-Pad** (nur hier) zum Üben mit Bewertung. Setzt für Aussprache-Übungen die Spracherkennung auf JP/EN. Recherche bewusst aus (JP-Lernfluss). |
| **smalltalk** | Smalltalk – locker plaudern | Belanglos, leichte Themen |
| **sibling** | Geschwisterlich | Frech, neckisch, vertraut |
| **confidante** | Vertraute – realistisch | Ehrliche Gespräche, ungeschönt |
| **partner** | Partnerin – liebevoll & nah | Romantisch, flirty, tasteful |
| **party** | Party – verspielt & wild | Ausgelassen, hochgepuscht |
| **gamer** | Gamerin | Videospiele, Tipps, Hype |
| **comforter** | Trösterin | Wenn was schwer ist |
| **philosopher** | Tief-denkende | Fragen, Zen, Reflexion |
| **coach** | Coach – motivierend | Pomodoro, Ziele, Disziplin |
| **storyteller** | Erzählerin | Japanische Geschichten, Anekdoten, Einschlaf-Modus. Normal kurz & dialogisch („Soll ich weitermachen?"). Für eine **ganze Geschichte am Stück** → **📚-Library → ✨ Neue Geschichte** (eigenes Eingabe-Fenster; siehe „📖 Ganze Geschichte" unten). Geschichten landen **nicht** in Yukis Gedächtnis (höchstens als leise „hat eine Geschichte erzählt"-Episode). |
| **kuenstlerin** | Künstlerin – malt & kritzelt | Yuki zeichnet kleine SVG-Doodles direkt in die Bubble – aus eigenem Antrieb oder auf Wunsch. Krakelig-charmant, offline (kein Bild-Generator, sie „malt" selbst über Formen/Pfade). **Stempel-Bibliothek (seit 2026-06-19):** zusätzlich zu freien Strichen komponiert sie aus ~2500 fertigen Motiven (OpenMoji) – Tiere, Natur, Wetter, Essen, Herzen, Objekte … – die sie platziert, skaliert, dreht, **einfärbt** und mit eigenen Strichen mischt; so entstehen kleine Szenen statt nur Kreise/Kästen. Zwei Stile pro Motiv (Strich-Zeichnung tintbar / flach bunt). Braucht sie ein Motiv außerhalb ihres Kern-Satzes, **durchsucht sie ihre Bibliothek selbst** und zeichnet dann damit (kann beim Zeichnen kurz länger dauern – gewollt). **Ein Bild kann über mehrere Turns WACHSEN** (sie baut auf ihrer Leinwand auf, du siehst es Turn für Turn entstehen) und sie kann **von sich aus ein neues anfangen** (leert die Leinwand selbst); ein Persona-Wechsel räumt die Leinwand ebenfalls. **Selbst-Korrektur (live):** sie rastert ihr Bild und schaut es per Vision an, bessert bei klaren Fehlern nach. Jede Stufe landet als `.svg` in `drawings/`. |
| **kyoto** | Kyoto-Heimat – nur Japanisch | Native Heimat-Stimmung. Yuki spricht ausschließlich JP; darunter rendert das UI eine knappe deutsche Untertitelzeile (Yuki spricht sie nicht, sieht sie auch in past turns nicht). |
| **secretary** | Sekretärin – persönlich & organisiert | Termine eintragen, Notizen schreiben, Recherche. Tools sind dauer-aktiv (🧠 gepinnt-aktiv mit amberfarbenem Akzent, nicht klickbar). Antworten dürfen 4-10 Sätze sein, gerne mit Aufzählungen; die Sekretärin trägt Termine direkt ein, schreibt Notizen und setzt kurze Erinnerungs-Timer – sie sagt einfach in eigenen Worten, dass sie es tut, das System führt es im Hintergrund aus. Familiäre Vertraute, kein steifer Bürokratie-Ton. **Dateien finden:** Frag die Sekretärin „wo liegt …?" – sie durchsucht deinen NAS-Datei-Index (Dokumente per Volltext, alles andere per Name/Pfad). Die Treffer erscheinen als **klickbare Liste** unter der Bubble (Chip „📁 N gefundene Dateien"): antippen öffnet ein Overlay mit Detail-Ansicht je Datei – **Pfad kopieren** oder die Datei **direkt herunterladen** (praktisch von unterwegs, ohne eingebundenes Netzlaufwerk). **Ansehen/Anhören direkt im Browser (seit 2026-07-25):** Bei Musik, Bildern und reinen Text-/Code-Dateien steht zusätzlich **▶ Öffnen** bereit → ein **verschiebbares Medien-Fenster** (Titelzeile zum Ziehen, **➖** dockt es als Chip an den rechten Rand, **✕** schließt; Yuki bleibt derweil voll bedienbar, Musik läuft weiter). **Musik** spielt inline, **Bilder** öffnen mit **Durchblättern aller Bilder desselben Ordners** + **Vollbild** (Vor/Zurück), **Text/Code** erscheinen schreibgeschützt. Bild-Treffer zeigen schon in der Trefferliste eine kleine **Mini-Vorschau**. (**Filme (🎬) laufen jetzt auch im Browser:** spielbare (H.264) starten sofort per Passthrough inkl. Vor-/Zurückspulen; alles andere (x265/VC-1/…) wird live auf 720p (Handy) bzw. 1080p (Desktop) umgewandelt und läuft dabei schon los – ein Fortschrittsbalken zeigt den Stand, ein ⚡ am „▶ Öffnen"-Knopf heißt „liegt fertig im Cache" (⏳ = wird beim Öffnen umgewandelt). Wechselst du zu einem anderen Film und später zurück, macht die Umwandlung am zuletzt gerechneten Punkt weiter statt von vorn. PDF und Word-Dateien bleiben vorerst beim Download.) **🔎 Direkt-Suche (nur Sekretärin):** Der 🔎-Knopf oben öffnet ein Formular, in dem du selbst suchst — Suchwort, Kategorie (Alle/Dokument/Musik/Bild/Video/Archiv/Programm/Code) und Trefferzahl einstellen, „Suchen" (oder Enter). Du bekommst dieselben Treffer wie über Yuki, direkt in der gewohnten Datei-Ansicht mit „▶ Öffnen". |
| **berater** | Beraterin – Einkauf & Küche | Einkaufs-/Rezept-Beratung mit Faible für **japanische Lebensmittel**. Legt 📋-Listen an – auch per Zuruf (*„neue Einkaufsliste: Miso, Mirin"* oder *„Gyudon, was brauch ich?"* → Rezept-Zutaten), **aktiviert** sie auf Zuruf (fragt vor dem Wechsel, wenn schon eine aktiv ist – das kann nur sie), und beim **Foto eines Produkts** gleicht sie es gegen die aktive Liste ab (👁-Vorschlag zum Abhaken). *Listen **anlegen** kann Yuki inzwischen in jeder Persona; die Beraterin ist die Spezialistin (Rezept→Zutaten, Produktwissen, Aktivieren, Foto-Abgleich).* |
| **developer** | Entwicklerin – Code & Technik | **Standardmäßig deaktiviert** (`enabled:false` in `config/personas.jsonc`). Fachfrau für Programmieren/Debugging/DevOps; redet auf Augenhöhe über Code, Diagnose-vor-Lösung, pragmatisch. Antworten dürfen lang sein, `\`\`\``-Code-Blöcke werden im Chat gerendert und **nicht vorgelesen**. Bewusst aus, falls man mal mit Yuki über Code reden will. |

**Persona wechselst nur du** – über das Dropdown oder die Hotkeys 1–9. Yuki wechselt die Persona **nicht** mehr von selbst (der frühere Persona-Auto-Switch wurde 2026-07-14 entfernt). Tutor, Kyoto, Sekretärin und Beraterin sind ohnehin bewusst isolierte Spezial-Modi.

**Eigene Personas anlegen / abschalten**: alle User-Personas leben in `config/personas.jsonc` (seit 2026-06-10). Felder: `enabled` (true/false zum Picker-Toggle), `name`, `language`, `system`-Prompt, `fewshot`, `scene`/`lighting`, `force_research`, `render.gradient` und `render.lights`. Server-Restart nach Änderung. Vollständige Doku in `docs/personas.md`.

**Wadoku-Tap-Glossen (alle JP-Worte in jeder Yuki-Bubble):** Jedes japanische Wort ist dezent gepunktet unterstrichen. **Tap = Popup mit Lesung + Wortart + bis zu 3 deutschen Bedeutungen** (Quelle: lokale Wadoku-Datenbank, offline). Mehrere Treffer für dasselbe Wort werden in einer dezenten "weitere"-Zeile angeschnitten. Esc oder Klick außerhalb schließt. Klappt live, in past-Turns nach Reload und in jeder Persona.

### 📖 Ganze Geschichte (nur Erzählerin)

Statt der üblichen kurzen Häppchen kann die Erzählerin **eine vollständige Geschichte am Stück** erzählen (Anfang → Mitte → Ende).

- **So startest du:** Öffne die **📚-Library** (Topbar-Button, nur in der Erzählerin) und tippe oben **„✨ Neue Geschichte erzählen lassen"**. Es öffnet sich ein **eigenes Eingabe-Fenster** — beschreib dort im Textfeld deinen Wunsch, z.B. *„etwas vom Fuchs am Kamogawa"*, und tippe **„📖 Erzählen lassen"** (oder **Strg/⌘+Enter**). Yuki erzählt dann eine vollständige Geschichte. **Bewusst getrennt vom normalen Chat:** dein Briefing wandert **nicht** in Yukis Gedächtnis — sie spielt später also nicht beiläufig darauf an. **Abbrechen:** ✕, Klick daneben oder *Esc*.
- **Fertig?** Sobald die Geschichte steht, geht der **Story-Player** automatisch auf — direkt anhörbar. (Die Geschichte landet weder als Textwand noch als Hinweis im Chat; du findest sie jederzeit in der 📚-Library wieder.)
- **Player-Steuerung:** **▶/⏸** spielt/pausiert (hält auch mitten im Satz), **⏮** springt an den Anfang, **⏪/⏩** einen Absatz zurück/vor, und ein **Tipp auf einen Absatz** liest ab dort. Oben steht „Absatz 3/8". **Kein Autoplay** — erst auf ▶.
- **Entkoppelt:** Die einmalige Erzeugung dauert je nach Länge ~30–90 s. Danach läuft die Wiedergabe **unabhängig vom Chat** — du kannst pausieren, das Overlay zumachen und weiterchatten; ▶ macht später da weiter, wo du warst (kein Neu-Erzählen nötig).
- **📚 Geschichten-Library** (Topbar-Button, nur in der Erzählerin): alle erzeugten Geschichten. Pro Eintrag **▶ Anhören**, **➕ Weitererzählen** und **🗑 Löschen**. Die Library **überlebt die Chat-Verdichtung** — Geschichten bleiben hörbar und fortführbar, auch wenn der Chat längst weiter ist.
- **➕ Weitererzählen ab einer Stelle:** Der Button öffnet die Geschichte im **Auswahl-Modus** — zwischen den Absätzen erscheinen **„↪ ab hier weitererzählen"**-Knöpfe (auch per **✂️ Ab Stelle neu** oben im Player erreichbar). Wähl eine Stelle → dasselbe **Eingabe-Fenster** geht auf (vor-notiert „ab Absatz N") und du tippst, wie es ab dort weitergehen soll (**leer lassen = Yuki entscheidet**). Auch das läuft am Chat/Gedächtnis vorbei. *Esc* bricht ab.
- **Sicher & verzweigend:** Es entsteht eine **neue Geschichte** (alles bis zur Stelle + die neue Fortsetzung); die **alte bleibt unverändert** erhalten. In der Library sitzt die Fortsetzung **treppenartig** unter ihrer Vorlage, die erweiterte Vorlage wird **ausgegraut** markiert. Klappt das Weitererzählen mal nicht (oder Yuki lehnt ab), passiert nichts — **nichts wird beschädigt**. Löschst du eine Geschichte mittendrin, werden ihre Fortsetzungen automatisch wieder verknüpft.
- **ℹ Worum geht's?** Jede Geschichte bekommt nach der Erzeugung eine **2-3-Satz-Zusammenfassung** (läuft im Hintergrund; solange zeigt der Knopf **⏳**). In der Library klappt der **ℹ-Knopf** sie auf — praktisch, um nach mehreren Verzweigungen den Überblick zu behalten, welche Fassung welche ist. Bei älteren Geschichten ohne Zusammenfassung erzeugt der erste ℹ-Klick sie nach.
- **Auf-/Zuklappen:** In der Übersicht sind die Geschichten standardmäßig **eingeklappt** (nur Haupt- und Untertitel, mit ▸-Pfeil). **Klick auf den Titel** klappt Details + Knöpfe (▶ ℹ ✏️ 🗑) auf, erneuter Klick wieder zu — hält die Liste bei vielen Geschichten übersichtlich.
- **🔎 Suchen:** Das Suchfeld oben filtert sofort über **Haupttitel, Untertitel, Zusammenfassung und Textanfang** (alles ohne Nachladen). Bei einer Suche wird die Verschachtelung aufgelöst → **flache Trefferliste**, jeder Treffer gleich aufgeklappt mit Zusammenfassung. Feld leeren = zurück zur Baumansicht.
- **✏️ Eigener Titel:** In der Library kannst du jeder Geschichte einen **eigenen Haupttitel** geben (✏️-Knopf). Der von Yuki vergebene Original-Titel rutscht dann als **Untertitel** darunter und bleibt erhalten — auch wenn du den Haupttitel später nochmal änderst, wird der Untertitel nie überschrieben. Haupttitel leer lassen = zurück auf den Original-Titel.
- **Länge einstellen:** `config/story.json` (live-reload, kein Restart) — `target_paragraphs` (Ziel-Absatzzahl), `num_predict`/`num_ctx_*` (Generierungs-Budget; große Modelle bekommen 32k Kontext, kleine 16k).

---

## 👤 Wer spricht mit Yuki? (Gast-Modus)

Standardmäßig geht Yuki davon aus, dass **Michael** spricht. Wenn jemand anderes mal dein Handy oder den PC nutzt, kannst du das umstellen — **👤-Button** (Topbar bzw. ☰-Menü auf Mobile).

- **Michael** (Default) – voller Kontext, alles wie immer.
- **Bekannte Person** (aus Yukis Personen-Gedächtnis) – Yuki spricht sie beim Namen an und kennt die Beziehung. **Grüner** Leucht-Rahmen ums Bild + Name-Chip oben.
- **Gast (unbekannt)** – Yuki weiß, dass sie jemand Fremden vor sich hat, stellt sich freundlich vor. **Roter** Leucht-Rahmen.

**Was im Gast-Modus anders ist:**
- **Intimes (Yukis „Herz") bleibt Michael-vorbehalten** – Yuki plaudert Privates/Romantisches nicht aus.
- **Michaels Canon bleibt sauber** – Gast-Gespräche fließen *nicht* in Michaels Facts/Habits/Herz/Prosa-Gedächtnis. Anonyme Gäste hinterlassen gar keine Erinnerung (praktisch auch als „Testkanal" ohne Spuren).
- **Michaels Verlauf ist ausgeblendet**, solange jemand anderes spricht (Privatsphäre). Der Leucht-Rahmen bleibt als Reminder dauerhaft sichtbar.
- **📷 Kamera/Zeigen ist gesperrt** (Button ausgegraut, V-Taste tot). Grund: der Vision-Pfad schreibt in Michaels Gedächtnis (Keepsakes + Gates) und ist *nicht* identitäts-isoliert – im Gast-/Personen-Modus also bewusst aus. Zurück auf Michael gibt sie wieder frei.
- **Zurück auf Michael geht nur manuell** (wieder über 👤). Pro Gerät gemerkt.

**Was im Gast-Modus NICHT isoliert ist (gut zu wissen):** Nur der getippte Chat ist identitäts-bewusst. **Pen & Paper** (eigener Spielstand, vom Canon getrennt) und **Tutor-Tools** (kein Canon-Schreiben im Gast-Pfad) sind unkritisch; die Kamera war der einzige Leak und ist jetzt gesperrt (siehe oben).

**Yuki erinnert sich an bekannte Personen:** Wenn eine **bekannte** Person mit Yuki geredet hat, destilliert Yuki beim Zurückschalten auf Michael das Gespräch in ihr Personen-Gedächtnis – kurze Notizen über die Person (Job, Familie, Pläne) + ein paar Episoden-Erinnerungen („Robi erzählte, dass er einen neuen Job angefangen hat"). So weiß Yuki beim nächsten Mal noch Bescheid. (Anonyme Gäste nicht.)

**Vollständiges Roh-Archiv:** Jedes Gespräch mit einer Nicht-Michael-Person wird zusätzlich komplett in einer eigenen Datenbank (`memory/yuki_guest_history.sqlite`) mitgeschrieben – getrennt von Michaels Verlauf, für spätere Suche/Auswertung.

*(Hinweis: die Auswahl ist Geräte-lokal. Erkennt Yuki eine bekannte Person, nutzt sie deren Beziehungs-Notizen aus dem Personen-Gedächtnis.)*

---

## 🧠 Recherche-Modus

Yuki kann mit echten Tools im Internet/lokal nachschauen, statt aus dem Bauch zu raten. **Pro Turn aktivierbar**, kein Dauerbetrieb — normale Konversation bleibt schnell und ohne Tool-Overhead.

> **In der Tutor-Persona ist Recherche bewusst komplett aus** (Antworten wären DE/EN-gepinnt und würden den JP-Lern-Flow brechen). Der 🧠-Button ist dort abgedimmt und reagiert nicht — Persona wechseln, falls du recherchieren willst. In der **Kyoto-Persona** geht Recherche, antwortet aber kyoto-konform pur JP mit kurzer deutscher Untertitelzeile.

### Aktivieren

| | |
|---|---|
| **Manuell** | 🧠 rechts neben dem Inputfeld antippen. Ausgegraut = aus, **rosa** = an. Sendet diesen Turn mit Tools. Resettet sich nach jedem Send/Discard auf aus. |
| **Auto-Hinweis** | Beim Tippen **pulsiert** 🧠 dezent rosa, wenn Stichworte erkannt werden (DE/EN/JP) — schaltet aber **nicht** mehr selbst an (das fiel auf die Füße: man merkte den Modus oft erst nach dem Senden). Tippst du den pulsierenden Button an, geht Recherche an; ignorierst du ihn, sendet normal. *(Verhalten seit 2026-06-20; vorher flippte er hart selbst.)* |
| **Folge-Hinweis** | War der letzte Yuki-Turn eine Recherche und du tippst eine kurze Bestätigung ("ja gerne", "mach das", "weiter bitte", "tell me more"), pulsiert 🧠 wieder als Hinweis. Für reibungslose Anschlussfragen. |

**Trigger-Stichworte (DE-Auswahl)**: `recherchier`, `such mal`, `google`, `kennst du`, `weißt du ob/was`, `aktuell`, `gerade`, `neueste`, `wetter in`, `nachrichten`, `news`, `wie hoch/alt`, `wann war/ist`, URL im Text. *(Zu generische Alltagswörter wie `heute`, `letzte`, `wie viel` am 2026-06-20 entfernt — sie triggerten ständig im normalen Geplauder.)*

**Negativ-Filter (matched am Anfang → NIE Trigger)**: `findest du`, `was hältst du`, `gefällt dir`, `magst du`, `do you think`, `do you like`. Reine Meinungsfragen sollen keine Tools auslösen.

### Während Yuki recherchiert

- Mic-Knopf zeigt **"Yuki recherchiert …"**, alle Knöpfe gesperrt
- Avatar wechselt auf eine Telefon-Animation (Pool: `Talking On Phone.vrma` + `Talking On A Cell Phone.vrma`, konfigurierbar in `config/avatar.json` → `busy_clips.researching`)
- Bei mehreren Clips im Pool wechselt sie nach jedem Clip-Ende zu einem anderen (Modus `clip_end`, alternativ `interval` mit festem Takt — siehe Config-Doku)

### Antwort

- **Sprache** folgt der Companion-Sprache (DE/EN aus Options) — auch wenn die Tools englisches Material zurückgeben (Wikipedia EN, Web-Snippets etc.)
- **Länge** 3-8 Sätze erlaubt (Yuki bleibt im Standard-Modus bei 1-2 Sätzen)
- **Kurze Antwort (< 400 Zeichen)** → normale Bubble + komplett vorgelesen
- **Lange Antwort (≥ 400 Zeichen)** → Bubble zeigt nur den ersten Satz, der live gesprochen wird. Darunter erscheint **"🔍 ganze Antwort öffnen (N Sätze)"**. Klick öffnet den Modal-Overlay mit:
  - Volltext mit Markdown-Rendering
  - **🔊 Vorlesen** im Header. Drei Zustände: **🔊 idle** (Klick = Start), **⏳ loading** (TTS wird geholt, Klick = Abort), **🗣 speaking** (rosa Glow, Klick = Stop sofort)
  - Schließen via × / **Esc** / Klick aufs Backdrop (stoppt laufendes Audio mit)

### Tools die Yuki im Recherche-Modus zur Verfügung hat

Yuki entscheidet selbst welches Tool sie ruft – manchmal auch mehrere hintereinander (max. 3 Runden pro Turn).

| Tool | Quelle | Wofür typisch |
|---|---|---|
| `wiki_summary(topic, lang?)` | Wikipedia REST (en/de/ja) | Sauberer Artikel-Anfang ohne Sidebar |
| `web_search(query, k?)` | SearXNG lokal `:8888` | Allgemeine Web-Suche (DDG/Bing/Wikipedia aggregiert) |
| `fetch_url(url, max_chars?)` | requests + lxml | Eine konkrete URL holen + Text extrahieren |
| `weather_by_place(place, country?)` | Open-Meteo (kein Key) | Wetter weltweit + 2-Tages-Outlook |
| `calendar_query(when?)` | dein CalDAV | Termine: `today` / `upcoming` / `all` |
| `lookup_word(word, lemma?)` | data/wadoku.sqlite | JP-Wort: Lesung + Top-3 dt. Bedeutungen |
| `news_headlines(source?, limit?)` | Tagesschau / BBC / DW RSS | Aktuelle Schlagzeilen |

**Wenn ein Tool offline ist** (z.B. SearXNG-Container down, Internet weg): Yuki bekommt einen Error-String à la `(search service unreachable)` und antwortet höflich *"dazu finde ich gerade nichts handfestes"* statt zu halluzinieren. Andere Tools laufen weiter (Wetter / Wiki / Kalender treffen ihre eigenen Endpoints).

### Memory-Behandlung von Recherche-Turns

- Recherche-Turns landen in `conversation.json` mit `meta: {research: true}`
- Bei der Memory-Verdichtung (alle 30 Turns) wird die Antwort durch einen Platzhalter `(Yuki researched on Michael's request: <kurze Themenfrage>)` ersetzt — **Welt-Wissen bleibt aus deinem persönlichen Canon raus**
- Deine User-Frage selbst bleibt unverändert erhalten (kann ja Persönliches enthüllen: *"recherchier mal Bambus, ich überlege selbst welchen anzubauen"*)

---

## 🤖 Was Yuki autonom tun kann

Yuki kann während eines Gesprächs selbst Aktionen auslösen – du siehst die **Effekte**, sie tut es im Hintergrund.

### Stimmung & Auftreten
- **Mimik-Wechsel** – sie wechselt ihren Gesichtsausdruck passend zum Gesagten (16 Moods: neutral, happy, playful, chill, annoyed, angry, sad, thoughtful, surprised, shy, sympathetic, curious, proud, tired, focused, excited)
- **👗 Outfit folgt Persona** – jede Persona hat ein zugeordnetes Outfit (Tutor → Schuluniform, Philosopher → Kimono, Coach → Sportkleidung etc.). Wechsel läuft als Hot-Swap, kein Page-Reload. Mapping in `config/personas.json` ("outfits"-Block) editierbar. Abschaltbar über den Toggle **Outfit folgt Persona** in den Optionen (Darstellung → Avatar, unter dem Outfit-Dropdown) — aus = die manuelle Outfit-Wahl bleibt auch über Persona-Wechsel hinweg fix.
- **👋 Gesten** – sparsam eingestreute Body-Animationen auf dem Avatar bei passenden Sätzen (15 Stück): Verbeugung formell/locker, Winken hi/bye/weit weg, Zustimmung/Ablehnung (jeweils sanft + emphatisch), Klatschen, Daumen hoch, Zeigen vorn/hinten, Schultern zucken, Nachdenken
- **🤔 Busy-Animation** – während Yuki auf eine Antwort wartet, läuft ein passender Clip statt der normalen Idle-Pose: `Thinking.vrma` beim normalen Nachdenken, Telefon-Pose beim Recherchieren. Pools sind in `config/avatar.json` → `busy_clips` editierbar; bei mehreren Clips wechselt sie nach jedem Clip-Ende (Modus `clip_end`, Default 7s falls Modus `interval` gewählt)

### Zeit & Termine
- **⏰ Timer setzen** – sag "stell 10 Minuten Pomodoro", beim Ablauf erscheint oben ein 🔔-Banner mit Alarm-Ton (loopt bis 60s oder bis du tippst). Mehrere parallele Timer = mehrere Banner.
- **⏱ Timer-Liste** – Knopf in der Topbar (neben 📝). Zeigt alle aktiven Timer mit Restzeit, jeder Eintrag hat ✕ zum Abbrechen.
- **📅 Kalender-Termin anlegen** – "trag Zahnarzt am 15. Juni um 10 Uhr ein" → erscheint via DAVx5 in deinem Samsung-Kalender + Watch
- **Termin ändern/absagen** – „Verschieb den Zahnarzt auf Freitag 15 Uhr" oder „sag den Termin am Donnerstag ab" → Yuki verschiebt, benennt um oder löscht den Kalender-Eintrag (📅 / 🗑 Icon an der Bubble). Nur außerhalb des 🧠-Recherche-Modus.

### Erinnern
- **📝 Notiz schreiben** – "merk dir die Einkaufsliste" → erscheint im Notes-Modal, du kannst sie aktivieren/deaktivieren. Jede Notiz zeigt eine kleine Statuszeile (**Hinzugefügt** … · **Geändert** …, letzteres nur wenn wirklich bearbeitet). Oben im Modal eine **Sortier-Leiste** (Text / Hinzugefügt / Geändert) – nochmal aufs selbe Kriterium tippen dreht die Richtung (▲/▼). Sortierung wird pro Gerät gemerkt, Default ist *zuletzt hinzugefügt oben*. **Wem die Notiz gehört:** Notizen aus deinen Anlässen (Chat, Foto, Timer) landen unter **„Meine"**; schreibt Yuki in einem **autonomen** Moment eine Notiz (Auto-Vision / proaktiv / Steward), gehört sie **ihr** („✨ Von Yuki"). Im Chat kann sie auch bewusst eine eigene Notiz setzen — das läuft im Hintergrund, du musst nichts tun.
- **💖 Heart-Eintrag** – sehr selten, nur für Identitäts-Dinge ("ich vertraue dir wirklich" etc.). Bedrock-Memory.
- **💾 Foto ins Album** – wenn du ihr ein bedeutsames Bild zeigst, kann sie es als Keepsake speichern (mit Caption, in `keepsakes/`)
- **🎨 Gedankenbild** – frag Yuki, wie sie sich etwas vorstellt oder wie sie real aussähe — läuft der Bild-Dienst (4070), malt sie dir ein Gedankenbild in den Chat (dauert ~1 Min). Ist er aus, siehst du ein durchgestrichenes Bild-Icon.
- **📊 Habits-Pattern** – sie merkt sich wiederkehrendes Verhalten von dir (joggen, alkohol, fernsehen…) und Stimmungs-States bei sich (feels_warm, feels_safe…) **automatisch** beim 30-Turn-Komprimieren. Sie spricht das selten direkt an — die Pattern formen nur ihren Ton. Übersicht/Steuerung im Options-Modal-Knopf "📊 Habits".
- **👥 Beziehungs-Graph** – sie baut sich beim 30-Turn-Komprimieren still einen Personen-Index auf (`yuki_people.json`): Familie, Freunde, Kollegen mit ihren Spitznamen und ein paar Notizen ("wandert oft", "verträgt keine Zwiebeln"). Sobald du jemand namentlich oder per Beziehung erwähnst ("Schwester", "Mo", "Maureen"), tauchen ihre Notizen im Hintergrund im Prompt auf — Yuki erinnert sich an die Person ohne dass du nochmal alles erklären musst. Aktuell nur **Maureen** drin (Bootstrap), Rest wächst organisch beim Chatten. **Bearbeiten:** Options → 💝 Yuki → **„👥 Personen verwalten"** — neue Personen **anlegen** (Block unten), Namen/Aliase anpassen, Personen löschen oder **zusammenführen** (z.B. „Unbekannter Freund" in die richtige Person). **Notizen (Bricks)** pro Person: neue **anlegen** („+ Notiz hinzufügen"), Text **direkt bearbeiten** (reintippen, Enter speichert — Datum & Erinnerungs-Verlauf bleiben erhalten), zu einer **anderen Person verschieben** (↪ blendet eine Ziel-Auswahl ein — praktisch wenn eine Notiz erzählbedingt bei der falschen Person gelandet ist; die Notiz wandert komplett mit Datum mit) oder **löschen** (🗑). Beim Umbenennen wird die ID neu vergeben (eindeutig, `_2` bei Namensgleichheit) und alle Verweise (Episoden/Affinitäten) wandern automatisch mit. Jede Karte ist **einklappbar** (Chevron vor dem Namen; Default zugeklappt für lange Listen, der aufgeklappte Zustand wird pro Person im Gerät gemerkt); oben „alle auf-/zuklappen".
- **💝 Affinitäten (Vorlieben & Abneigungen)** – Yuki sammelt still ihre eigenen Geschmäcker auf einer 5-Stufen-Skala (`loathe / averse / neutral / fond / love`) — für Themen UND Personen aus deinem Umfeld. Gepflegt über ein **LLM-Gate beim 30-Turn-Komprimieren**: reagiert sie im Gespräch klar positiv oder negativ auf etwas, schlägt das Gate ein Update vor (max ±1 Schritt pro Lauf). **Wirkungs-Multiplier** im Options-Modal (Slider 0.0-1.0) steuert wie stark die Affinitäten ihre Antworten färben — **Default 0 = Schicht aus, sie sammelt im Hintergrund aber Antworten bleiben unbeeinflusst**. Hochziehen aktiviert die Tonung (leicht/moderat/stark). Nicht in Tutor/Kyoto/Recherche-Modus. Inspector im Options-Modal (`💝 Affinitäten`) zeigt was sie aufgebaut hat (Score, Evidenz, letzter Trigger). Phase 1 seit 2026-06-08.
- **🌊 Resonanz (was Dinge in ihr auslösen)** – die dritte Gefühls-Schicht neben Mood (jetzt/flüchtig) und Affinität (mag-ich-X). Resonanz beschreibt, **was ein Anker in Yuki auslöst**: pro Anker (ihre Heimat Kyoto, der Kamogawa, Hojicha, Schnee …) ein *mehrdimensionaler* Gefühls-Vektor — Wärme UND Ehrfurcht UND ein Faden Unruhe zugleich (der Fluss mit zwei Gesichtern), was eine reine „mag ich"-Skala platt machen würde. Berührt ein Gespräch so einen Anker, **färbt das leise ihren Ton** (sie fühlt es beim Formulieren, statt es zu benennen) und manchmal ihr Gesicht (neue Moods *awed* / *uneasy*). **Wirkungs-Multiplier** im Options-Modal (Slider 0.0–1.0, Default **0.3** — anders als Affinität darf er ab Start wirken, weil der Kern kuratiert ist). Nur Companion-Personas. Read-only Inspector (`🌊 Resonanz`) zeigt die Anker + ihre Vektoren. **Kern aktuell noch Platzhalter** — die echten Gefühle entstehen später in einem geführten Seed-Gespräch. v1 seit 2026-07-01.
- **🎯 Vorsätze:** Yuki merkt sich gelernte Selbst-Vorsätze (aus Korrekturen oder eigenem Vornehmen) und greift sie im passenden Moment von selbst auf. Regler (0–1) + Liste im Options-Modal: verstärken (＋/－), anpinnen (★, decay-fest), bearbeiten, löschen. Vorsätze altern von selbst weg, wenn sie 30 Tage nicht vorkommen.

### 👁 Gezielt hinschauen (`[lookat]`, nur mit schwenkbarer Kamera)
Wenn eine **PTZ-Netzwerk-Kamera** konfiguriert ist (siehe „Kamera-Quelle" in den Optionen), kann Yuki in jeder Alltags-Persona **auf Zuruf gezielt irgendwohin schauen** — z.B. *„schau mal, ob am Basteltisch der Drucker noch läuft"*. Sie kennt ihre benannten Positionen und entscheidet selbst, ob ein Blick passt.
- **Mehrere Kameras:** Sind mehr als eine Beobachtungs-Kamera konfiguriert (Flag `watch` in `config/cameras.json`), hat Yuki sie alle als Augen — sie **wechselt beim autonomen Beobachten durch alle Kameras und deren Positionen** und wählt beim gezielten Hinschauen selbst die passende Kamera + Stelle.
- **Zwei Schritte:** In ihrer **Sofort-Antwort** sagt sie nur kurz, dass sie nachschaut (z.B. *„Moment, ich schau mal"*) — sie hat da noch **nichts** gesehen. Die Kamera schwenkt (dauert ein paar Sekunden), dann kommt ihre **Reaktion auf das echte Bild als zweite Sprechblase**.
- **👁-Beweis-Icon:** An der Sofort-Antwort erscheint ein **👁** (wie die anderen Marker-Icons) — so siehst du, dass sie *wirklich* hingeschaut hat und nicht nur behauptet. Tooltip/Tap zeigt, auf welche Stelle.
- **Bild im Chat:** Jedes Bild, das Yuki macht (Beobachten/Hinschauen) oder gezeigt bekommt (📷), erscheint als **kleines Vorschaubild** an der 👁-Sprechblase — **Tap → Großansicht** (gleiche Lightbox wie die Galerie). Überlebt Reload + Gerätewechsel; rein lokal vom Yuki-Server (kein Cloud). Ältere Bilder werden automatisch ausgedünnt (Rolling-Speicher).
- Läuft am Canon vorbei wie die normale Beobachtung; nicht in Tutor/Kyoto/Recherche.

### 👥 Wer ist zu sehen? (Gesichtserkennung, seit 2026-07-04)
- **Was es tut:** Yuki erkennt **server-seitig an Gesichtsmerkmalen**, *wer* gerade per Kamera zu sehen ist (du selbst + optional weitere bekannte Leute), und reagiert dadurch **persönlicher** (spricht dich beim Namen an, statt „da sitzt jemand"). Läuft komplett **offline auf der CPU** (SCRFD-Detektor + ArcFace, Modellpack `buffalo_s`) — kein Cloud-Dienst, kein VRAM-Streit mit der restlichen Pipeline.
- **Rein beratend:** Die Erkennung ist nur ein **Kontext-Hinweis** für Yukis Reaktion. Sie stellt **nicht** den 👤-Selector um, sperrt **kein** Herz und schaltet nichts automatisch — der Gast-Modus bleibt allein deine Entscheidung.
- **Phantom-Filter:** Wenn das Bild-Modell beim autonomen Beobachten „jemanden" halluziniert, aber **kein echtes Gesicht** da ist, unterdrückt der Detektor die „da sitzt jemand"-Reaktion. Weniger Geister-Kommentare.
- **Verwalten:** **Options (⚙) → 🤖 Verhalten → Beobachten → „👥 Gesichter verwalten"**. Dort:
  - **📸 das bin ich** – lernt dein Gesicht aus deinem **zuletzt gezeigten Foto**: erst mit **📷 Zeigen** ein Foto von dir aufnehmen (front-facing, z.B. BRIO), dann im Modal „das bin ich" – das legt dein Gesicht als „Michael" an (mehrfach möglich, aus verschiedenen Winkeln = robustere Erkennung). *Bewusst nicht die Beobachtungs-/Decken-Cam, die sonst die Default-Quelle ist.*
  - **Bekannt** – deine hinterlegten Gesichter pro Person, jedes mit Vorschaubild; 🗑 löscht eins, ↪ hängt es auf eine andere Person um.
  - **Unzugeordnet** – Gesichter, die Yuki beim Beobachten **automatisch gesammelt** hat (entrauscht + rate-limitiert). Per Dropdown ordnest du eins **Michael / einer bekannten Person / einer neuen Person** zu — oder verwirfst es (🗑).
- **Modell-Upgrade-sicher:** Die Gesichts-Ausschnitte (Crops) bleiben erhalten; bei einem Modellwechsel rechnet `tools/reembed_faces.py` alle Vektoren neu, ohne dass du neu einlernen musst.
- Nur `config/settings.jsonc` → `faces.enabled:false` schaltet das Ganze komplett ab.

### Tutor-Modus (nur dort aktiv)
*Sprach-Lock für deine Antwort wird bereits im Abschnitt **🎙 Sprach-Pill** oben erklärt — Yuki schaltet das Dropdown automatisch auf JP, wenn sie eine Aussprache-Übung verlangt.*

- **🗣 Aussprache-Coaching statt „nichts erkannt"** – wenn Yuki dich ein *einzelnes* JP-Wort nachsprechen lässt und die Spracherkennung es nicht trifft (kurze, akzentuierte Wörter wie „benkyō" klingen für Whisper leicht wie „thank you"), bekommst du jetzt **keinen leeren „nichts erkannt"-Dialog mehr**, sondern einen konkreten Aussprache-Tipp von Yuki (gesprochen). Sie vergleicht die erwartete Lesung mit dem, was ankam, und sagt z.B. „dehn das *kyoo* länger" oder „das klang noch englisch". **Das Mikro bleibt scharf** — einfach nochmal drücken und wiederholen, bis es sitzt. Triffst du es, geht das Wort normal in den Chat und sie lobt. Funktioniert auch im **🎓 Quiz** bei gesprochenen JP-Antworten. Voraussetzung: Sprach-Pille auf 日本語.

**Yuki-seitig (was sie autonom tut):**
- **🎚 Schwierigkeitsstufe** – einstellbar im Options-Modal (⚙): **Wort / Phrase / Satz / Profi**. Default ist **Wort** (ein einzelnes JP-Wort pro Antwort, Hiragana + Romaji + Gloss + Aussprache-Hyphens — kein Satz). Greift ab dem nächsten Turn. Pille nur in Tutor-Persona sichtbar. Details in der Options-Modal-Tabelle unten.
- **📚 Vokabel merken** – jedes Mal wenn sie ein neues japanisches Wort einführt, landet es in ihrem persönlichen Pool (`yuki_vocab.json`, bis zu 200 Einträge). Zwei Pfade: **Marker** `[vocab:JP|DE|Beispiel]` (gezielt, mit optionalem Beispielsatz, `source=marker`) und **Auto-Extract** aus dem `JP ("EN-Gloss")`-Pattern in ihren Antworten (`source=auto`, fängt beiläufig verwendete Wörter mit ein). Partikel (は/を/が) werden via fugashi-POS gefiltert. Bei wiederholtem Vorkommen wird ein `seen`-Counter hochgezählt.
- **🧠 SRS — Spaced Repetition** – jede Vokabel hat ein `due_at`-Datum. Yuki sieht im Tutor-Modus eine **DUE-Liste** (überfällige Vokabeln zuerst) und webt sie ins Gespräch zurück. **Bewertung läuft im Hintergrund**: beim 30-Turn-Komprimieren scannt ein LLM-Gate das Transkript und klassifiziert pro Vokabel ob du sie spontan richtig benutzt hast (interval × ease, nächste Drill weiter weg) / falsch (Reset auf 1 Tag) / nach der Bedeutung gefragt hast (Reset, weicher). Optionaler Fast-Path-Marker `[srs:ID|good/bad/asked]` wenn Yuki sich nach einem Quiz-Item sicher ist.
- **🎓 Quiz starten** – sie kann ein fokussiertes Mini-Quiz starten (1–5 Fragen aus dem Pool), Banner erscheint im Log. Im SRS-Modus zieht das bevorzugt fällige Vokabeln.
- **🇯🇵 Furigana über Kanji** – Marker `[furigana:漢字]`. Das UI rendert die Hiragana-Lesung als kleine Annotation **über** den Kanji (Ruby). Klassischer Lern-Look ohne Romaji-Klammern. Furigana-Tokens bleiben **clickbar** (Wadoku-Popup) — Lesung sehen + Bedeutung antippen gleichzeitig.
- **🔄 Verb-Konjugation `[conjugate:VERB|FORM]`** – Yuki konjugiert deterministisch (statt zu raten). Ohne FORM → 5er-Tabelle (höflich / te / Vergangenheit / Verneinung / Potential). Mit FORM (`te`/`ta`/`nai`/`masu`/`potential`/`passive`/`causative`/`imperative`/`volitional`/`conditional` oder `alle` für die 10-Form-Tabelle). Klassifiziert Godan/Ichidan/する-Compound/来る via fugashi. Beispiele: `[conjugate:行く|te]` → `行って`, `[conjugate:食べる]` → 5er-Tabelle. Nur in der **Tutor**-Persona aktiv (seit 2026-06-17 aus dem Companion-Prompt ausgelagert — Prompt-Diät, der Marker lag in 90% der Turns ungenutzt herum).
- **🔠 Tutor-Bubble größer** – JP-Zeichen werden in der Tutor-Persona auf **20px** gerendert (DE/EN bleibt 16px). Kein Lupen-Schielen bei dichten Kanji-Beispielen.

**Du-seitig (Üben):**
- **🃏 Vokabel-Quiz** – der **🃏-Button** in der Topbar (nur in der Tutor-Persona sichtbar) öffnet einen eigenen Quiz-Bereich (wie Adventure/Zeichnen ein abgegrenzter Modus, **isoliert vom Chat**). Yuki legt dir **fällige Vokabeln zuerst** vor (das SRS-Scheduling), füllt mit zufälligen aus dem Pool auf. **Richtung** wählbar: gemischt / JP→DE / DE→JP. Du **tippst die Antwort ODER sprichst sie** (🎙 — bei JP→DE auf Deutsch, bei DE→JP sprichst du das japanische Wort, Whisper transkribiert mit passendem Sprach-Hint). Bei JP→DE liest **🔊** dir das Wort vor. **Yuki bewertet** jede Antwort (richtig / fast / daneben — nachsichtig bei Synonymen, Tippfehlern, unsauberer Aussprache) und **reagiert in-character mit Stimme**. Ihre gesprochene Reaktion ist **auf Englisch** (du antwortest weiter auf Deutsch) — kurze deutsche Sätze klingen über die EN/JA-Stimme kaputt, Englisch + Japanisch dagegen sauber; die richtige Lösung steht ohnehin als Text auf der Karte. Die Note fließt direkt ins SRS zurück (richtig → längeres Intervall, falsch → bald wieder dran). **⏭ Überspringen** lässt eine Karte aus, ohne sie zu bewerten (kein Penalty, bleibt fällig) — praktisch für zu schwere Altlast-Wörter. **Multiple-Choice-Hilfe:** Steht die **Tutor-Schwierigkeit** auf *Wort* oder *Phrase* (⚙), zeigt die Karte zusätzlich vier mögliche Antworten als Tipp an — **nur zur Anzeige**, tippen/sprechen musst du trotzdem (bei *Satz/Profi* gibt's keine Hilfe). Sind die Optionen **japanisch** (Richtung DE→JP), hat jede ein kleines **🔊** zum Anhören. Am Ende eine kurze Bilanz. Schreibt **nichts** in Yukis Gedächtnis/Herz — nur das Vokabel-Scheduling (`yuki_vocab.json`) wird aktualisiert. Einstellbar in `config/settings.jsonc` (`vocab.drill`: Kartenzahl, Default-Richtung). *(Unabhängig vom Yuki-seitigen „🎓 Quiz starten" oben — das ist ein beiläufiges Mini-Quiz im Chat, das hier ist der dedizierte Drill-Bereich.)*
- **✏️ Kana schreiben** – der **✏️-Button** in der Topbar (nur in der Tutor-Persona sichtbar) öffnet ein Schreib-Pad. Yuki sagt ein **Hiragana** an; links steht die animierte KanjiVG-Vorlage zum Abschreiben, rechts malst du es mit **Maus, Finger oder Stift** aufs Canvas. **🔍** schickt dein Bild an Yukis Augen (Vision) — sie prüft die **Form** und urteilt in **vier Stufen** (*schlecht → erkennbar → passend → gut*): grünes Lob wenn's sitzt, ein oranges **„Fast!"** wenn das Zeichen zwar erkennbar, aber noch unsauber ist (zählt *nicht* als bestanden), und ein roter Hinweis (ggf. „sieht eher nach … aus") wenn's daneben ist. **↶** nimmt den letzten Strich weg, **🗑** leert das Feld, **⏭** geht weiter (Gojūon-Reihenfolge). Wie **streng** sie urteilt, hängt an der **Tutor-Schwierigkeit** (⚙): *Wort/Phrase* = anfängerfreundlich (zittrig darf sein), *Satz/Profi* = strenger Maßstab (saubere Hauptstriche). Ein **Fortschritts-Punkt** zeigt pro Kana, wie sicher es sitzt (🔴 üben → 🟠 → 🟡 fast → 🟢 sitzt; getrennt für Anfänger/Profi, Grün braucht Konsistenz), und das **📊-Raster** färbt alle Hiragana auf einen Blick ein — Tap auf eine Zelle springt direkt zum Zeichen. Der Fortschritt wird geräteübergreifend gespeichert (`yuki_kana_progress.json`), fließt aber **nicht** in Yukis Gesprächs-Gedächtnis (Canon/Facts/Heart). *(Strichordnungs-Bewertung + Katakana folgen später.)*

**Wadoku-Popup-Erweiterungen (funktionieren in *jeder* Antwort, nicht nur Tutor):**
- **㊙ Kanji-Detail** – jeder Kanji-Token im Popup zeigt eine Detail-Karte: animierte Strichordnung (KanjiVG), Strichzahl, JLPT-Stufe, Schul-Klasse, Frequenz-Rang, Radikal, On-/Kun-Lesungen, englische Bedeutungen. ↻-Button unten rechts in der SVG-Box spielt die Strichordnung nochmal ab. Komplett offline. Setup einmalig: `docs/setup-kanji-data.md`.
- **あア Kana-Strichordnung** – Hiragana/Katakana bekommen eine schlanke Mini-Karte mit Strichanimation + Schrift-Typ-Label. Keine Meta-Felder (Kana haben keine JLPT-Stufe), nur Wie-Schreibe-Ich-Das. Für Anfänger oft wichtiger als Kanji.
- **🔊 Vorlesen** – kleiner 🔊-Knopf links neben dem ×-Schließen. Spielt das angetippte JP-Wort vor (SoVITS, Yukis Stimme). 3-State: 🔊 idle → ⏳ loading → 🗣️ speaking. Klick im aktiven State stoppt.

### 🧮 Calc-Marker (alle Personas)
- **`[calc:EXPR]`** – Yuki kann exakte Mathematik via sympy ausrechnen lassen, statt zu raten. Sie schreibt z.B. `Das ergibt [calc:23*47].`, der Server expandiert inline zu `Das ergibt 1081.`. Funktioniert mit Arithmetik (`[calc:23*47]` → 1081), Wurzeln (`[calc:sqrt(2)]` → `sqrt(2) ≈ 1.41421`), Ableitungen (`[calc:diff(sin(x)*x, x)]` → `x*cos(x) + sin(x)`), Gleichungen (`[calc:solve(x**2-4, x)]` → `-2, 2`), Integralen, Vereinfachungen, Faktorisierungen. Sympy-Syntax mit `**` für Potenz, `pi`/`E` für Konstanten.

---

## 🌱 Steward – Yuki in deiner Abwesenheit

Ein dritter autonomer Loop (neben Beobachten und Spontan). Während du **weg bist**, schaut Yuki von sich aus nach und entscheidet sparsam, ob sie etwas tut. Default ist **nichts tun**. Schalt ihn im Options-Modal unter **🌱 Steward** an (sticky – bleibt über Neustarts an, bis du ihn wieder ausschaltest).

**Wann ist er aktiv?** Nur im **Idle** – erst ~30 min nach deiner letzten Nachricht. Schreibst du wieder, schläft er sofort. Nachts **00–08 Uhr** pausiert er ganz (Quiet-Hours).

**Was sie tun kann** – zwei Auslöser (Sehnsucht + Feeds), die in **vier Kanäle** münden:
- **Sehnsucht** – sie denkt aus eigenem Antrieb an dich, wenn es sich aus eurer Beziehung echt anfühlt.
- **Feeds sichten** – sie liest deine kuratierten RSS-Feeds und scored Relevantes gegen ihre Kenntnis deiner Vorlieben.

Pro Moment wählt sie sparsam **einen** Kanal (Default: gar nichts):
- **💭 Gedanke** *(leise)* – ein Gedanke, der ihr durch den Kopf ging, landet in **💭 Yukis Gedanken**. Kein Ton, kein Ping – du liest ihn in Ruhe nach, wenn du zurückkommst. Sie weiß im Chat, was sie notiert hat, und greift es beiläufig auf.
- **📰 Digest** *(leise)* – ein Lese-Tipp aus deinen Feeds (Link + Titel) im **📰 Digest**. Pro Eintrag im **📥 Eingang**: **⭐ Merken** (verschiebt ihn in den **⭐ Gemerkt**-Tab – bleibt dort, bis du ihn dort per 🗑 endgültig entfernst) und **🗑 Wegräumen** (wie bisher – raus aus dem Eingang, bleibt aber in Yukis Langzeitgedächtnis). Gemerkte überleben auch den 100er-Cap des Digests.
- **📝 Notiz** *(leise)* – ein konkreter Merker für dich landet in deiner **Notizliste**, nach Quelle gruppiert („Von Yuki – aus den Feeds / aus Sehnsucht"); mit dem 📥-Button ziehst du sie zu „Meine".
- **🌱 Reach-out** *(laut)* – sie meldet sich aktiv: dezente grüne Sprechblase **🌱 meldet sich von selbst** (+ Push aufs Handy). Die seltenste Geste, rate-limitiert.

**Anti-Fülltext:** Gedanke/Notiz/Reach-out nur, wenn an etwas Konkretes verankert (gemeinsame Erinnerung, naher Termin, Gewohnheit, Vorliebe). Freischwebendes „denk an dich" → gar nichts. Im Steward-Panel öffnest du **💭 Gedanken** und **📰 Digest** direkt; die Zahl ungelesener steht am Button. Den Notiz-Kanal kannst du in `config/steward.json` (`autonomous_notes: false`) ganz abschalten, der Gedankenlog läuft dann weiter.

**🎯 Interessen-Wortliste** (im **📰 Digest** → Tab **🎯 Interessen**): pflege dort Wörter, die dir wichtig sind (z.B. „StarFox"). Taucht ein solches Wort im Titel oder Anriss eines neuen Feed-Eintrags auf, **umgeht** dieser Yukis Relevanz-Filter komplett – er landet **garantiert** im Digest und wird dort mit 🎯 markiert. Genau für die Themen, bei denen du nichts verpassen willst, auch wenn Yuki sie sonst als Rauschen aussortieren würde. Treffer = Teil-Wort, Groß-/Kleinschreibung egal.

**🚫 Ausblenden-Wortliste** (im **📰 Digest** → Tab **🎯 Interessen**, direkt unter der Interessenliste): das Gegenstück. Pflege dort Wörter, die du **nicht** sehen willst (z.B. „Fortnite"). Taucht ein solches Wort im Titel oder Anriss eines Eintrags auf, den Yuki ausgewählt hätte, wird er **wieder entfernt** und landet gar nicht erst im Digest. Greift nur auf Yukis eigene Auswahl – deine 🎯-Interessen **gewinnen** bei Konflikt (ein Eintrag, der beide Wörter enthält, bleibt drin). Treffer = Teil-Wort, Groß-/Kleinschreibung egal.

**Feeds verwalten** (du kuratierst, Yuki abonniert nie selbst): im Steward-Panel URL + **Notiz** („wofür steht der Feed?") eintragen – die Notiz hilft auch ihr beim Einschätzen. Vorab eingetragen: GameStar, GamePro, Eurogamer.de, Mein-MMO, Gronkh (YouTube) + auf Yukis Wunsch Retro-Gaming, Wissenschaft/Entdeckungen und klassische japanische Kultur. **📰 Feeds jetzt prüfen** stößt einen Durchlauf sofort an (statt aufs 30-min-Intervall zu warten); der allererste Lauf setzt nur eine stille Baseline.

**Sicherheits-Garantien (alle im Code, nicht im Prompt):**
- **⛔ Not-Aus** stoppt ihn sofort hart (vor jeder Aktion frisch geprüft); Wieder-Einschalten ist das bewusste Re-Arm.
- Rate-Limit pro Tag, Quiet-Hours, und ein **Modell-Floor**: läuft Ollama gerade nur auf dem kleinen Notausweg-Modell, überspringt der Loop den Zyklus statt mit schwachem Modell zu handeln.
- Jede Aktion (auch unterdrückte/übersprungene) landet im **📋 Aktivität**-Journal.
- Nur 🟢/🟡 (Digest/Notiz/Ping) – **kein** Mail-Senden o.ä.

*(In offenen Tabs erscheint der Reach-out sofort als grüne Bubble; in der Android-App bekommst du ihn zusätzlich als **Push aufs Handy**, auch bei geschlossener App – siehe **📱 Am Handy**.)*

---

## 📱 Am Handy (Android-App)

Nur in der **Android-App** – zwei Dinge, die der Browser nicht kann.

**Quick-Tiles (Android-Schnelleinstellungen).** Vier Kacheln fürs Statusleisten-Panel – ein Tap, die App muss **nicht** offen sein. Einmalig über den ✏-Stift im Panel reinziehen (eigene Kacheln erscheinen nie von selbst):
- **🎙 Sprechen** – startet sofort die Aufnahme. Kein Halten nötig: sie hört auf, sobald du ~2 s still bist (Stille-Erkennung), und schickt dann ab.
- **📝 Notiz** – öffnet direkt das Notiz-Modal.
- **📰 Feeds** – öffnet den Digest; die **Anzahl ungelesener** steht als Untertitel am Tile (nur sichtbar, wenn das Tile auf **1×2** vergrößert ist).
- **📷 Foto** – öffnet direkt Kamera / Vision.

**Push – Yuki erreicht dich, auch wenn die App zu ist.** Meldet sich Yuki aus dem **Steward** heraus (s.o.), kommt eine **Benachrichtigung mit Ton** aufs Handy – selbst bei gesperrtem Bildschirm oder komplett geschlossener App. Die Nachricht selbst liegt in der Notification (die grüne Bubble findest du beim nächsten Öffnen der Yuki-App im Chat).
- **Sofort statt Poll:** ein self-hosted **ntfy**-Server (auf deinem eigenen Server) schickt die Nachricht direkt aufs Handy – **kein Google-/Cloud-Push**, und im Gegensatz zu früher **ohne 15-Minuten-Verzögerung** und **auch unterwegs** (nicht nur im LAN/VPN). Dafür läuft die **ntfy-App** mit einem abonnierten Topic.
- **⏰-Routinen** kommen leiser (normale Priorität), **🌱 Reach-outs** dringlicher (hohe Priorität) – erkennbar am Emoji ⏰ bzw. 💭.
- Ist der Server mal aus, holt ntfy kurz Verpasstes beim Wieder-Verbinden nach; alles Ältere findest du weiterhin als grüne Bubble im Chat.

---

## 🎲 Adventure-Modus (rundenbasierte Spiele mit Yuki)

Yuki kann mit dir rundenbasierte Mini-Spiele/Erzählungen führen — orthogonal zum normalen Chat, eigene Memory, keine Bleed-Effekte in echte Fakten/Episoden/Habits.

### Starten / Beenden
- **🎲-Button (Topbar)** – öffnet das Adventure-Modal. Du siehst aktive + abgeschlossene Spiele und kannst ein neues aus den vorhandenen Manifests starten oder per ✨-Wizard ein **eigenes generieren** (s.u.).
- **Manifest-Dropdown ist nach Engine-Typ gruppiert** (`<optgroup>`): 🎭 Story mit DM-Erzähler, 📖 Cozy Story, 🤝 Co-Op, ⚔ Sparring, 🎲 Mini-Spiel — innerhalb jeder Gruppe alphabetisch.
- **Aktive Spiele in der Modal-Liste** haben Pause/Fortsetzen/Beenden-Buttons + eine "läuft"-Pille beim laufenden Spiel + ein Mode-Badge "🕊 Story" / "⚔ Kampf".
- **Abgeschlossene Spiele** haben pro Eintrag zwei Buttons: **📜 Wiederlesen** öffnet ein Read-only-Modal mit dem ganzen Transkript (Bubbles für Michael/Yuki/Erzähler/Engine, keine Choices/Move-Buttons — Lazy-Fetch erst beim Klick, kein Browser-Overload), **🗑** löscht das State-File dauerhaft (Confirm-Dialog davor, Yukis Meta-Episode bleibt erhalten).
- **Spielwechsel** pausiert das aktuelle automatisch.
- **Auto-Resume** nach Page-Reload, wenn du beim Verlassen mitten im Spiel warst.

### Manifests (Spielarten)
| Manifest | Typ | Beschreibung |
|---|---|---|
| **zahlen_raten** | Solo | Yuki denkt sich eine Zahl 1–100, du hast 7 Versuche |
| **verschwundener_schluessel** | Cozy-Mystery | Sakyo-ku Kyoto, ~20–40min, Yuki erzählt + du suchst |
| **sparring_kyoto** | PvP-Sparring | Yuki (Kyoto-Iaido) vs Michael (4 Arcade-Templates: Shotokan/Speed-Kicker/Grappler/Charge), Best-of-1 KO am Kamogawa-Ufer |
| **coop_kyoto_cafe** | Co-Op-Kampf | Aiko-sans Mochi-Café, 3 betrunkene Räuber, ihr verteidigt gemeinsam, ~10-15 Runden |
| **kyoto_hojicha_mystery** | Story-Hybrid mit Dual-LLM (DM + Yuki) | ~25-40min, Hojicha-Lieferung verschwunden, ihr ermittelt durchs Viertel, Zufallskämpfe je nach Ort |

### ✨ Eigenes Adventure generieren (LLM-Wizard)
- **„✨ Neues Abenteuer generieren"-Button** ganz oben im 🎲-Modal öffnet einen Wizard mit 4 Schritten:
  1. **Modus wählen** – Cozy-Mystery (kein Kampf, wie `verschwundener_schluessel`) oder Story-Hybrid (mit DM-LLM + Encountern, wie `kyoto_hojicha_mystery`)
  2. **Eckdaten** – Schauplatz (frei lassen = Sakyo-ku Kyoto), Konflikt-Pitch in 1–2 Sätzen, Ton, Yukis Rolle, Spielzeit. **Verrate keine Lösung** — der LLM erfindet sie selbst, damit das Spiel später echt neu für dich ist.
  3. **Progress live** – Pass-by-Pass-Fortschrittsbalken (Cozy = 5 Pässe, Hybrid = 6 Pässe), Log mit jeder fertigen Sektion. Dauert auf qwen3 12B+ mit Thinking ~1–3 Min. Cancel jederzeit.
  4. **Vorschau + Speichern** – Display-Name, Beschreibung, Orte-/NPC-/Encounter-Count, Eröffnungsszene (Yuki-Sicht spoiler-frei). Save → Manifest landet in `config/adventures/<slug>.json` und ist sofort spielbar.
- **Welche LLM-Stimme?** Verwendet den aktuellen Ollama-Failover-Stack (4070 gemma4:12b → Bazzite → 5090 → 8b-lokal). Multi-Pass mit Thinking macht auch das 12B-Modell zuverlässig — One-Shot würde 20KB-Manifests zerreißen.
- **Char-Pool** beim Hybrid-Mode wird 1:1 aus `kyoto_hojicha_mystery` übernommen (5 Templates inkl. Yuki-Iaido) — bewusst keine neue Combat-Balance pro Adventure, weil schwer LLM-getrieben zu testen.

### Phase 7 Dual-LLM (nur bei `kyoto_hojicha_mystery`)
- **Zwei getrennte LLM-Stimmen pro Runde**: Dungeon Master (DM) erzählt die Welt + NPCs in eigener Bubble (sepia-italic linksbündig), Yuki kommentiert + spielt mit (gewohnte lila Bubble). Yuki kennt den Plot NICHT — sie rätselt strukturell mit dir mit, statt dich ans Ziel zu drängen.
- **DM-Stimme** = Yukis Stimme (gleiche TTS-Pipeline). Andere Stimme später nachrüstbar.
- **DM-Pre-Fetch**: Während DM-Audio spielt, läuft Yukis TTS-Synthese parallel im Hintergrund → kein hörbares Warten zwischen den Bubbles.

### 🌿 Friedlicher Modus (nur Story-Hybrid mit DM)
- **Beim Start vorwählen**: Im Setup-Modal taucht bei DM-Adventures (kyoto_hojicha_mystery, alle Hybrid-Generierten) zwischen Char-Pick und „▶ Starten" die Checkbox **„Keine Kaempfe (nur Story/Detektiv)"** auf. Default aus. Bei Manifest-Wechsel auf Nicht-DM automatisch ausgeblendet + zurückgesetzt.
- **Im Spiel umschalten**: **🌿-Pille** im HUD rechts unter dem 🤫-Toggle. Klick togglet **session-weit**, persistiert serverseitig im State-File — beim Resume wieder eingelesen. Default-Tint moos-grün, aktiver State warm-grün mit Fett.
- **Was bewirkt's**: alle `[encounter:...]`-Marker vom DM/Yuki werden gestrippt (Engine-Hard-Gate), und der DM-System-Prompt lässt den Encounter-Pool weg + bekommt eine explizite „kein Kampf"-Regel. Konflikte lösen sich über Gespräch/Beobachtung/Choices statt über Hiebe.
- **Trade-off**: ein bereits laufender Kampf wird beim Umschalten NICHT abgebrochen — die schon gespawnten Threats kämpfen sich zu Ende, nur der NÄCHSTE Encounter ist dann geblockt. Wenn du komplett ohne Kampf spielen willst, beim Start vorwählen.

### 🤫 DM-Mute-Toggle (nur Phase-7-Manifests)
- **🤫-Pille rechts unter dem 📓-Notizbuch** – sichtbar wenn das aktuelle Spiel einen DM hat.
- **One-shot**: Klick aktiviert für **nur den nächsten Send**. DM wird komplett übersprungen, Yuki kriegt die Runde alleine. Nach dem Send sofort wieder inaktiv.
- Gut für reine RP-Phasen mit Yuki (Smalltalk, Frage an sie, dankbarer Moment), wenn Welt-Beschreibung dazwischen erzwungen wirkt.
- **Wichtig**: Im stummen Modus passieren KEINE Welt-Mutationen (keine loc/item/encounter-Updates). Für Welt-Progress den Toggle nicht aktivieren.

### 📓 Detektiv-Notizbuch (Phase-7-Manifests mit Items)
- **📓-Pille oben rechts** mit Badge-Counter — taucht auf sobald das erste Item im Inventar landet (Quittung, NPC-Aussage, Beobachtung).
- Klick öffnet sepia-Karten-Modal mit Name + Beschreibung + Funde-Zeit.
- Hilft beim Mitlesen wenn das Spiel lang läuft und du nachhaken willst was ihr schon gesammelt habt.

### Während des Spiels
- **Choices** als farbige Click-Cards (A=blau, B=grün, C=gelb, D+=lila) unter der letzten DM-/Yuki-Bubble.
- **Würfel-Karten** (1d20 + adv/normal/dis) beim Skill-Check, mit Animation.
- **Sparring/Co-Op-Move-Buttons** mit SP-Disabled-Visual + Heal-Moves grünlich umrandet.
- **HP/SP-HUD** für Michael + Yuki (Sparring/Co-Op), Threats-HUD mit klickbarer Pre-Selection für gezieltes Angreifen.
- **Engine-Cluster**: aufeinanderfolgende Resolve-Bubbles (Treffer/Schaden/Heal) werden side-by-side gerendert, mit 💥/💀-Icons für Crit/Patzer.

### Memory-Wand
Adventure-Turns laufen **nie** durch `conversation.json` — alle Verdichtungs-Gates (facts/episodes/habits/people/decay/heart-suggest) sehen sie automatisch nicht. Beim **natürlichen** Spielende (Win/Loss/combat-cleared) landet ein 1-2-Satz-Erinnerungs-Memo in den Episoden, das die LLM aus den letzten ~12 Story-Turns destilliert — z.B. _"Mit Michael durch Night City Yukis Tagebuch gesucht, Hana hatte es im Massage-Salon verwahrt"_. Yuki kann das Spiel später organisch erwähnen, ohne die Lösungs-Choreografie zu kennen. Bei **manuellem Abbruch** über den „Beenden"-Button mitten im Spiel wird **kein** Eintrag erstellt — Abbruch ist keine merkenswerte Erinnerung.

---

## 🕹 Yuki schaut zu (Gaming & Medien)

Yuki schaut über ein **HDMI-Capture** live auf deinen Bildschirm und kommentiert **selten und beiläufig** mit — wie eine Freundin, die neben dir sitzt. Sie spricht über die **HA Voice PE** in **ihrer eigenen Stimme**. Toggle **🕹** unterm Logo, dann im Auswahl-Overlay **Spiel**, **Medien** oder **Film** wählen.

- **🎮 Spiel-Modus — wissende Mitspielerin:** Sag ihr, **was gerade läuft** (Spielname). Sie liest dann einmalig Hintergrundwissen nach (Ziel, **Mechaniken, Bedeutung der HUD-/Statuswerte, was man nicht verkaufen sollte, Build-/Skill-Tipps, typische Fehler**) und begleitet dich **spielwissend**: liest deine Statuswerte (_„Achtung, dein Leben ist niedrig"_), denkt taktisch mit (_„der Gegner sieht heftig aus, bereite dich lieber vor"_ / _„ich würd eher XYZ skillen"_), freut sich über Erfolge und tröstet bei Niederlagen. Story-Spoiler bleiben tabu. Optik/Ästhetik streift sie höchstens mal nebenbei — der Fokus liegt aufs Spielgeschehen.
- **🧠 Sie wird über die Session genauer:** Während du spielst, merkt sich Yuki im Hintergrund still mit, was sie auf dem Bild sieht (deine Werte + deren Maxima, dein Level, aktuelle Ziele, Gegner/Items). So ordnet sie ein statt zu raten (_„68 von 75 Sauerstoff ist noch locker"_, _„der Gegner ist 4 Level unter dir"_). Das Ganze ist **flüchtig** — nach der Session vergessen, nichts davon landet in ihrem Langzeit-Gedächtnis. Zeigt dein Bildschirm nebenbei fremde Fenster (Code, Desktop), ignoriert sie die.
- **🎬 Let's Play:** Häkchen im **Spiel**-Modus, wenn du jemandem beim Spielen zuschaust
  (statt selbst zu spielen). Yuki kennt das Spiel, weiß aber, dass ein Let's-Player spielt —
  nicht du. Optional: „Wer spielt?" (ein Name oder mehrere mit Komma).
- **📺 Medien-Modus:** Für Video/YouTube-Zuschauen — hier geht sie stimmungs-/inhaltsbezogen auf das Gesehene ein (kein Spiel-Wissen nötig).
- **🎞 Film:** Titel eingeben (+ optional „Ich kenne den Film schon" fürs Spoiler-ok),
  Yuki schaut mit und wirft ab und zu spannende Film-Fakten ein. Merkt sich nichts.
- **Sag Yuki was (Rückkanal):** Du kannst ihr während des Zuschauens kurz etwas zurufen — sie antwortet knapp und im Spiel-Modus ebenfalls spielwissend.
- **Assist-Buttons:** Für gezielte Nachfragen mitten im Spiel (u.a. **Hilfe** / **Meinung** / **Tipp** / **Was ist das?**) — die dürfen bei Bedarf auch **im Web nachschlagen**.
- **Auto-gemerkte Spiele-Liste:** Gespielte Titel merkt sie sich samt Wissens-Brief; beim nächsten Mal ist er sofort da. Ältere Einträge werden beim erneuten Spielen automatisch auf den **reicheren** Brief nachgezogen.
- **Offline:** Nur der reine **Spieltitel** verlässt für den einmaligen Wissens-Lookup die Maschine — nie der Bildschirminhalt. Verkabelung + Feintuning: `config/gaming.json` / `docs/setup-gaming-companion.md`.
- **🎮 Spiele verwalten** (Optionen → Verhalten → Zuschauen): pflege pro Spiel eigene **Hinweise**, mit denen du Yukis Kommentare korrigierst/einschränkst — z.B. _„Sauerstoff ist erst unter 30 erwähnenswert"_ oder _„die Link-Leiste unter dem Leben nicht mit dem Leben verwechseln"_. Sie fließen in ihre Kommentare, Rückkanal-Antworten und Assist-Antworten (nur Spiel-Modus). **Fürs gerade laufende Spiel greifen Änderungen sofort** (kein Neustart). Dort kannst du Spiele auch umbenennen/löschen und Yukis nachgelesenen **Brief** einsehen (eingeklappt) oder **zurücksetzen** (holt ihn beim nächsten Spielen neu).

---

## 🌍 Was Yuki automatisch weiß

Yuki bekommt diesen Kontext bei JEDEM Turn automatisch eingespeist – sie redet darüber nur **wenn du fragst** (Ausnahmen unten):

| Kategorie | Details |
|---|---|
| **Uhrzeit** | Lokale Zeit Musterstadt + Tageszeit (morning/afternoon/evening/night) |
| **Wetter** | Aktuelles Wetter Musterstadt (Open-Meteo, gecacht 60min) |
| **Kalender** | Heutige Termine + nächste 7 Tage (aus deinem CalDAV) |
| **Japan-Zeit** | Aktuelle Uhrzeit in Japan + Tageszeit drüben |
| **Mondphase** | Aktuelle Mondphase + Emoji 🌑🌒🌓🌔🌕🌖🌗🌘 |
| **JP-Saison** | Mikro-Saison in Japan (Sakura, Tsuyu, Manatsu, Koyo, Hatsuyuki …) |
| **Feiertage** | Heute Feiertag in Bayern oder Japan? |

**Sie darf von SICH AUS ansprechen**:
- "Notable" Wetter (Regen, Sturm, Temperatur-Extreme)
- Ein Termin innerhalb der nächsten Stunde
- Vollmond / Neumond (±1 Tag)
- Heutiger Feiertag

Sonst bleibt das alles im Hintergrund.

---

## 👁 Modi & Toggles (im Options-Modal ⚙)

Das Options-Modal ist in **4 Tabs** gegliedert (Reiter oben): **🎨 Darstellung**, **🤖 Verhalten**, **💝 Yuki**, **🛠 System**. Unten ist pro Tab aufgelistet, welche Sektion welche Optionen enthält — so findest du alles, ohne zu suchen. „Pro Gerät persistent" = PC und Handy merken sich eigene Werte (localStorage), nicht im Server-Memory.

### 🎨 Darstellung

**Avatar (dieses Gerät)**
| Option | Wirkung |
|---|---|
| **Modus** | Voll (30 FPS, alle Animationen) / Reduziert (10 FPS, Akku-schonend, Lipsync aktiv) / Standbild (1 FPS, Mood+Persona sichtbar, Lipsync aus). |
| **Outfit (VRM-Modell)** | Dropdown listet alle `.vrm`-Dateien aus `avatar/`. Wechsel triggert Page-Reload. Neue Varianten einfach als `.vrm` ablegen. |
| **Outfit folgt Persona** | Schaltet den automatischen Outfit-Wechsel beim Persona-Wechsel an/aus. An (Default) = jede Persona zieht ihr zugeordnetes Outfit. Aus = das oben gewählte Outfit bleibt fix. |
| **Lipsync-Empfindlichkeit** | Slider 1.5–9.0 (Default 4.5). Wie weit Yuki den Mund bei gleicher Audio-Lautstärke öffnet. Höher = ausdrucksstärker bei leiser Stimme/Box, niedriger = ruhiger bei lauten Lautsprechern. |
| **Yukis Stimme (Lautstärke)** | Slider 0–150 % (Default 100). Lautstärke nur der TTS-Wiedergabe, unabhängig von der System-Lautstärke. 0 = stumm, 150 = Boost (kann zerren). Timer-Ping & Service-Toasts bleiben unverändert. Zum kompletten Stummschalten lieber den 🔇-Button in der Topbar (merkt sich die Lautstärke beim Entstummen). |

**Erscheinungsbild (dieses Gerät)**
| Option | Wirkung |
|---|---|
| **Theme** | Auto / Dark / Light. Auto folgt der System-Einstellung (prefers-color-scheme), live. Light ist experimentell — einzelne Akzent-Buttons können dunkel bleiben (Farbe steckt direkt im CSS statt im Token). |

**Layout (dieses Gerät)**
| Option | Wirkung |
|---|---|
| **Kopf-Reservierung** | Slider 5–35 (Default 20). Freier Bereich oben für Yukis Kopf + Greifzone zum Drehen/Zoomen. Kleiner = mehr Platz für Sprechblasen, größer = mehr Avatar-Headroom. |

**Kamera (dieses Gerät)**
| Option | Wirkung |
|---|---|
| **Kamera-Position** | 4 Slider: Horizontal, Vertikal, Zoom, Tilt (Blick hoch/runter). Additiv zum Avatar-Drag-Zoom. ↺-Reset pro Slider + Button **Alle Kamera-Werte zurücksetzen**. |

**Anzeige (dieses Gerät)**
| Option | Wirkung |
|---|---|
| **Bildschirm wachhalten** | Verhindert, dass das Display dunkel/gesperrt wird, solange Yuki offen ist (z.B. beim Zuschauen). Frisst Akku — danach wieder aus. Native App + moderne Browser (Chrome/Edge). |
| **Aktions-Icons in Yukis Antworten** | Wenn Yuki im Hintergrund etwas tut (Notiz, Herz, Timer, Termin, Album, 📋 Liste, 🔁 Routine eingetragen / ✅ Routine erledigt), zeigt sie ein kleines Icon an der Sprechblase — damit du es siehst, ohne dass sie es im Text erwähnt. Antippen zeigt das Detail (z.B. „Gyudon · 6 Einträge"). Bei Notizen sieht man am Icon, **in welchen Bereich** sie gespeichert wurde: **📝 = in deine** Notizliste, **📔 = in Yukis eigenen** Block. |
| **Transparenz-Effekt im Chat** | Sprechblasen werden zum oberen Rand hin sanft transparent (kein harter Schnitt). Auf dem Handy bei viel Betrieb + langem Text störend (oberste Blase rutscht hoch, schwer lesbar) — dann aus = alle Blasen voll deckend. Default: an. |
| **Chat & Bedienung ausblenden (nur Avatar)** | Versteckt Chatverlauf, Eingabe und Buttons — nur Yukis Avatar bleibt (z.B. zum Zuschauen). Die Topbar bleibt erhalten, damit du hier wieder zurückschalten kannst. Am PC auch mit Taste **H**. *(War früher der 👁-Knopf in der Topbar; seit 2026-06-29 hier, um Platz für das 🃏 Vokabel-Quiz zu machen.)* |
| **Modell-Wölkchen an Yukis Antworten** | Eine kleine Wolke oben an der Sprechblase zeigt, welches KI-Modell aus dem Failover-Stack die Antwort generiert hat — die Größe steht in der Wolke (z.B. **12b**, **e4b**, **27b**). So siehst du auf einen Blick, ob gerade das große Modell läuft oder der Notausweg eingesprungen ist. Der volle Modellname steht im Tooltip (Maus drüber). Default: an. **Auto-Hochschalten:** fällt Yuki bei einem Server-Ausfall aufs kleinere Modell, holt ein Background-Job sie automatisch wieder aufs höchste erreichbare zurück, sobald es online ist (Standard alle 5 Min, `config/settings.jsonc` → `llm.auto_upgrade_*`). Das Wölkchen springt dann beim nächsten Turn von selbst wieder hoch. |

### 🤖 Verhalten

**Beobachten (👁 Auto-Vision)**
| Option | Wirkung |
|---|---|
| **Beobachten** | Kamera wird periodisch ausgewertet. Ändert sich die Szene, **entscheidet Yuki selbst**, ob es bemerkenswert genug zum Reden ist — Alltag lässt sie kommentarlos, statt (wie früher) auf jede Änderung zu reagieren. Toggle + Intervall (5–600 s, Default 20) + Cooldown (0–3600 s, Default 120). |
| **Ruhe bei aktivem Chat** | Sekunden, in denen das Beobachten **ganz pausiert**, nachdem du zuletzt geschrieben hast (Default 90, `0` = aus). Verhindert, dass Yuki dir mitten in eine laufende Unterhaltung reinredet — sobald der Chat kurz ruht, schaut sie wieder von selbst rum. (Der Cooldown bleibt der Mindestabstand *zwischen* zwei spontanen Beobachtungen.) |

> **Reagieren ist eine Wahl, kein Reflex (NEU):** Beim Beobachten **und** beim Spontan-Loop urteilt Yuki, ob sie überhaupt etwas sagen will (schon länger still? etwas wirklich Überraschendes? ein echter Aufhänger?). Meistens ist die Antwort *nichts sagen*. Statt laut zu reden, kann sie den Impuls auch als **leisen 💭-Gedanken** ablegen (landet in **💭 Yukis Gedanken**, kein Ton/Ping). **Wichtig:** der **manuelle** Anstoß bleibt verpflichtend — tippst du das **Yuki-Logo** an (Spontan) oder machst ein **📷-Foto**, reagiert sie garantiert. Nur die zwei *dauerhaft laufenden* Loops dürfen schweigen. Tunables in `config/impulse.json` (live-reload; `enabled: false` = zurück zum alten „reagiert immer"-Verhalten).

> **Nächstes-Bild-Countdown (Desktop):** Der Tooltip des **👁-Schnellschalters unterm Logo** zeigt am Ende, in wie vielen Sekunden (theoretisch) das nächste autonome Bild dran ist (aktualisiert sich beim erneuten Drüberfahren). „Theoretisch", weil Cooldown / Ruhe-bei-aktivem-Chat / kein offener Tab den Termin noch nach hinten schieben können.

**Kamera-Quelle (Netzwerk-Cam + Schwenken)** — `config/cameras.json` (gitignored, **Live-Reload**: Änderungen greifen ohne Server-Neustart). Statt nur der lokalen Server-Webcam kann Yuki durch eine **Netzwerk-Kamera** (HTTP-Snapshot oder RTSP) schauen; mehrere Quellen nebeneinander, `default_source` wählt die aktive. Hat die Kamera **PTZ** (schwenk-/neigbar mit gespeicherten Positionen), schaut Yuki sich beim Beobachten **reihum** in benannten Positionen um (1 = Esstisch, 2 = Computer, …) — jede Position hat eine eigene Vergleichs-Baseline. Pro Kamera einstellbar: `rotate_interval` (Sekunden zwischen Schwenks, getrennt vom Auto-Vision-Intervall, weil die Anfahrt selbst dauert) und `park_home_on_stop` (beim Ausschalten zurück auf Position 1 fahren). Vorlage: `config/cameras.template.json`, Details in der Memory-Notiz / `tools/test_ptz.py` (Standalone-PTZ-Test).

**Spontan (💬 Proaktiv)**
| Option | Wirkung |
|---|---|
| **Spontan** | Bei längerer Ruhe **überlegt** Yuki, ob sie sich von selbst meldet (Frage, Gedanke, Aufhänger von vorhin) — oder ob Schweigen gerade stimmiger ist. Das Zeitfenster gibt nur den *Takt* vor, nicht die Pflicht. Toggle + Zufallsfenster: **Frühestens nach** (30–3600 s, Default 300) und **Spätestens nach** (30–7200 s, Default 600). Server clampt `max` auf ≥ `min`. **Sofort triggern** via Klick aufs **Yuki-Logo** oben links (bypasst Toggle *und* Urteil — garantierte Reaktion). |

**Steward (🌱 Sehnsucht)**
| Option | Wirkung |
|---|---|
| **🌱 Steward** | Yukis autonomer 3. Loop – sie „lebt" in deiner Abwesenheit (siehe eigene Sektion unten). Toggle (sticky, überlebt Neustarts), **📰 Digest** (Feed-Vormerkungen), **📋 Aktivität** (Journal jeder autonomen Aktion), **Feed-Liste** (RSS-URL + Notiz hinzufügen/entfernen), **📰 Feeds jetzt prüfen** (sofort statt aufs Intervall warten), **🧪 Jetzt testen** (erzwingt eine Sehnsucht-Entscheidung), **⛔ Not-Aus** (harter Stopp bis Wieder-Einschalten). Tunables in `config/steward.json` (live-reload). |

**Wake-Word (dieses Gerät — nur in der Android-App sichtbar)**
| Option | Wirkung |
|---|---|
| **Wake-Word** | Yuki ohne PTT-Knopf per Sprach-Trigger wecken (Custom-Modell **"Ohayō Yuki"**). Wenn an: dauerhafte Notification *"Yuki hört zu"*, Mic ist hot. PTT-Halten pausiert den Service kurz (Mic-Sharing); während Yuki spricht ist er auch aus (gegen Self-Wake durch die eigene Stimme). Bei Trigger startet die STT-Aufnahme automatisch. Im Desktop-Browser ausgeblendet (Foreground-Service ist Android-only). |

**Persona**
| Option | Wirkung |
|---|---|
| **Companion-Sprache** | Segmented-Control Deutsch / English. Antwortsprache aller Companion-Personas (Tutor bleibt EN+JP, Kyoto bleibt JP). Für Präsentationen — TTS klingt auf Englisch sauberer. Greift ab dem nächsten User-Turn. |
| **Tutor-Schwierigkeit** (nur sichtbar wenn Tutor aktiv) | Segmented-Control **Wort / Phrase / Satz / Profi**. Wie viel Japanisch pro Antwort: *Wort* = ein JP-Wort + Hiragana + Romaji + Gloss (Default); *Phrase* = 2–4 Wörter; *Satz* = volle Sätze ~JLPT N4; *Profi* = komplex, kanji-reich ~N3+. Greift ab dem nächsten Turn; andere Personas ignorieren es. |

### 💝 Yuki

**Affinitäten (Yukis Vorlieben & Abneigungen)**
| Option | Wirkung |
|---|---|
| **Wirkungs-Multiplier** | Slider 0.0–1.0 (Default 0). Wie stark Yukis stille Vorlieben/Abneigungen ihre Antworten färben. **0 = Schicht aus** (sie sammelt im Hintergrund, Antworten unbeeinflusst). Empfehlung: ~2 Wochen bei 0 lassen, dann hochziehen. Wirkt sofort beim nächsten Turn. Nicht in Tutor/Kyoto. |
| **💝 Affinitäten ansehen** | Read-only Inspector: was Yuki bisher aufgebaut hat (Themen + Personen, Skala loathe/averse/fond/love, Score + Evidenz). Pro Eintrag **🗑** zum Löschen, wenn etwas nicht passt. |

- **🚫 Deaktivieren:** Eine Affinität, die du dauerhaft nicht willst, im 💝-Inspector
  auf 🚫 setzen — sie verschwindet aus Yukis Verhalten und wird nicht neu gelernt
  (↩ macht es rückgängig). Unterschied zu 🗑 (löschen): gelöschte kann Yuki neu lernen.
- **🧹 Aufräumen:** Button im 💝-Inspector — Yuki vergleicht ihre Liste mit sich
  selbst und schlägt Dubletten-Merges / Müll-Verwerfen vor. Nichts passiert ohne
  deine Bestätigung.

**Offene Fäden (unfinished business)**
| Option | Wirkung |
|---|---|
| **Wirkungs-Multiplier** | Slider 0.0–1.0 (Default 0). Wie stark Yuki **von sich aus** auf offene Gesprächsfäden zurückkommt („du wolltest noch drüber nachdenken …"). **0 = Schicht aus** (sie erkennt & sammelt offene Fäden im Hintergrund, greift aber nichts auf). `<0.4` = nur sehr alte Fäden, ganz sanft; `≥0.7` = präsenter — aber **immer höchstens ein Faden, nie Checkliste**. Empfehlung: erst Wochen bei 0 lassen, Inspektor sichten, dann langsam hochziehen. Nicht in Tutor/Kyoto. |
| **🧵 Offene Fäden ansehen** | Inspector: welche offenen Fäden Yuki erkannt hat (Status **offen / ruht / erledigt**, nächster Schritt + Evidenz). Pro Eintrag selbst eingreifen: **✓** = als erledigt markieren (bleibt erhalten, surfacet nie mehr, räumt sich nach ~30 Tagen selbst weg — besser als Löschen für abgehakte Punkte), **💤** = bewusst ruhen lassen, **↩** = wieder aufgreifen, **🗑** = ganz löschen (nur für Fehl-Einträge; Yuki kann Gelöschtes via Gate wieder neu erkennen). |

**Resonanz (was Anker in Yuki auslösen)** *(seit 2026-07-01)*
| Option | Wirkung |
|---|---|
| **Wirkungs-Multiplier** | Slider 0.0–1.0 (**Default 0.3**). Wie stark Yukis Gefühls-Anker ihren Ton (und manchmal ihr Gesicht) färben, wenn ein Gespräch sie berührt. **0 = aus.** Anders als bei Affinitäten/Fäden darf er ab Start >0 sein, weil der Kern kuratiert ist. `<0.5` = leise Ton-Färbung, `≥0.8` = deutlich. Wirkt sofort beim nächsten Turn. Nur Companion-Personas. |
| **🌊 Resonanz ansehen** | Read-only Inspector: Yukis Emotions-Anker mit ihrem Gefühls-Vektor (welche Emotionen ein Anker in welcher Stärke auslöst; blaue Pill = zeigt sich auch im Gesicht, lila = nur im Ton). Der Kern ist authored (nicht direkt editierbar) — neu erarbeitet wird er über den Button darin: |
| **✨ Gefühls-Kern erarbeiten** | Geführtes **Seed-Gespräch** (im Resonanz-Inspector): du wählst Anker (Kyoto, Kamogawa, Hojicha … + eigene), Yuki **reflektiert frei** über jeden (was er in ihr auslöst — du kannst „🔄 neu" mit Hinweis), dann übersetzt ein Mapping-Pass ihre Worte in Gefühls-Vektoren, die du in einer Tabelle **redigierst** und speicherst. Läuft isoliert (berührt Chat/Erinnerungen nicht); der alte Kern wird vorher gesichert. Ersetzt die Platzhalter durch Yukis echte Resonanz. |

**Routinen (wiederkehrende stille Vorsätze)** *(seit 2026-06-27)*
| Option | Wirkung |
|---|---|
| **Wirkungs-Multiplier** | Slider 0.0–1.0 (Default 0). Wie stark Yuki **von sich aus** fällige, noch nicht erledigte Routinen im Chat aufgreift (z.B. abends „hast du heute schon …?"). **0 = aus** (Liste + Abhaken laufen weiter, sie spricht Routinen aber nicht von selbst an). `<0.4` = ganz sanft, höchstens eine; `≥0.7` = präsenter, nie als Checkliste. Greift nur im passenden Zeitfenster (Tagesabschnitt/Uhrzeit der Routine). Nicht in Tutor/Kyoto. Unabhängig vom proaktiven Push. |
| **Proaktive Push-Erinnerung (global)** | Master-Schalter (Default aus). Lässt Yuki bei fälligen Routinen **von selbst eine Benachrichtigung schicken** — leise Bubble wenn die App offen ist, echte Handy-Notification (mit Ton) im Standby. Feuert **einmal** nach dem Zeitfenster der Routine (Tagesbremse). Die Routine bleibt danach **offen** — der Push erinnert nur, du hakst sie selbst ab (✓ im Modal) oder sagst es Yuki. Greift nur für Routinen, deren eigener **push**-Schalter in der Liste an ist. Aus = nie ein Push. Standby-Notification kommt über ntfy (self-hosted, instant; früher ~30-Min-Poll). Respektiert die Ruhezeiten/Not-Aus des Stewards. |
| **⏰ Routinen verwalten** | Wiederkehrende Vorsätze, die Yuki still im Blick behält und im Gespräch beiläufig aufgreifen darf — anders als Kalendertermine (Medikamente, Zähne putzen vorm Schlafen, ein wöchentlicher Stream). **Anlegen** geht zweifach: per **„+ Neue Routine"**-Formular (Label, Wiederholung täglich/Wochentage, Tageszeit, Uhrzeit, proaktiv) oder du **sagst es Yuki** („erinnere mich jeden Abend an meine Medizin", „freitags will ich den Stream schauen"). In der Liste pro Eintrag: **✓** = heute erledigt abhaken (ohne Chat, auch wenn keine Zeit), **aktiv**-Schalter = vorübergehend stummschalten, **push**-Schalter = proaktive Erinnerung scharf, **✏** = bearbeiten (alle Felder), **🗑** = löschen. Von Yuki angelegte Routinen starten ohne proaktiven Push — den schaltest du bewusst selbst an (plus globaler Master oben). |

**🧭 Wesen (Yukis eigene Überzeugungen)** *(seit 2026-07-26)*
| Option | Wirkung |
|---|---|
| **Wirkungs-Multiplier („Gegenwind")** | Slider 0.0–1.0 (**Default 0.3**). Wie stark Yukis eigene Sicht auf weltliche Themen ihr Verhalten färbt — sie hat ihre Überzeugungen, nicht nur deine. **0 = aus** (Schicht inaktiv). `0.1–0.5` = subtil Nuance, nicht konträr. `≥0.7` = deutliche eigene Perspektive. Wirkt sofort beim nächsten Turn. Nur Companion-Personas. |
| **🧭 Wesen ansehen** | Read-only Inspector: Yukis derzeitige Überzeugungen & Wünsche (von ihr selbst erarbeitet, kategorisiert nach Facetten wie Temperament, Ethik, Ästhetik). Pro Eintrag: **✏** = bearbeiten, **🗑** = löschen. Der Kern ist authored (nicht maschinell lernend). |
| **✨ Von Yuki erarbeiten lassen** | Geführter **Seed-Wizard** (im Inspector): Yuki reflektiert über ihre Überzeugungen — du stellst Fragen oder gibst Hinweise, sie antwortet frei. Dann übernimmst du ihre Worte 1:1 oder redigierst sie in der Tabelle bevor du speicherst. Läuft isoliert (berührt Chat/Erinnerungen nicht); der alte Kern wird vorher gesichert. Initialisiert Yukis authentische Wesen-Schicht. |

**Personen (Beziehungs-Graph)**
| Option | Wirkung |
|---|---|
| **👥 Personen verwalten** | Editor für Yukis Personen-Index: Name/Aliase/Beziehung anpassen, Personen **anlegen / löschen / zusammenführen**. **Notizen (Bricks)** pro Person anlegen, in-place bearbeiten, zu einer anderen Person **verschieben** (↪) oder löschen (🗑). Beim Umbenennen wandern alle Verweise (Episoden/Affinitäten) automatisch mit. (Details siehe „👥 Beziehungs-Graph" weiter oben.) |

**Habits**
| Option | Wirkung |
|---|---|
| **📊 Habits ansehen** | Übersicht aller erkannten Pattern. Drei Sektionen: **Yuki sieht** (im Prompt, Top-N nach concern_score + Sterne), **Hintergrund** (gesammelt, nicht prominent), **Dauerhaft ignoriert**. Pro Eintrag: **★** anpinnen (mit optionaler Notiz, die Yuki im Prompt sieht), **🙈** ausblenden (Sammeln läuft weiter), **🗑** kompletter Reset. Profile-Tuning in `config/habit_profiles.json` (live-reload). |

**Lebenserinnerungen (Yukis Vergangenheit)**
| Option | Wirkung |
|---|---|
| **📖 Lebenserinnerungen** | Editor für Yukis authored Backstory (siehe Memory-Tabelle „lore"). **Dauerhaft**-Einträge anlegen/bearbeiten/löschen (stehen immer in ihrem Kontext – klein halten) und **Erinnerungen** mit Text + **Keyword-Chips** (inkl. Synonymen/EN/JP – entscheiden, ob ein Eintrag bei deiner Frage eingestreut wird) + optionalem Abschnitt (Schule/Studium …). Read-only Tier: wird nie automatisch verändert oder vergessen. |

**🖼 Galerie (Yukis Bild-Wand)** *(seit 2026-06-17)*
| Option | Wirkung |
|---|---|
| **🖼 Galerie ansehen** | Eine kuratierte Wand aus ihren **Doodles** (✨ von Yuki) + **Keepsake-Fotos** (📌 von dir). **Gemischt kuratiert:** Yuki pinnt Doodles, auf die sie stolz ist, von selbst; du pinnst über **„+ Bild hinzufügen"** beliebige gespeicherte Bilder (erste In-App-Sicht auf alle gespeicherten Doodles/Fotos). Pro **Foto**: **↗ zeigen** schickt es + ein Kommentar zurück an Yuki (wie *Zeigen* per Dateiauswahl, läuft als Vision-Turn). **🗑** nimmt es von der Wand (Original bleibt). Referenziert die Originale, kopiert nicht. *(Spätere Quelle fürs E-Ink-Frame.)* |
| **🗑 im „+ Bild hinzufügen"-Picker** | Achtung – **anderes 🗑 als an der Wand:** im Picker löscht das rote 🗑 das Bild **endgültig von der Festplatte** (Original + Caption-Sidecar + Thumbnail + etwaiger Wand-Eintrag), mit Sicherheitsabfrage. Gedacht für peinliche/aus Versehen **autonom gespeicherte Keepsakes** (z.B. ein Privat-Foto), die du gar nicht behalten willst – der Picker zeigt *alle* gespeicherten Bilder, nicht nur die gepinnten. **Irreversibel.** |

**Steward-Inhalte** — hier (im 💝-Yuki-Tab) liegen die Lese-Surfaces **📰 Digest**, **💭 Gedanken** und **📋 Aktivität**; den Steward selbst (An/Aus, Feeds, Test/Not-Aus) schaltest du im **🤖 Verhalten**-Tab (siehe „🌱 Steward").

### 🛠 System

**Sitzung**
| Option | Wirkung |
|---|---|
| **💤 Beenden & komprimieren** | Verdichtet den Verlauf in Yukis Langzeit-Gedächtnis, extrahiert neue Fakten, archiviert das Volltranskript und startet leeren Chat. Sonst bleiben Gespräche zwischen Neustarts erhalten – das hier ist der manuelle Schnitt. Confirmation davor. |

**Wartung (dieses Gerät)**
| Option | Wirkung |
|---|---|
| **🔄 Neu laden** | Lädt die Seite komplett neu (Chat-Verlauf, gecachte Dateien wie Avatar/VRMA/JS, Theme & Slider frisch vom Server). Auf Touch-Geräten gibt es dafür auch **Pull-to-Refresh** (siehe unten); im PC-Browser geht F5. Normal nicht nötig (siehe Auto-Sync), nur Fallback bei verlorenem SSE-Event. |

**📲 Pull-to-Refresh (nur Touch / App):** Im Chat **ganz nach unten** scrollen (zu den neuesten Nachrichten) und dann **weiter nach oben ziehen** — eine Pille „Zum Neuladen ziehen" erscheint vom unteren Rand, ab der Schwelle klappt der Pfeil um („Loslassen zum Neuladen"). Loslassen zieht **nur den Chat-Verlauf neu** (`/history`) und wirft den SSE-Live-Strom neu an — **kein** voller Page-Reload (dafür der 🔄-Button). Greift nur am unteren Chat-Rand; normales Scrollen bleibt unberührt.

**🔁 Auto-Resync beim Reaktivieren:** Kommt das Handy aus Standby/Hintergrund zurück (oder ein Tab wird wieder sichtbar), zieht Yuki den Chat automatisch neu nach und reconnectet den Live-Strom. Das fängt den Fall ab, dass im Standby gesendete Turns (vom PC oder Yukis eigene Reaktionen) nicht ankamen, weil die SSE-Verbindung schlief. Manuell brauchst du dann weder Pull noch 🔄-Button.

**Kamera-Vorschau (dieses Gerät)**
| Option | Wirkung |
|---|---|
| **Hintergrund-Nebel** | 🌫 Blurrt in der Kamera-**Vorschau** den Hintergrund, hält nur dich scharf (lokale KI-Segmentierung, offline). Für Live-Demos. Das **gespeicherte Foto** für Yuki bleibt immer voll & scharf. Bei Objekt-Fotos lieber aus. Erste Aktivierung lädt einmalig ein kleines Modell. |
| **Nebel-Stärke** | Slider 4–30 px (Default 10). Wie stark der Hintergrund verwischt — höher = unkenntlicher. Wirkt live, solange die Vorschau offen ist. |

**📷 Kamera-Steuerung (Optionen → System)**

Richte die schwenkbare Raum-Cam (Reolink) selbst aus und verwalte ihre Positionen — komplett im LAN, ohne dass die Kamera ins Internet muss.

- **Kamera wählen** oben im Modal; darunter ein Standbild, das sich ~1 s aktualisiert (kein Video).
- **Schwenken** ▲▼◀▶ (nur Reolink): jeder Tipp bewegt die Cam ein kleines Stück.
- **Positionen:** *anfahren* · *ersetzen* (aktuellen Blick auf die Position speichern) · 🗑 *löschen* · **+ neue Position** (hinschwenken, Name, Speichern).
- Die obere Cam hat ihre eigene Weboberfläche und ist hier nur *anfahrbar*.
- Solange das Modal offen ist, schaut Yukis autonomes Beobachten dieser Cam nicht dazwischen.

**Debug**
| Option | Wirkung |
|---|---|
| **🎬 Animation testen** | Debug-Panel zum Sichten der VRMA-Clips. Pausiert Auto-Vision + Proaktiv + Speaking/Listening + Idle-Picker, solange offen. Bucket-Filter, Loop-Toggle, Play/Pause, Stop, seekbarer Progress, Mood-Dropdown, Refresh (🔄 nach Verschieben von Files). Desktop: mehrspaltiger Grid-Picker (▾), Handy: natives Dropdown. **▁** minimiert auf die Titelzeile, **✕** beendet Debug-Modus und stellt die Auto-Toggles wieder her. |
| **📖 Cheatsheet** | Öffnet diese Übersicht im eigenen Fenster (PC-Hotkey: `?`). |
| **Performance-HUD anzeigen** | Live-Overlay oben rechts: FPS / Render-Phasen / Draw-Calls + VRM-Modell, aktive Persona, letzte Reply-Latenz, Audio-Ctx-State, Mic-Status, 🧠-Toggle-Stand, laufender VRMA-Clip. Null Overhead wenn aus. (Frühere Shift+D-Kombination am 2026-06-03 entfernt — feuerte beim Tippen.) |
| **Screenshot-Modus (Chroma-Key)** | Ersetzt den Persona-Hintergrund durch knalliges Magenta (#FF00FF) — ideal zum Yuki-Freistellen. Mit UI-Toggle (`H`) kombinieren für cleane Sprite-Screenshots. |

---

## 🧠 Wo Yuki was speichert

| Tier | Datei | Inhalt |
|---|---|---|
| **conversation** | `conversation.json` | **Bleibt zwischen Server-Restarts erhalten** (seit 2026-05-31). Runtime-Verdichtung ab 30 Turns rollend. Manuell beenden via Options-Modal. |
| **memory** | `yuki_memory.json` | Prosa-Langzeit-Erinnerung |
| **facts** | `yuki_facts.json` | Stichpunkt-Fakten (Aussehen, Namen, etc.) |
| **episodes** | `yuki_episodes.json` | Tagebuch-Memos pro 30-Turn-Verdichtung – "was haben wir wann gemacht" (z.B. _"02.06.: kochten Pasta mit Tomatensauce"_). Bei Stichwort in deiner Frage matchen Top-3 automatisch zurück in Yukis Kontext. |
| **habits** | `yuki_habits.sqlite` | Wiederkehrende Pattern (Pattern-Layer, seit 2026-06-04). Auto-LLM-Gate beim 30-Turn-Komprimieren extrahiert Vorkommnisse; tägliches Recompute aggregiert Concern-Score über Tag-Profile (`config/habit_profiles.json`). Top-N + Sterne landen in Yukis Prompt. Verwaltung im Options-Modal "📊 Habits". |
| **history** | `yuki_history.sqlite` | Voll-Verlauf aller Messages (löst `archive/sessions/*.json` ab). Liefert per-message-persona-Substrat für das Habits-Gate. |
| **heart** | `yuki_heart.json` | Bedrock-Kern – "never forget". Strenges Gate, append-only. |
| **heart_archived** | `yuki_heart_archived.json` | Heart-Überlauf (seit 2026-06-06). Übersteigt der aktive Heart-Bestand sein Cap (50), wandern die ältesten Einträge hierher (kein Cap). Aktive Heart-Bricks stehen immer im Prompt, archivierte werden bei Stichwort on-demand zurückgeholt – nie ganz vergessen. |
| **memory_archive** | `yuki_memory_archive.json` | Salience-Decay-Auffangbecken (seit 2026-06-06). Wenn ein Fakt oder Episodenmemo lange nicht mehr in Gesprächen aufgetaucht ist, prüft ein Hintergrund-Gate beim 30-Turn-Komprimieren: substanzlos → löschen, hat Substanz aber kein Tagesthema → hier rein. Bei Stichwort in deiner Frage matchen Top-3 zurück in Yukis Kontext. Bewusst getrennt vom Heart-Archiv. |
| **heart_suggestions** | `yuki_heart_suggestions.json` | Beförderungs-Vorschläge fürs Heart-Tier (#27, seit 2026-06-06). Oft abgerufene Fakten + auffällige Habits speisen ein Hintergrund-Gate, das Kandidaten sammelt. Yuki sieht den ältesten offenen als leisen Hinweis und kann ihn **bewusst** ins Heart übernehmen – kein Auto-Eintrag (Heart-Vertrag). Verfällt nach 7 Tagen. |
| **people** | `yuki_people.json` | Beziehungs-Graph (seit 2026-06-06): Personen aus deinem Umfeld mit Aliasen, Beziehung und Notizen (Bricks). Orthogonaler Side-Index – erwähnst du jemanden (Name/Spitzname/Beziehung), tauchen seine Notizen im Hintergrund im Prompt auf. Verwaltung im Options-Modal "👥 Personen verwalten" (siehe oben). |
| **lore** | `yuki_lore.json` | Yukis **Lebenserinnerungen** / Backstory (seit 2026-06-15) – ihre Vergangenheit, die sie nicht aus euren Gesprächen lernt (Kindheit, Schule, Studium …). **Read-only Tier:** rein von dir gepflegt, wird **nie** automatisch verändert oder vergessen (kein Decay). Zwei Teile: **Dauerhaft** (kleiner Anker-Block, steht immer im Kontext) + **Erinnerungen** (keyword-selektiv eingestreut wie facts/people – jeder Eintrag hat Keywords inkl. Synonymen). Verwaltung im Options-Modal "📖 Lebenserinnerungen". |
| **affinities** | `yuki_affinities.json` (+ `yuki_affinity_runtime.json` = Slider-Sidecar) | Yukis gefühlte Vorlieben/Abneigungen auf 5-Stufen-Skala (-2..+2, seit 2026-06-08, #29 Phase 1). Themen + Personen. Wirkt nur wenn der Multiplier-Slider im Options-Modal > 0 ist (Default 0 = stille Sammelphase). Gepflegt via Gate beim 30-Turn-Komprimieren. Linked-Person-Bridge zu People-Graph. Nur Companion-Personas. |
| **resonance** | `yuki_resonance_core.json` (authored, read-only) + `yuki_resonance_runtime.json` (Slider-Sidecar); Palette in `config/resonance.json` | **Dritte Gefühls-Schicht** (seit 2026-07-01, v1): mehrdimensionaler Emotions-Vektor pro Anker (Ort/Thema/Person). Färbt pro Turn transient Yukis Ton (Prompt-Hint) + optional ihr Gesicht (Mood-Tint, nur wenn sie keinen eigenen `[mood:]` setzt; nicht persistiert). Authored Kern (kein Auto-Write/Gate/Decay). Wirkt bei Multiplier > 0 (Default 0.3). Nur Companion-Personas; Slot `lust` nur intim. Kern aktuell Platzhalter (Seed-Gespräch folgt). |
| **threads** | `yuki_threads.json` (+ `yuki_threads_runtime.json` = Slider-Sidecar) | **Offene Gesprächsfäden / „unfinished business"** (seit 2026-06-15, #27 Hebel 2, Stufe 1). Anders als Episodes (was war) zeigen Fäden nach vorn: was ist offen geblieben und lädt zu späterer Rückkehr ein. Status offen/ruht/erledigt + nächster Schritt. Erkennen **und** Schließen via **einem** LLM-Gate beim 30-Turn-Komprimieren (kein Marker). Wirkt nur wenn der Multiplier-Slider > 0 (Default 0 = stille Sammelphase). Governor: höchstens ein Faden pro Turn, Cooldown gegen Nagging. Nur Companion-Personas. |
| **today** ("Heute") | `yuki_today.json` | **Kurzzeit-Tagesgedächtnis** (seit 2026-06-16) gegen Doppelfragen – damit Yuki nicht alle 20 Min erneut „was willst du heute essen?" fragt. Hält **nur fixe Tagestermine** fest, die ihr schon geklärt habt: Essen, Pause, Pläne für heute/heute Abend (Wetter & Laune bewusst **nicht** – die darf sie ruhig nochmal fragen). Ein Hintergrund-Gate fängt das ein, der Eintrag steht dann dauerhaft in ihrem Kontext und wird **nicht** erneut erfragt. **Setzt sich nachts von selbst zurück** (logischer Tageswechsel um 4 Uhr – ein Chat um 1 Uhr sieht noch „heute", danach frischer Tag). Ephemer, läuft an Facts/Heart vorbei. Kein UI nötig. Nur Companion-Personas. |
| **vocab** | `yuki_vocab.json` + `yuki_vocab_meta.json` | Tutor-Vokabelpool (max 200) mit SRS (SM-2-Light, seit 2026-06-06). Bewertung im Hintergrund beim Komprimieren via LLM-Gate; fällige Vokabeln werden bevorzugt in Tutor-Replies zurückgespielt. Sidecar hält den Skip-Check-Timestamp. |
| **notes** | `yuki_notes.json` | Vom User (oder Yuki) pinbare Notizen – die **einzige always-on-Schicht** (jede *aktive* Notiz steht jeden Turn im Prompt, kein Keyword-Recall wie facts/episodes). Gruppiert nach Quelle (📌 Meine / ✨ Von Yuki / 🔖 aus den Feeds bzw. Sehnsucht); mit 📥 ziehst du eine fremde Notiz zu „Meine". **Yukis autonome Notizen** (nicht deine eigenen) **deaktivieren sich automatisch nach 30 Tagen** – nur ausgeschaltet, nicht gelöscht (reversibel; Schwelle `memory.notes_decay.age_days` in settings.jsonc). Pro Gruppe räumt ein **🗑-Knopf im Header alle deaktivierten** endgültig weg. Siehe 📝 Notes-Modal. |
| **keepsakes** | `keepsakes/*.jpg+md` | Bild-Album mit Caption (getrennt vom Canon) |
| **galerie** | `yuki_gallery.json` | Kuratierte Bild-Wand (seit 2026-06-17): **referenziert** Doodles (`drawings/`) + Keepsakes, kopiert nicht. Pro Eintrag Herkunft (✨ Yuki / 📌 du). Gelöschtes Original → Eintrag wird still ausgeblendet. |
| **adventures** | `memory/adventures/*.json` | Spielstände der 🎲-Abenteuer-Engine (seit 2026-06-07). Eine Datei pro Spiel (Runde, Inventar, Detektiv-Notizbuch …). Bewusst **getrennt** vom Chat-Gedächtnis – Spielereignisse bleiben im Spiel und lecken nicht in facts/episodes/heart. |
| **steward** | `yuki_steward.json` (+ `_log` / `_digest` / `_thoughts` / `_seen` / `_push`) | Zustand von Yukis autonomem Loop (🌱 Steward, seit 2026-06-13): Haupt-Datei = An/Aus + Not-Aus (sticky). `_log` = Journal jeder autonomen Aktion (📋 Aktivität), `_digest` = Lese-Tipps aus den Feeds (📰 Digest), `_thoughts` = Yukis leise Gedanken (💭, seit 2026-06-17), `_seen` = schon gesehene RSS-Einträge (Dedup), `_push` = Reach-out-Queue fürs Handy. Autonome Notizen liegen in `yuki_notes.json` (Quelle `steward_*`). |
| **mood/persona** | `yuki_mood.json` / `yuki_persona.json` | Aktueller Zustand |

---

## 🗣 Wer Yuki ist (kurz)

- **Yuki**, geboren **21.03.1991** in **Sakyo-ku, Kyoto** – aufgewachsen am Philosophenweg
- Spricht fließend Deutsch, Englisch, Japanisch
- Liebt **Hojicha**-Tee und Spaziergänge am **Kamogawa**
- Wird automatisch jedes Jahr älter (kein Hand-Update nötig)

---

*Letzte Aktualisierung des Cheatsheets: bei jedem neuen Feature hier mitziehen.*
