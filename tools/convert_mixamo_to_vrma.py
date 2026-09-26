r"""
Mixamo-FBX -> VRMA-Konverter (Blender + saturday06 VRM-Addon v4.2.2).
=====================================================================
Aufruf NUR im Blender Scripting-Tab:
    1. Blender 4.4 oeffnen (VRM-Addon muss installiert und aktiv sein)
    2. Scripting-Tab (oben in der Leiste)
    3. "Open" (oder Text > Open) -> diese .py-Datei waehlen
    4. Skript-Ausfuehren-Button (Play-Icon im Editor) ODER Alt+P

Pipeline pro FBX:
    Import FBX -> Skalieren auf 0.01 (Mixamo ist 100x zu gross) ->
    Scale anwenden -> spec_version = "1.0" -> Auto-Bone-Assignment
    (saturday06's mixamo_mapping greift hier) -> VRMA-Export.

Wenn TEST_SINGLE_FILE gesetzt ist: nur diese eine Datei konvertieren
(fuer Pipeline-Validierung). Sonst: alle FBX rekursiv.

Pfade: SCRIPT_DIR hardcoded am Anfang der Konfiguration (Blender setzt
weder __file__ noch einen brauchbaren Text-Datablock-Filepath im
Scripting-Tab, daher kein Auto-Detect moeglich). Wenn das Skript
verschoben wird, SCRIPT_DIR anpassen. `mixamo/` (Input) und `vrma/`
(Output) leiten sich darunter ab. Skip-Check: bereits konvertierte VRMAs
werden uebersprungen; FORCE_REBUILD=True erzwingt Neu-Konvertierung.

Output-Struktur spiegelt Input-Struktur:
    mixamo\idle\Standing Idle.fbx -> vrma\idle\Standing Idle.vrma
"""
import bpy
import os
import sys
import traceback
from pathlib import Path

# ============================================================================
# KONFIGURATION
# ============================================================================
# SCRIPT_DIR = dein ARBEITS-Ordner (unabhaengig davon, wo dieses Skript liegt).
# Darunter: `mixamo/` = deine heruntergeladenen Mixamo-FBX (Input), `vrma/` =
# konvertierter Output. Blender setzt __file__ nicht (kein Auto-Detect im
# Scripting-Tab), daher hier EXPLIZIT setzen ODER per Umgebungsvariable
# YUKI_ANIM_DIR. >>> HIER anpassen: <<<
SCRIPT_DIR  = Path(os.environ.get("YUKI_ANIM_DIR", r"C:\yuki-mixamo"))
MIXAMO_ROOT = SCRIPT_DIR / "mixamo"
VRMA_ROOT   = SCRIPT_DIR / "vrma"

# Mixamo's "X Bot.fbx" ist die T-Pose-Skelett-Datei (kein Animation-Inhalt) -
# wird im Pack-Download automatisch mitgeliefert.
SKIP_NAMES = {"X Bot.fbx"}

# Wenn True: bereits vorhandene VRMA-Output-Dateien werden ueberschrieben.
# Default False: bereits konvertierte FBX werden uebersprungen (typischer
# Workflow - User legt neue FBX in mixamo/ ab und laesst Skript laufen, nur
# die neuen werden konvertiert).
FORCE_REBUILD = False

# TEST-MODUS: solange diese Variable nicht None ist, wird NUR diese eine
# Datei konvertiert (Skip-Check wird im Test ignoriert, Test erzwingt
# immer Re-Convert). Erst wenn der Test durchgegangen ist und die VRMA in
# three-vrm anstaendig aussieht: auf None setzen fuer Batch-Lauf.
TEST_SINGLE_FILE = None   # <- Batch-Lauf: konvertiert alle FBX rekursiv
#TEST_SINGLE_FILE = MIXAMO_ROOT / "stimmungs_idles" / "Sad Idle_kick.fbx"


# ============================================================================
# HILFSFUNKTIONEN
# ============================================================================
def clear_scene():
    """Loescht alle Objekte und Orphan-Datenbloecke aus der Szene.
    Wichtig zwischen den Konvertierungen, sonst sammeln sich Armatures an."""
    if bpy.context.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    for block in list(bpy.data.armatures):
        bpy.data.armatures.remove(block)
    for block in list(bpy.data.actions):
        bpy.data.actions.remove(block)
    for block in list(bpy.data.meshes):
        bpy.data.meshes.remove(block)
    for block in list(bpy.data.materials):
        bpy.data.materials.remove(block)
    for block in list(bpy.data.images):
        bpy.data.images.remove(block)


def find_imported_armature():
    """Sucht die nach dem FBX-Import entstandene Armature im Scene-Graph."""
    for obj in bpy.context.scene.objects:
        if obj.type == 'ARMATURE':
            return obj
    return None


