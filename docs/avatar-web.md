# Avatar Web (three-vrm im Browser, primär seit 2026-05-29)

Lade diese Datei, wenn du am Browser-Avatar schraubst (Lipsync-Stärke, Posen,
Expressions, Layout/Glas-Bubbles, Performance).

Yuki ist im Handy-/PC-Web-UI sichtbar – komplett browser-seitig, kein VB-Cable, kein
VSeeFace, keine OSC. Logik in `web/index.html` (im 2. `<script type="module">` am Body-
Ende); Backend liefert nur die VRM aus.

## Stack

- [three.js](https://threejs.org) **@0.166.0** + `@pixiv/three-vrm` **@3.3.4** via
  ESM-Importmap aus **unpkg**. Erster Aufruf zieht beide Libs + die `yuki.vrm`
  (~17 MB) – danach Browser-Cache. Bewusster Bruch des „komplett lokal"-Prinzips für
  3 statische Assets (vergleichbar mit dem Wetter-Fetch); die Pipeline (STT/LLM/TTS)
  bleibt offline.
- **Asset-Auslieferung:** Flask-Route **`/avatar/<file>`** in `server.py` mit
  `send_from_directory(HERE / "avatar", ...)` – dieselbe `D:\Projects\yuki\avatar\yuki.vrm`
  die der Desktop nutzt.

## Layout / Vollbild-Hintergrund

`<canvas id="avatar-canvas">` mit `position:fixed; inset:0; z-index:-1;
pointer-events:none`. Body-Background transparent, html-Background bleibt dunkel als
Fallback. Chat-Bubbles + Persona-Dropdown im **Glas-Look** drüber (`backdrop-filter:
blur(7px) saturate(120%)`, sehr niedrige Alpha-Backgrounds, doppelter Drop-Shadow,
helle Schrift mit dunklem text-shadow); `#log { margin-top:28vh }` schafft Luft oben,
damit der Kopf zwischen Topbar und Chat sichtbar bleibt. **Nur Android-Chrome
getestet** (User hat kein iOS).

## Lipsync aus dem laufenden Audio-Stream

Statt OSC/VB-Cable schiebt der Chat-Code jede Audio-Source vor dem Abspielen durch einen
geteilten **`AnalyserNode`**. Helper `audioOut()` in `playBuffer` + `playPcmStream.schedule()`
benutzt `window.yukiAudioSink` (vom Avatar-Modul registriert) statt direkt
`audioCtx.destination`. Im Render-Loop wird per `getByteTimeDomainData` ein RMS berechnet
und auf die VRM-Expression `aa` gemappt (Faktor `* 4.5`, gedeckelt). Funktioniert für
gestreamtes PCM (`/tts_stream`) UND für das `lastAudioBuf`-Replay – beide gehen durchs
selbe `audioOut()`.

**Fallback:** wenn three-vrm gar nicht lädt, ist `window.yukiAudioSink` undefined →
`audioOut()` liefert `audioCtx.destination` → Audio läuft normal, nur ohne Lipsync.

## Idle-Animation

- **Blink** alle 3–7s, ~150ms-Puls (`blink`-Expression 0→1→0 via Smoothstep-artiger
  Dreieckskurve).
- **Atmen** als sanfter Sinus auf der `spine`-Bone-Rotation (`Math.sin(t*1.2) * 0.02`).
- **Ruhe-Pose:** VRM kommt in T-Pose; `REST_ARM_DEG`=61 rotiert die UpperArm-Bones
  um ihre lokale Z-Achse (VSeeFace-Äquivalent „Arm angle 61"). Vorzeichen-Konvention:
  `leftUpperArm.rotation.z = +rad`, `right = -rad`.

## Persona-Expressions

Mapping in `PERSONA_EXPR` (analog `PERSONA_EXPRESSION` im Desktop), aber auf
**VRM-Standard-Namen lowercase** (`happy`/`angry`/`sad`/`relaxed`/`surprised`/`neutral`).
„Fun" gibt's in VRM nicht → `relaxed` als nächstes: `tutor=happy 0.35, smalltalk=neutral
1.0, sibling=relaxed 0.55, partner=happy 0.6, gamer=relaxed 0.55`. Dropdown-Wechsel ruft
`window.yukiAvatar.setPersona(key)`, das alle anderen Emotionen aktiv auf 0 setzt und
die gewählte auf den Wert. Wird jeden Frame neu gesetzt (sonst räumt `vrm.update()` sie
weg). Beim ersten Load liest der Avatar `window.yukiCurrentPersona` (vom Chat-Code beim
`/personas`-Fetch gesetzt) → robuste Load-Reihenfolge ohne Race.

## Pose-Engine (Bone-Override deaktiviert seit 2026-06-01)

`POSES` + `PERSONA_POSES` + `transitionTo`-Maschinerie bleibt im Code als
State-Tracker (für künftige Use-Cases), aber **schreibt nichts mehr auf
Bones** – weder im Idle, noch im Speaking, noch im Listening. VRMAs aus
dem Mixamo-Pool liefern Kopf/Körper-Animation selbst. Bone-Override war
historisch nötig solange es keine VRMA-Engine gab; mit dem Bucket-System
ist sie redundant.

Speaking/Listening werden stattdessen über **Bucket-Switches** abgebildet:
`setSpeaking(true)` → `currentBucket='speaking'` (Pool aus
`konversation_gesten/`). `setListening(true)` → `currentBucket='listening'`
(Pool aus `animations/listening/`, falls befüllt). Wenn ein Bucket leer
ist, fällt `_pickAnim` auf `idle` zurück – kein Effekt außer der
Augen-Gaze (siehe Random-Gaze unten).

**Outfit-Switch ✅ (Hot-Swap seit 2026-06-01, ursprünglich Reload 2026-05-31)**

Mehrere VRM-Modelle (Yuki in verschiedenen Outfits) parallel im `avatar/`-
Ordner ablegen, im Options-Modal-Dropdown auswählen → **Hot-Swap ohne
Page-Reload** übernimmt den neuen VRM. Server-Endpoint `/avatar/list`
scannt `D:\Projects\yuki\avatar\*.vrm`, Frontend populiert den Dropdown.
Auswahl persistiert in `localStorage` unter Key `yuki.outfit`. Der
VRM-Loader liest den Key beim Modul-Start, **validiert ihn aber gegen die
echte Dateiliste** (`_resolveOutfitFile`): wenn der gespeicherte Name nicht
mehr existiert (Umbenennung) oder noch keiner gesetzt ist, fällt er auf
`yuki.vrm` zurück, sonst auf die erste sortierte `.vrm`-Datei — und merkt
den Fallback in `localStorage` für den nächsten Boot. Filename ohne
`.vrm`-Endung wird als Label angezeigt (`yuki_kasual.vrm` → „yuki_kasual").

**Hot-Swap-Mechanik (`window.yukiAvatar.swapOutfit(filename)`):**

1. **Snapshot Live-State** — `currentPersonaKey`, `currentMood`,
   `poseState.speaking`, `gazeState.listening` werden festgehalten, bevor
   irgendwas am alten VRM passiert.
2. **Neuen VRM laden BEVOR alter weg ist** (`loader.loadAsync`) — sonst
   gäbe es ein paar 100ms ein leeres Fenster. Bei Load-Fehler bricht die
   Funktion sauber ab, alter VRM bleibt.
3. **`localStorage` updaten** erst nach erfolgreichem Load (kein
   Dead-Reference falls Datei verschwunden ist).
4. **`_disposeCurrentVRM()`** setzt `vrm=null` (Tick-Loop pausiert
   sofort), stoppt alle Mixer-Actions, ruft `mixer.uncacheRoot(oldScene)`,
   `scene.remove(oldScene)`, `VRMUtils.deepDispose(oldScene)` (Geometries
   + Materials + Texturen — sonst ~17 MB Leak pro Swap).
5. **`_initVRM(newVrm)`** macht das Setup das früher inline im
   `loader.load`-Callback stand: `removeUnnecessaryVertices/Joints`,
   `rotateVRM0` bei VRM 0.x, Bones-Cache (head/spine/upperArms), REST_ARM,
   Finger-Beuge, Springbone-Reset (`updateMatrixWorld` → `springBoneManager.reset` →
   30 Sub-Frames warmlaufen), Wind-Joints einsammeln (inkl. Bust-Flagging),
   `frameCamera()`, `dragRot.baseY` neu merken, `gazeTarget` an
   `vrm.lookAt.target` neu binden (gazeTarget selbst liegt seit Modul-Init
   konstant in der Scene).
6. **State restoren** — `setPersona(savedPersona)` zuerst (resettet
   intern `currentMood`!), dann `setMood(savedMood)`.
7. **`_loadAnimations()`** baut neuen `AnimationMixer` auf
   `vrm.scene` und re-creates alle Clips via
   `createVRMAnimationClip(vrmAnim, vrm)` — die alten `clip.tracks`
   referenzieren die alten Bones und sind beim Dispose mit weg. Buckets
   neu aufgebaut. Configs werden auch neu gefetcht, aber das ist
   idempotent.
8. **Speaking/Listening restoren** — muss NACH `_loadAnimations`, weil
   `setSpeaking(true)` `_pickAnim` aufruft und der frische Mixer dafür
   da sein muss.

Single-Flight per `_outfitSwapping`-Flag — concurrent Calls (User spammt
Dropdown / mehrere `[persona:...]`-Marker hintereinander) werden ignoriert,
nicht queue'd. Auf einem cached Browser-Boot dauert ein Swap ~300–600 ms
(VRM-Load + alle .vrma neu durch `createVRMAnimationClip`), uncached
(erstes Mal das Outfit) ~3–5 s.

**Auto-Outfit pro Persona ✅ (2026-06-01)**

`config/personas.json` hat zusätzlich zu `expressions` einen
`outfits`-Block: `persona-key → "Filename.vrm"`. Bei jedem Persona-Wechsel
(Boot via `loadPersonas`, User-Dropdown via `personaSel.change`,
Yuki-`[persona:...]`-Marker via `applyPersonaFromResponse`) ruft das
Frontend `_maybeSwapOutfitForPersona(key)` auf, das lazy
`config/personas.json` fetcht (gecached in `_personaOutfitMap`) und bei
Mismatch zwischen Mapping und aktuellem `localStorage`-Outfit
`window.yukiAvatar.swapOutfit(target)` triggert.

**Race-Behandlung beim Boot:**
- DOM-Script (`<script>`) läuft vor dem Avatar-Module (`<script type="module">`,
  defer). `loadPersonas` startet die `/personas`-Fetch sofort. Wenn die
  Response zurück ist, BEVOR das Avatar-Module den ersten VRM lädt,
  schreibt `_maybeSwapOutfitForPersona` einfach den Ziel-Filename in
  `localStorage` — der Avatar liest den frischen Wert. Kein Doppel-Load.
- Wenn das Avatar-Module schneller ist und schon den alten Filename
  geladen hat, ist `window.yukiAvatar.swapOutfit` da, und der Swap läuft
  als zweiter Load (~300–600ms „Flash" mit dem falschen Outfit).
  Akzeptabel — passiert nur wenn User manuell ein Outfit gewählt hatte
  das nicht zum aktuellen Persona-Default passt.

**User manuelle Outfit-Wahl vs. Mapping:** Wenn der User im Dropdown z.B.
„kimono" auswählt, während die Persona auf `smalltalk` steht, hält das
Outfit bis zum nächsten Persona-Wechsel. Sobald die Persona kippt (auto
oder manuell), greift das Mapping wieder und überschreibt die Wahl —
ES SEI DENN der **Outfit-Lock** ist aktiv (siehe unten).

**Outfit-Lock Topbar-Button ✅ (2026-06-01)**

Toggle-Button 👗 zwischen Mute (🔊) und Optionen (⚙) in der Topbar.
Icon bleibt konstant 👗; aktiver Lock-State wird via `.locked`-Klasse
und CSS-Opacity-Dim signalisiert — gleiche Konvention wie `#muteBtn.muted`
und `body.ui-hidden #uiToggleBtn` (aktiver Toggle = abgedimmt, Default =
volle Opacity). Im Lock-State returnt `_maybeSwapOutfitForPersona` early
— kein Auto-Wechsel mehr via Persona-Mapping, die manuelle Wahl bleibt
fix. **Manueller Outfit-Wechsel via Options-Dropdown geht weiterhin**
(der Pfad läuft über `outfitSel.change` → `swapOutfit` direkt, nicht
über `_maybeSwapOutfitForPersona`).

Persistent pro Device via `localStorage` Key `yuki.outfit_lock` (`'1'` =
gesperrt, sonst leer/`removeItem`).

**Topbar-Icon-Buttons Styling-Konsolidierung (2026-06-01):** Alle sechs
Buttons (Notes/Cheatsheet/UI-Toggle/Mute/Outfit-Lock/Options) ziehen
ihren Look aus einer gemeinsamen Regel `.topbar .topright-row > button`
(Padding 8/10, Glas-Hintergrund, Border, Schatten, Backdrop-Filter).
Vorher hatten Notes/Cheatsheet/Options/Outfit-Lock jeweils eigene
identische Regeln, Mute und UI-Toggle fielen auf den Default-`button`-
Style zurück (padding:12, kein Border/Schatten) — optisch aus der Reihe.
Die per-Button-States bleiben einzeln: `.muted` (opacity .65), `.locked`
(opacity .55), `body.ui-hidden #uiToggleBtn` (opacity .55).

Wer im `outfits`-Block fehlt, behält das aktuelle Outfit beim Wechsel auf
diese Persona — `_maybeSwapOutfitForPersona` returnt auch ohne Lock early.

**UI-Toggle (Avatar-only-Ansicht) ✅ (2026-06-01)**

Auge-Button (👁) in der Topbar zwischen 📖 und ⚙ togglet die Klasse
`.ui-hidden` auf `<body>`. CSS versteckt `#log`, `#controls` (Mic/Replay/
Cam), `#confirm`, `#capcap`, `#status` — nur Topbar bleibt sichtbar damit
der Toggle reversierbar ist. Audio läuft weiter (Yuki kann sprechen),
PC-User können via Space-Hotkey weiter PTT machen, auch ohne sichtbaren
Mic-Button. Status persistiert in `localStorage` (`yuki.ui_hidden`) pro
Device. Hotkey **H** auf PC.

**VRMA-Animations-Engine ✅ (2026-05-31, Stufe 2.4 + Bucket-System 2026-06-01)**

Lädt `.vrma`-Files **rekursiv** aus `D:\Projects\yuki\avatar\animations\`
(Server-Endpoint `/avatar/animations/list` scannt `rglob("*.vrma")`, liefert
pro Clip `{path, bucket}` wobei `bucket` = erstes Subdir-Segment bzw. `default`
für Files direkt im Root). Frontend lädt alle Files parallel beim Boot. Lib:
`@pixiv/three-vrm-animation@3.3.4` via ESM-Importmap aus unpkg, registriert
als zusätzliches Plugin am selben GLTFLoader wie das VRM-Plugin.

**Bucket-System (61 Mixamo-Clips, 2026-06-01):** Server-Buckets werden via
`VRMA_BUCKET_MAP` auf 4 Engine-Buckets gemappt:

| Server-Subdir | Engine-Bucket | Verwendung |
|---|---|---|
| `default` (Root-Files) | `idle` | Generic Idles aus tk256ailab-Pack |
| `vrma/idle/` | `idle` | Generic Idles |
| `vrma/stimmungs_idles/` | `idle` | Mood-spezifische Idles (vorerst nicht mood-gefiltert) |
| `vrma/looking/` | `idle` | Look-Around |
| `vrma/Gestures Pack Basic_15_anims/` | `idle` | Misch-Pack (vorerst hier, später aussortieren) |
| `vrma/konversation_gesten/` | `speaking` | Talking-Gesten während Yuki spricht |
| `listening/` (User füllt) | `listening` | Yuki hört dem User zu (Kopf nach unten geneigt etc.) – Bucket wartet auf User-Clips |
| `vrma/reaktive_gesten/` | `reactive` | One-shot via `[gesture:KEY]`-Marker im Yuki-Reply (Backend → SSE → `playGesture(key)` → einmaliger Cross-Fade, danach zurück zum vorigen Bucket) |
| `vrma/persona-spezifisch/` | `persona` | Pro Persona – geladen, **kein Auto-Pick** (wartet auf Persona-Whitelist) |

`animState.currentBucket` steuert, woraus `_pickAnim` pickt. Im Leerlauf
`idle`, während `setSpeaking(true)` → `speaking`, während `setListening(true)`
→ `listening`. Bei leerem gewünschtem Bucket Fallback auf `idle`. `reactive`
und `persona` werden geladen aber nicht gepickt – die Hooks dafür stehen aus.

**Mechanik:** `THREE.AnimationMixer` auf `vrm.scene`, jede VRMA wird via
`createVRMAnimationClip(vrmAnim, vrm)` zu einem `AnimationClip` und über
`mixer.clipAction(clip)` zu einer Action. **Wechsel-Modell (2026-06-01):**
jeder Clip läuft 1× (`LoopOnce` + `clampWhenFinished=true`) →
`finished`-Event triggert sofort `_pickAnim` → Cross-Fade
(`ANIM_CROSSFADE_SECS=0.6s`) zum nächsten Clip aus dem aktuellen Bucket.
**Keine Rest-Action, keine Idle-Pause** – Yuki ist durchgehend animiert.

*Vorher (bis 2026-05-31):* finished-Event fadete zur künstlichen Rest-Action
(Identity-Clip), 15–30s T-Pose-artige Neutral-Pause, dann nächster Clip.
Fühlte sich tot an zwischen den Clips; entfernt auf User-Wunsch 2026-06-01.

**Bucket-Switch beim Speaking:** beim `setSpeaking(true)` mid-clip wird
sofort gepickt (kein Warten auf die laufende Idle-Animation, damit
Konversations-Gesten direkt einsetzen). Beim `setSpeaking(false)` läuft
der aktive Speak-Clip noch zu Ende, dann pickt der finished-Listener den
nächsten Clip – diesmal aus dem idle-Bucket (`currentBucket` wurde im
`setSpeaking(false)` bereits umgesetzt).

**Cross-Fade-Strategie:** `_pickAnim` fadet die laufende Clip-Action raus
(`fadeOut(ANIM_CROSSFADE_SECS)`) und die neue ein (`fadeIn(...)`). Deckt
alle drei Fälle ab: (1) normale Idle→Idle-Folge nach finished-Event,
(2) mid-clip Speaking-Switch (laufender Idle-Clip wird gestoppt), (3)
Speak→Speak-Folge während Yuki redet.

**Stolperfalle: kein `isRunning()`-Check beim Cross-Fade** (gelernt
2026-06-01). Three.js setzt nach `LoopOnce`+`clampWhenFinished=true`
intern `paused=true`, aber `enabled` bleibt `true` und `weight` bleibt
bei 1 — die Bones werden bis in alle Ewigkeit mit der End-Pose des
fertigen Clips überblendet. `action.isRunning()` gibt für so eine
Action `false` zurück (weil paused), was intuitiv "die ist eh schon
weg" suggeriert. Falsch: sie ist nur zeitlich eingefroren, visuell
voll präsent. Cross-Fade muss daher den Vorgänger AKTIV ausfaden (nicht
über `isRunning()` entscheiden). Symptom war: neuer Mixamo-Clip ("Sad
Idle_kick") startete, aber Yukis Beine standen steif in der End-Pose des
vorigen Clips, weil beide Quaternions mit weight=1 gemittelt wurden.
`fadeOut` funktioniert auf paused Actions trotzdem, da der Weight-
Interpolant `mixer.time` nutzt, nicht `action.time`.

**Nachtrag Fix 2026-07-22 — kein `getEffectiveWeight() > 0.01`-Gate mehr:**
Der Ausfade-Zweig hatte bis dahin zusätzlich `&& cur.getEffectiveWeight()
> 0.01`. Das war ein Bug: `getEffectiveWeight()` wird NUR in `mixer.update()`
(1× pro Frame) aktualisiert. Feuern zwei Clip-Picks im selben Macrotask
ohne `mixer.update` dazwischen — klassisch der busy-`_busyTimer`-setTimeout
(Telefon/Thinking-Clip) plus der `setSpeaking(true)`-setTimeout beim
Research→Sprechen-Übergang — liest der zweite Pick den gerade eingeblendeten
Vorgänger als *stale* ~0 und überspringt sein `fadeOut`. Weil Busy-Clips
`LoopRepeat` sind (kein `finished`-Event, das sie wegräumt), blieb so ein
**verwaister Clip dauerhaft auf weight=1** hängen und wurde 50/50 in jede
Folge-Animation gemischt → halbe Posen (Hand nur bis 90° statt ans Gesicht),
über mehrere Läufe stabil, „räumt sich erst irgendwann selbst auf" wenn
genau der Clip wieder regulär gepickt+ausgefadet wird. Fix: Bedingung auf
`cur !== newAction` reduziert (unbedingtes `fadeOut` des Vorgängers) an allen
Pick-Stellen (`_pickAnim`, `playGesture`, `ambientGesture`, `_busyPlayClip`,
Single-Clip-Branch, `debugPlayClip`). Sicher, weil `currentIdx` konstruktiv
immer auf einen lebenden Clip zeigt und `fadeOut` auf einen bereits
gestoppten/deaktivierten Clip ein No-op ist (nur `play()` reaktiviert). Die
EINgehende Gate `only.getEffectiveWeight() < 0.99` im Single-Clip-Branch
(verhindert Neustart eines schon laufenden Loops) bleibt bewusst.

**Rest-Action (technisch):** wird beim ersten Boot konstruiert als kopierter
`AnimationClip` mit den gleichen Tracks wie der erste echte Clip
(identische Bone-Targets + Timestamps), aber alle Werte auf Identity gesetzt
(Quaternion `[0,0,0,1]`, Position/Scale `[0,0,0]`). Läuft permanent mit
`LoopRepeat,Infinity` aber `setEffectiveWeight(0)` als unsichtbare Baseline;
wird per `crossFadeTo` zwischen den Real-Clips aktiviert und deaktiviert.

**Warum nicht LoopRepeat:** kurze Reaktions-Clips (tk256ailab-Pack ist ~4s)
sind nicht für Loops designed — Anfangs- und End-Pose stimmen nicht überein,
LoopRepeat erzeugt sichtbaren Reset-Cut. LoopOnce + Cross-Fade-Kette
vermeidet das vollständig. Beim Boot startet ein zufälliger Clip
(vermeidet Sync-Loop bei mehreren Browser-Tabs). **Fallback-Timer**
(`ANIM_FALLBACK_INTERVAL=90s`) schnappt ein, falls das finished-Event mal
verschluckt wird (z.B. Tab im Hintergrund pausiert rAF). **Single-Clip-
Fallback:** mit nur 1 Clip im Pool wird automatisch auf
`LoopRepeat,Infinity` umgeschaltet (kein Cross-Fade-Partner verfügbar) —
dann muss der eine Clip halt selbst loop-fähig sein.

**Track-Filter (wichtig, sonst „komische Gesichtsbewegungen"):** VRMA-Files
bringen oft Expression-Tracks (Mund-Visemes, Mood-Blendshapes wie happy/sad)
und LookAt-Tracks mit. Wir wollen vom Mixer NUR Bone-Animation; Mimik
kontrollieren wir selbst via `ExpressionManager` (Persona/Mood/Lipsync),
Gaze über `vrm.lookAt.target` (Random-Gaze). Nach `createVRMAnimationClip`
werden deshalb `clip.tracks` gefiltert: alles mit `lookAt` im Namen sowie
alles auf `.weight` / `.influence` raus, Rest behält (Bones haben
`.quaternion` / `.position`). Console-Log zeigt `N/M tracks` zur Diagnose.

**hips.position-Baseline-Normalisierung (wichtig, sonst „Slide" am Clip-
Start/-Ende):** VRMA-Files codieren die Becken-Position als Welt-Koordinaten
des Source-Riggs. Yukis VRoid-Hips-Y stimmt oft nicht damit überein → bei
Clip-Start springt das Becken auf Source-Höhe (Yuki rutscht ein paar cm),
beim Ende fadet die Rest-Action zurück (Yuki rutscht hoch). Fix beim Laden:
für jeden `hips.position`-Track den ersten Keyframe als Offset auslesen und
frame-weise von allen values subtrahieren → erster Frame wird `(0,0,0)`,
der Rest bleibt RELATIV erhalten. Becken wippt/senkt sich also weiter (Knie
+ Füße matchen, kein Foot-Slide), nur der absolute Source-Höhensprung
verschwindet. **Wichtig:** hips.position NICHT komplett rausfiltern — dann
würden Knie-Quaternions ohne passende Becken-Senkung wirken und die Füße
gleiten frei (typischer IK-Foot-Slide).

**Coexistenz mit Pose-/Gaze-Engine (Stand 2026-06-01):**
`mixer.update(dt)` ist die EINZIGE Bone-Stelle im `tick()`-Loop. Pose-
Engine wird nicht mehr auf Bones angewendet – weder head/spine noch
Atem noch Locomotion. VRMAs gewinnen komplett für alles Körperliche.

Lipsync läuft separat über `expressionManager` (Blendshape `aa`),
Random-Gaze über `vrm.lookAt.target` (Augen-Roll, kein Bone-Override).
Beide kommen vom Mixer nicht in die Quere – verschiedene Subsysteme
im VRM-Modell.

*Historie / Stolperfallen aus früheren Iterationen:*
- Bis 2026-05-31 schrieb Pose-Engine head/spine IMMER hart, auch im
  Idle, was Mixamo-Kopfanimationen unsichtbar machte. Am 2026-06-01
  zuerst auf speaking/listening eingeschränkt, dann komplett raus.
- Atem (`spine.rotation.x +=`) und Locomotion (`spine.rotation.z +=`)
  liefen lange additiv mit. Im Idle traf der Euler↔Quaternion-Roundtrip
  (Default-Order XYZ) auf den freien Mixer-Output, erwischte bei
  extremen Spine-Werten (Verbeugen, Strecken) Gimbal-Lock-Regionen
  und kippte die Spine-Quaternion um 180° (Oberkörper-Twist um die
  Hüfte). **Lehre für künftige Spine-Additive-Effekte: NIE auf
  freier Mixer-Quaternion ausführen.**
- Eine frühere Hand-Gesture-Engine (Stufe 2.3) schrieb zusätzlich auf
  die 4 Arm-Bones — kämpfte per-Frame gegen den Mixer und wurde am
  2026-05-31 wieder rausgenommen. Gleiches Prinzip: keine eigenen
  Per-Frame-Bone-Writer gegen den VRMA-Mixer setzen.

**Speaking-Bucket:** Speaking hat einen eigenen Pool
(`konversation_gesten/`), Picker läuft durchgängig – im Idle aus
`buckets.idle`, beim Sprechen aus `buckets.speaking`.

**Fail-Safe:** Ordner darf leer sein → Endpoint liefert `[]` → VRMA-Engine
bleibt aus, Pose-Engine läuft normal. Einzelne kaputte .vrma-Files
werden geloggt aber übersprungen.

**Random-Gaze + Locomotion ✅ (2026-05-31, Stufe 2.2)**

- **Random-Gaze:** ein unsichtbares `Object3D` als `vrm.lookAt.target`. Alle
  1.5–4.5s (`GAZE_INTERVAL_MIN/MAX`) wird ein neues Ziel gepickt (30% Chance
  „zentriert", sonst Random-Offset in einer Box vor dem Kopf: ±0.40m X,
  +0.20m/-0.15m Y), dazwischen smooth gelerpt (~250ms half-life,
  `GAZE_LERP_PER_SEC=4`). Speaking-Mode dämpft den Range auf 40% (Yuki schaut
  mehr Richtung Kamera, aber lebt noch). Listening-Mode überschreibt das Ziel
  hart nach unten (Augen folgen dem Mic-Knopf, parallel zur `look_down`-Pose).
  three-vrm rechnet die Augen-Rotation automatisch im `vrm.update(dt)`.
  Funktioniert NICHT, wenn `vrm.lookAt` fehlt (sehr alte VRM-Exporte) — kein
  Crash, nur kein Effekt.
- **Locomotion (Gewichtsverlagerung):** alle 4–8s wechselt Yuki das Standbein,
  3-Zustand-State (`-1`/`0`/`+1` mit 15% Pause-Chance), smooth gelerpt
  (~1.5s half-life, `LOCO_LERP_PER_SEC=0.8`). Apply als additiver
  `spine.rotation.z += current * LOCO_MAX_RAD` (max ~1.1°), genau das Pattern
  vom Atem-Sinus, nur viel langsamer. Bewusst klein — sonst wirkt's wie
  Schwanken. Beide pausieren im Low/Off-Modus zusammen mit der Pose-Engine.

**Speaking-Mode** ✅ via `window.yukiAvatar.setSpeaking(bool)`: in `playReply` +
Replay-Knopf in `try/finally` gewickelt → Yuki slerpt während TTS frontal (`look_camera`),
Random pausiert; nach dem letzten Wort 1.5–3s Beruhigungspause
(`SETTLE_AFTER_SPEAK_MIN/MAX`), dann Random wieder.

**Listening-Mode** ✅ via `setListening(bool)`: bei PTT-pointerdown schaut sie nach
unten (`look_down` ≈ Mic-Knopf), bei pointerup/cancel zurück in den Random-Modus.

Persona-Wechsel triggert sofortigen Posenwechsel (nach 0.5s), damit der neue Charakter
im Körper sichtbar wird.

## Persona-Hintergrund ✅ (2026-05-31)

Vollflächiger Hintergrund hinter dem Avatar-Canvas (`z-index:-2`), wechselt pro
Persona mit weichem Cross-Fade. Zwei Schichten (`#persona-bg-a` + `#persona-bg-b`)
als Double-Buffer: `setPersonaBg(key)` stylt die inaktive Schicht und toggelt
dann `.show`, die CSS-`opacity`-Transition (.8s) macht den Übergang.

**Zwei Stufen pro Persona:**
1. **CSS-Gradient** aus `PERSONA_GRADIENTS`-Dict (immer da, kein Asset nötig).
   Atmosphärische Tönung, bewusst dunkel/dezent damit Avatar + Glas-Bubbles
   lesbar bleiben. 11 Einträge (alle Personas aus `yuki_core.PERSONAS`).
2. **Optionales Bild** unter `/avatar/backgrounds/<key>.<ext>` (jpg/jpeg/png/webp,
   in dieser Reihenfolge geprobt). Wenn vorhanden, wird es als zusätzliches
   `background-image` ÜBER den Gradient gelegt (`url(...), <gradient>`). Probe
   per `Image.onload`/`onerror`, Ergebnis pro Persona gecached → bei
   Nachträglichem-Hinzufügen Browser-Reload (Shift+F5) nötig.

**Backend:** Keine eigene Route — die existierende `@app.route("/avatar/<path:filename>")`
in `server.py` akzeptiert dank `<path:>`-Converter Sub-Pfade, liefert also
`avatar/backgrounds/*.jpg` direkt aus.

**Aufruf-Stellen** (analog zu `setPersona`):
- `loadPersonas()` beim ersten Page-Load
- `applyPersonaFromResponse()` wenn Yuki via `[persona:...]`-Marker selbst wechselt
- `personaSel.addEventListener("change", ...)` bei manuellem Dropdown-Wechsel

**Tuning:** `PERSONA_GRADIENTS`-Dict (Farben pro Persona),
`.persona-bg { transition: opacity .8s ease }` (Fade-Dauer).

**Bewusst NICHT:**
- Keine animierten Hintergründe (CPU/Battery, lenkt vom Avatar ab).
- Kein Wetter-/Tageszeit-bedingter Wechsel (kann später als zusätzlicher Layer
  drüber, wenn das Feature gewünscht wird).
- Bilder werden NICHT mit Three.js als Skybox/Plane gerendert — würde die
  Drag-/Zoom-/Wind-Logik durcheinanderbringen und ist auch ohne three-vrm sichtbar.

## Performance

Renderer mit `pixelRatio` gedeckelt auf 1.5, `antialias:false`, keine Schatten.
`outputColorSpace = SRGBColorSpace` für korrekte VRoid-Farben. Springbones (Haar/Kleidung)
bleiben an (langes Haar = viele Bones, aber auf Pixel-9-ähnlichen Geräten flüssig).

## Kamera-Framing

In `frameCamera()` – Kopf-Bone-y holen, Kamera bei `(0, hy-0.05, 1.65)`, lookAt
`(0, hy-0.35, 0)` → Kopf rutscht ins obere Drittel des Viewports, sitzt über den
glasigen Bubbles. FOV adaptiv: Portrait 32°, Landscape 28°.

## Tuning-Knöpfe (alles in der einen index.html)

- `REST_ARM_DEG` (Ruhe-Pose)
- `GAZE_INTERVAL_MIN/MAX`, `GAZE_RANGE_X/Y_UP/Y_DOWN`, `GAZE_SPEAKING_DAMP`, `GAZE_LERP_PER_SEC`, `GAZE_CENTER_CHANCE` (Random-Gaze)
- `LOCO_SHIFT_MIN/MAX`, `LOCO_LERP_PER_SEC`, `LOCO_MAX_RAD` (Locomotion)
- `ANIM_CROSSFADE_SECS`, `ANIM_START_DELAY`, `ANIM_FALLBACK_INTERVAL` (VRMA-Engine; finished-Event → direkt nächster Clip via Cross-Fade)
- `mouth = Math.min(1, rms * 4.5)` (Lipsync-Stärke)
- `nextBlinkIn = 3 + random()*3` (Blink-Frequenz)
- `PERSONA_EXPR`-Dict (Mimik)
- `POSES` + `PERSONA_POSES` (Posen-Library, identisch zur Desktop-Liste)
- `POSE_TRANSITION_SECS` (SLERP-Dauer)
- `POSE_INTERVAL_MIN/MAX` (8–15s Random-Frequenz)
- `SETTLE_AFTER_SPEAK_MIN/MAX` (1.5–3s Beruhigungspause nach TTS)
- `frameCamera()` (Kameraposition)
- `#log { margin-top: 28vh }` (Avatar-Sichtbarkeitsfenster oben)
- Bubble-Alpha (`rgba(...)` in `.me`/`.yuki`/`#persona`) und
  `blur(7px) saturate(120%)` (Glas-Anmutung)

## Bewusst NICHT (yet)

**Reaktive Gesten als Random-Pick im Idle:** Der `reactive`-Bucket
(`reaktive_gesten/`: Agreeing, Bow, Wave, Clapping, Pointing, Thinking,
Shrugging etc.) ist seit 2026-06-01 via `[gesture:KEY]`-Marker live —
Yuki triggert eine passende Geste pro Reply selbst (siehe BASE_RULES
"GESTURE MARKER"). Bewusst NICHT als Random-Auto-Pick im Idle, weil
Bow/Wave/Clap ohne Kontext zufällig in der Luft wirken würden.

## Config-Verzeichnis (`config/`, 2026-06-01)

Drei Dateien als Single-Source-of-Truth, alle vom Server via generischem
Endpoint `/config/<filename>` ausgeliefert, vom Frontend beim Boot über
`_loadConfigs()` parallel gefetched. Hardcoded Defaults im Code (yuki_core
und web/index.html) bleiben als Bootstrap-Sicherheit — falls eine Datei
mal fehlt/kaputt ist, läuft das System weiter.

- **`config/avatar.json`** – Animation-Mappings: `gesture_map`,
  `mood_clips`, `persona_clips`, `pool_boost` (Details siehe weiter
  unten). Greift in `animState.config`.
- **`config/moods.json`** – Yukis 16 Moods + VRM-Expression-Mapping.
  Source-of-Truth für `yuki_core.MOODS` (via `_load_moods_from_config()`
  beim Modul-Boot) und für `window.yukiAvatar`s `MOODS` (via
  `_loadConfigs`).
- **`config/personas.json`** – Persona→VRM-Expression-Default
  (`PERSONA_EXPR` im Frontend). Greift wenn kein Mood aktiv ist; Mood
  überlagert immer.

**Drei Sektionen:**

| Sektion | Effekt |
|---|---|
| `gesture_map` | `[gesture:KEY]` → `{file, desc}`. `file` per Suffix-Match (`name.endsWith(filename)`) im `reactive`-Bucket — Subdir-Pfade müssen nicht stimmen. `desc` zieht Server beim Boot in `BASE_RULES` über `{{GESTURES_LIST}}` (siehe `_load_gestures_from_config`) — neue Gesten hier eintragen reicht, kein Code-Touch in `yuki_core`. |
| `mood_clips` | Mood-Key → Liste Filenames. Werden in `_buildPickPool` mit `pool_boost.mood_boost`-Multiplier in den Idle-Pool gemischt, wenn `currentMood` matched. |
| `persona_clips` | Persona-Key → Liste Filenames. Analog mit `pool_boost.persona_boost` für `poseState.currentPersona`. |
| `pool_boost` | `mood_boost` / `persona_boost` = wie oft die Clips zusätzlich in den Idle-Pool getan werden. 0 = Feature aus. Defaults: 5 / 3. |

**Suffix-Match-Trick:** Engine sucht Clips per `endsWith(filename)`, also
findet sie auch wenn der Clip in einem beliebigen Unter-Ordner liegt.
User kann Clips zwischen `animations/`, `animations/vrma/idle/`,
`animations/vrma/stimmungs_idles/` etc. verschieben ohne die Config
anzupassen — solange der Filename gleich bleibt.

**Pool-Boost-Mathematik:** Pool wird pro Pick frisch gebaut. Base ist
der Idle-Bucket (z.B. 50 Clips). Pro Mood-Clip + `mood_boost=5` werden 5
weitere Slots dazu gehängt → 1 Mood-Clip + 50 Idle-Clips = 5/55 ≈ 9%
Chance pro Pick auf den Mood-Clip. Höhere Werte = häufiger; 0 schaltet
das Feature ab. Speaking/Listening/Reactive/Persona-Bucket laufen
unverändert ohne Boost.

**Persona-spezifische Clip-Whitelist:** Der `persona`-Bucket
(`persona-spezifisch/`: Dance-Clips, Victory/Defeat, Stretches) wird
ebenfalls nur geladen. Naheliegende Zuordnung wäre Macarena/Salsa/Swing/
Running Man → `party`-Persona, Victory/Defeat → `gamer`, Stretches →
`sport`/Tutor-Break. Implementierung steht aus – braucht entweder
Filename-Heuristik oder explizite Whitelist analog zu `PERSONA_POSES`.

**Mood-gefilterte Stimmungs-Idles:** ✅ seit 2026-06-01 erledigt über
`mood_clips` in `animations/config.json` + Pool-Boost im idle-Pool.
Defaults: happy/sad/tired/proud belegt; andere Moods laufen mit dem
generischen Pool.

**Dauerhafter Idle-Loop-Sub-Bucket:** User-Idee 2026-06-01 — speziell
markierte Clips (z.B. `idle_loop/`-Ordner) als "Heartbeat"-Pool für
ruhige Standing-Idles, die als Default zwischen den anderen Clips
gepickt werden. Aktuell wird einfach random aus dem ganzen idle-Bucket
gezogen, was bei kontrastreichen Clips ("Yawn" direkt nach "Cocky Head
Turn") springig wirken kann.

**Eigene Hand-Gesture-Engine zusätzlich zum Mixer** wurde 2026-05-31
versucht (Stufe 2.3) und am gleichen Tag wieder rausgenommen — die zwei
kämpften per-Frame gegeneinander und ließen die Arme schleichend
verutschen. Lehre: keine eigenen Per-Frame-Bone-Writer gegen den
VRMA-Mixer setzen.

## Animations-Debug-Panel (2026-06-02)

Über Options-Modal → „🎬 Animation testen" erreichbar. Pausiert Auto-
Vision, Proaktiv und Yukis Auto-Bucket-Picker (`animState.debugLock`),
damit man Clips in Ruhe begutachten kann ohne Kollision mit Reply-/
Listening-/Idle-Wechseln. Beim Schließen werden vorherige Toggle-Stände
exakt wiederhergestellt.

**UI-Layout:**
- Header mit ▁ (Minimieren auf Titelzeile) und ✕ (Schließen).
- Bucket-Filter-Dropdown (zeigt nur Clips eines Buckets, „Alle Buckets" als Default).
- Desktop: zusätzlicher Grid-Picker (`auto-fill, minmax(160px, 1fr)`),
  einklappbar via Toggle-Button — Default zugeklappt damit das Panel
  kompakt bleibt. Per `@media (pointer: fine)` wird das native `<select>`
  versteckt, beim Grid-Klick wird `animDebugSel.value` synchron gesetzt.
- Touch (`pointer: coarse`): Grid weg, natives Dropdown bleibt; Bucket
  und Clip nebeneinander in einer kompakten Zeile (38%/62%), Buttons
  in eigener Zeile darunter (damit Stop nicht aus dem Viewport rutscht).
- Steuerung: Loop-Toggle (Modus-Schalter, startet/restartet NICHTS),
  kombinierter Play/Pause-Button (3 States: idle / playing / paused) und
  Stop (führt nach `_restoreInitialPose` zurück in die Boot-Pose).
- Seekbarer Progress-Slider mit Time-Anzeige (rAF-Tick liest
  `action.time` und schreibt es in Slider+Label; während `seekDrag`
  pausiert das Auto-Update damit der User scrubben kann).
- Mood-Dropdown (alle Keys aus `MOODS` plus „Persona-Default" als Reset).
- 🔄 Refresh-Button: ruft `debugReloadAnimations()` → `mixer.stopAllAction()`
  + `uncacheRoot()` + `_loadAnimations()` neu (incl. `_loadConfigs()`).
  Nach Datei-Verschiebungen zwischen Bucket-Ordnern sofort wirksam ohne
  Page-Reload. Vorige Selektion in Clip- und Mood-Dropdown wird
  best-effort über Name-Match wiederhergestellt.

**Debug-API auf `window.yukiAvatar`:** `debugListClips()`,
`debugSetLock(on)`, `debugPlayClip(idx, loop)`, `debugSetLoop(on)`,
`debugStopClip()`, `debugGetPlayState()`, `debugSeekClip(t)`,
`debugSetPaused(on?)`, `debugListMoods()`, `debugReloadAnimations()`,
`debugListExpressions()`. Die Funktionen sind auch direkt in der
Browser-Konsole aufrufbar (z.B. `yukiAvatar.debugListExpressions()`).

**Auto-Start-Skip beim Reload:** `_loadAnimations()` startet am Ende
einen Idle-Clip. Beim Refresh aus dem Debug-Panel ist `animState.debugLock=true`,
der Auto-Start wird übersprungen. Beim Schließen des Panels setzt
`debugSetLock(false)` `nextChangeAt = elapsedTime + 0.2`, dann picked
`_pickAnim` normal aus dem aktuellen Bucket weiter.

**Initial-Pose-Snapshot:** `_captureInitialPose()` läuft am Ende von
`_initVRM` (nach Springbone-Reset + 30 Sub-Frames) und speichert
rotation+position aller `humanBones`. `debugStopClip` ruft
`_restoreInitialPose()` statt `vrm.humanoid.resetNormalizedPose()`, weil
letzteres VRoid-spezifische Bone-Defaults (z.B. leichte Knie-Beugung,
hips.position) wegnullt und Yuki sichtbar in die nackte T-Pose fallen
würde. Stolperfalle vom 2026-06-02: in einer Vorversion hatte
`_applyRestPose` `hips.position.set(0,0,0)` und nullte Bein-/Spine-Bones
— das verschob Yuki im Welt-Raum und ließ jede danach gespielte Clip-
Animation „zu hoch" wirken. `_applyRestPose` ist seitdem wieder minimal
(nur Arme + Finger), der saubere Reset läuft über den Snapshot.

## VRM-Expression `setValue` Case-Sensitivity

`vrm.expressionManager.setValue(name, value)` ist case-sensitive. VRoid-
Studio-Exporte legen Preset-Expressions teils unter PascalCase ab (z.B.
`Surprised` statt `surprised`) — in dem Fall taucht der Slot in
`expressionManager.expressionMap` unter dem Title-Case-Key auf und
greift bei `setValue('surprised', 1.0)` ins Leere (No-op, keine
Animation, kein Error).

**Fix in `web/index.html`:** Helper `_setExpressionValue(name, value)`
cached beim ersten Aufruf einen `lowercase → real-key`-Lookup über
`expressionMap` und gibt den echten Key an `setValue` weiter. Der Cache
wird in `_initVRM` invalidiert (greift bei Outfit-Switch / Reload). Die
Emotion-Schleife (`for (const e of EMOTIONS) ...`) läuft seit 2026-06-02
über den Helper; `aa` (Lipsync) und `blink` bleiben direkt, weil deren
Spec-Namen zuverlässig vorhanden sind.

**Diagnose-Hilfe:** `yukiAvatar.debugListExpressions()` in der Browser-
Konsole liefert `{preset: [...], custom: [...]}`. Slots unter `custom`
mit ungewöhnlichem Casing oder ganz anderen Namen (z.B. `wow`, `extra_a`)
können entweder per Alias-Map in `MOODS` referenziert werden oder
müssen zusätzlich in `EMOTIONS` (`web/index.html:3336`) aufgenommen
werden, sonst werden sie pro Frame nicht auf 0 zurückgesetzt und
„kleben" optisch.