def convert_one(fbx_path: Path, vrma_path: Path) -> bool:
    """Konvertiert eine einzelne FBX-Datei nach VRMA. Returns True bei Erfolg."""
    print(f"\n--- {fbx_path.name} ---")
    print(f"    -> {vrma_path}")

    clear_scene()

    # 1. FBX importieren
    try:
        bpy.ops.import_scene.fbx(filepath=str(fbx_path))
    except Exception as e:
        print(f"    [FAIL] FBX-Import: {e}")
        return False

    armature = find_imported_armature()
    if armature is None:
        print("    [FAIL] Keine Armature im FBX gefunden")
        return False
    print(f"    Armature: {armature.name} ({len(armature.data.bones)} Bones)")

    # 2. Skalieren auf 0.01 (Mixamo ist standardmaessig in cm, VRM in m)
    armature.scale = (0.01, 0.01, 0.01)
    bpy.context.view_layer.objects.active = armature
    bpy.ops.object.select_all(action='DESELECT')
    armature.select_set(True)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)

    # 3. Armature als VRM 1.0 markieren
    armature_data = armature.data
    try:
        ext = armature_data.vrm_addon_extension
        ext.spec_version = '1.0'
    except Exception as e:
        print(f"    [FAIL] spec_version setzen: {e}")
        print(f"    (Ist das VRM-Addon aktiv? Edit > Preferences > Add-ons > 'VRM format')")
        return False

    # 4. Human Bones automatisch zuweisen - saturday06's mixamo_mapping greift hier
    #    (siehe common/human_bone_mapper/mixamo_mapping.py)
    try:
        result = bpy.ops.vrm.assign_vrm1_humanoid_human_bones_automatically(
            armature_object_name=armature.name
        )
        if 'CANCELLED' in result:
            print("    [FAIL] Bone-Assignment CANCELLED - vermutlich nicht genug")
            print("           Mixamo-Bones erkannt. Pruef FBX-Skelett-Naming.")
            print(f"           Bone-Namen im FBX (erste 5): "
                  f"{[b.name for b in armature.data.bones[:5]]}")
            return False
    except Exception as e:
        print(f"    [FAIL] Auto-Bone-Assignment: {e}")
        return False

    # Validieren: sind ALLE required Human-Bones jetzt zugewiesen? Wenn nicht,
    # canceled der VRMA-Exporter spaeter still - hier vorab abfangen mit
    # konkreter Liste was fehlt.
    #
    # Stolperfalle (2026-06-02): aeltere VRM-Addon-Versionen hatten
    # hb.specification().requirement, neuere (>=v3) haben das entfernt. Wir
    # versuchen die alte API; bei AttributeError listen wir konservativ
    # alle un-assigned Bones (ohne Required/Optional-Unterscheidung) - reicht
    # zur Diagnose, fuer den Skip ist es eh egal warum.
    human_bones = ext.vrm1.humanoid.human_bones
    if not human_bones.bones_are_correctly_assigned():
        hb_map = human_bones.human_bone_name_to_human_bone()
        missing = []
        for hb_name, hb in hb_map.items():
            if hb.node.bone_name:
                continue
            try:
                if hb.specification().requirement:
                    missing.append(str(hb_name))
            except AttributeError:
                missing.append(str(hb_name))
        print(f"    [FAIL] Human-Bones nicht zugewiesen: {missing}")
        print(f"           Bone-Namen im FBX (erste 10): "
              f"{[b.name for b in armature.data.bones[:10]]}")
        return False

    # 4.5 Translation-FCurves behandeln:
    #     - Hip-Translation BEHALTEN (Mixamo nutzt sie fuer Body-Sway, Gewichts-
    #       verlagerung, Schritte) und von cm auf m skalieren. Ohne die wird
    #       Yuki "eingefroren": Huefte starr, aber die Fuesse muessen die fehlende
    #       Verschiebung kompensieren -> Beine rutschen.
    #     - Alle anderen Bone-Locations RAUS. Mixamo schreibt fuer ~60 Bones
    #       je X/Y/Z, fast alle konstant - die haben in einer Humanoid-Animation
    #       nichts verloren (Bone-Laengen sind statisch durchs Rig).
    #
    #     Warum die Skalierung noetig: transform_apply(scale=True) oben skaliert
    #     Bone-Lengths + Rest-Positions, NICHT die Animation-FCurve-Werte. Die
    #     bleiben in den Original-Mixamo-cm und sind 100x zu gross fuer VRMA (m).
    if armature.animation_data and armature.animation_data.action:
        action = armature.animation_data.action
        hips_bone_name = ext.vrm1.humanoid.human_bones.hips.node.bone_name
        hips_path = f'pose.bones["{hips_bone_name}"].location'
        removed = 0
        scaled = 0
        for fc in list(action.fcurves):
            if fc.data_path == hips_path:
                # Hip-Translation: cm -> m
                for kp in fc.keyframe_points:
                    kp.co[1] /= 100.0
                    kp.handle_left[1]  /= 100.0
                    kp.handle_right[1] /= 100.0
                fc.update()
                scaled += 1
            elif fc.data_path.endswith('.location'):
                action.fcurves.remove(fc)
                removed += 1
        print(f"    Hip-Translation: {scaled} FCurves cm->m skaliert, {removed} weitere Locations entfernt")

        # 4.6 Scene-Frame-Range auf die echte Action-Laenge setzen.
        # Stolperfalle (2026-06-02): Blenders Default-Range ist 1..250 (= 8.33s
        # @ 30fps). Der VRMA-Exporter sampled diesen Scene-Range, NICHT die
        # Action-Range. Ohne diesen Fix wurden alle 47 Clips als 8.33s
        # exportiert - kuerzere Animationen mit nachgeklemmter Stand-Pose,
        # laengere hinten abgeschnitten. Action.frame_range liefert Floats,
        # wir runden auf den naechsten ganzen Frame (start abrunden, end
        # aufrunden, damit kein letzter Keyframe rausfaellt).
        import math
        fr_start, fr_end = action.frame_range
        bpy.context.scene.frame_start = int(math.floor(fr_start))
        bpy.context.scene.frame_end   = int(math.ceil(fr_end))
        fps = bpy.context.scene.render.fps
        dur = (bpy.context.scene.frame_end - bpy.context.scene.frame_start) / fps
        print(f"    Scene-Frame-Range: {bpy.context.scene.frame_start}..{bpy.context.scene.frame_end} "
              f"({dur:.2f}s @ {fps}fps)")

    # 5. VRMA-Export
    vrma_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        bpy.ops.export_scene.vrma(
            filepath=str(vrma_path),
            armature_object_name=armature.name,
        )
    except Exception as e:
        print(f"    [FAIL] VRMA-Export: {e}")
        traceback.print_exc()
        return False

    if vrma_path.exists():
        size_kb = vrma_path.stat().st_size / 1024
        print(f"    [OK] geschrieben ({size_kb:.1f} KB)")
        return True
    print(f"    [FAIL] VRMA-Datei wurde nicht erstellt")
    return False


# ============================================================================
# MAIN
# ============================================================================
def main():
    print(f"SCRIPT_DIR  : {SCRIPT_DIR}")
    print(f"MIXAMO_ROOT : {MIXAMO_ROOT}")
    print(f"VRMA_ROOT   : {VRMA_ROOT}")
    if not MIXAMO_ROOT.is_dir():
        print(f"[FAIL] MIXAMO_ROOT ist kein Verzeichnis - liegt das Skript "
              f"neben einem mixamo/-Ordner?")
        return

    if TEST_SINGLE_FILE is not None:
        if not TEST_SINGLE_FILE.exists():
            print(f"[FAIL] Test-Datei nicht gefunden: {TEST_SINGLE_FILE}")
            return
        rel = TEST_SINGLE_FILE.relative_to(MIXAMO_ROOT)
        out = VRMA_ROOT / rel.with_suffix('.vrma')
        print(f"=== TEST-MODUS: nur {rel} ===")
        # Test ignoriert Skip-Check absichtlich - Test soll IMMER konvertieren.
        ok = convert_one(TEST_SINGLE_FILE, out)
        print(f"\n=== Ergebnis: {'OK' if ok else 'FEHLER'} ===")
        if ok:
            print(f"VRMA: {out}")
            print("Naechster Schritt: VRMA in three-vrm laden und pruefen.")
            print("Wenn ok: TEST_SINGLE_FILE oben auf None setzen und neu starten.")
        return

    # Batch-Modus
    fbx_files = sorted(MIXAMO_ROOT.rglob("*.fbx"))
    fbx_files = [f for f in fbx_files if f.name not in SKIP_NAMES]
    print(f"=== BATCH-MODUS: {len(fbx_files)} FBX-Dateien ===")
    print(f"FORCE_REBUILD={FORCE_REBUILD} "
          f"({'alles neu konvertieren' if FORCE_REBUILD else 'nur fehlende konvertieren'})\n")
    ok = fail = skipped = 0
    failed_paths = []
    for fbx in fbx_files:
        rel = fbx.relative_to(MIXAMO_ROOT)
        out = VRMA_ROOT / rel.with_suffix('.vrma')
        # Skip-Check: bereits konvertierte VRMAs ueberspringen, ausser
        # FORCE_REBUILD ist gesetzt. >0 Byte ist Schutz gegen abgebrochene
        # Konvertierungen (leere Datei -> doch nochmal versuchen).
        if not FORCE_REBUILD and out.exists() and out.stat().st_size > 0:
            size_kb = out.stat().st_size / 1024
            print(f"--- {fbx.name} ---")
            print(f"    [SKIP] VRMA existiert bereits ({size_kb:.1f} KB)")
            skipped += 1
            continue
        if convert_one(fbx, out):
            ok += 1
        else:
            fail += 1
            failed_paths.append(str(rel))
    print(f"\n=== Fertig: {ok} ok, {skipped} skipped, {fail} fail ===")
    if failed_paths:
        print("Fehlgeschlagene Dateien:")
        for p in failed_paths:
            print(f"  - {p}")


if __name__ == "__main__":
    main()
