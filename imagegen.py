"""ComfyUI-Bild-Client fuer Yukis Gedankenbild (Traum-/Vorstellungs-Bild).
Standalone: nur requests + stdlib, KEINE yuki-Imports (kein Circular). Rendert ueber
das lokale Workflow-Template config/comfy_workflow.json (Michaels 'Save (API Format)'-
Export) und patcht nur Prompt/Negativ/Checkpoint/Seed per Graph-Rolle rein. Die 4070
faehrt nur vanilla ComfyUI. Degradiert still, wenn der Dienst aus ist. Siehe
docs/setup-comfyui-imagegen.md."""
import json
import random
import time
from pathlib import Path

import requests

_ROOT = Path(__file__).parent

_CONFIG_DEFAULTS = {
    "enabled": True,
    # server_urls (Array, top-down durchprobiert) ist der neue Weg; server_url
    # (Einzahl) bleibt als Fallback. server_urls MUSS hier als Default stehen,
    # sonst wirft load_imagegen_config den unbekannten Key aus der Datei weg.
    "server_urls": [],
    "server_url": "http://127.0.0.1:8000",
    "timeout_seconds": 120,
    "default_style": "painterly",
    "checkpoints": {"painterly": "DreamShaperXL1.0Alpha2_fixedVae_half_00001_.safetensors"},
    "negative_prompt": "lowres, bad anatomy, text, watermark, blurry",
}


def load_imagegen_config():
    """Routing-Tunables aus config/imagegen.json (live-reload), fehlende Keys -> Defaults."""
    cfg = dict(_CONFIG_DEFAULTS)
    cfg_path = _ROOT / "config" / "imagegen.json"
    if cfg_path.is_file():
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k in cfg:
                    if k in data:
                        cfg[k] = data[k]
        except (json.JSONDecodeError, OSError, ValueError, TypeError) as e:
            print(f"  [ImageGen-Config kaputt, Defaults bleiben: {e}]", flush=True)
    return cfg


def comfyui_servers(cfg=None):
    """Geordnete [(name, base_url), ...] aus der Config, von oben nach unten
    durchprobiert. Zwei Eintragsformen in server_urls: nackte URL-String ODER
    {"name","url"} (name optional, nur zur Orientierung). Faellt auf das Legacy-
    Einzel-server_url zurueck, wenn server_urls fehlt/leer ist. Eintraege ohne
    url werden ignoriert; base wird per rstrip('/') normalisiert."""
    cfg = cfg if cfg is not None else load_imagegen_config()
    out = []
    for item in (cfg.get("server_urls") or []):
        if isinstance(item, str):
            url, name = item.strip(), ""
        elif isinstance(item, dict):
            url, name = (item.get("url") or "").strip(), (item.get("name") or "").strip()
        else:
            continue
        if url:
            out.append((name, url.rstrip("/")))
    if out:
        return out
    single = (cfg.get("server_url") or "").strip()
    return [("", single.rstrip("/"))] if single else []


def _server_alive(base, timeout=3):
    """GET base/system_stats -> True bei HTTP 200. Alles andere/Fehler -> False."""
    try:
        r = requests.get(base.rstrip("/") + "/system_stats", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def pick_comfyui_server(cfg=None):
    """Erster per /system_stats erreichbarer (name, base) top-down, sonst None."""
    cfg = cfg if cfg is not None else load_imagegen_config()
    for name, base in comfyui_servers(cfg):
        if _server_alive(base):
            return (name, base)
    return None


def is_comfyui_reachable():
    """Schneller Health-Check ueber ALLE konfigurierten Server (top-down, stoppt beim
    ersten erreichbaren). False bei enabled=False oder wenn keiner antwortet."""
    cfg = load_imagegen_config()
    if not cfg.get("enabled", True):
        return False
    return pick_comfyui_server(cfg) is not None


def _load_template():
    """Michaels ComfyUI-API-Workflow (config/comfy_workflow.json). None wenn fehlt/kaputt
    -> generate faellt auf _fallback_workflow zurueck."""
    p = _ROOT / "config" / "comfy_workflow.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if (isinstance(data, dict) and data) else None
    except Exception:
        return None


def _nodes_by_class(graph, cls):
    return [nid for nid, n in graph.items()
            if isinstance(n, dict) and n.get("class_type") == cls]


def _patch_workflow(graph, prompt, negative, checkpoint, seed):
    """Prompt/Negativ/Checkpoint/Seed ins Template setzen - per Graph-ROLLE statt fixer
    Node-ID (robust gegen Re-Export mit anderer Nummerierung). Positiv/Negativ werden
    ueber die positive/negative-Links des KSamplers gefunden. Render-Parameter
    (steps/cfg/sampler/scheduler/Groesse) bleiben unangetastet (Michaels getunter Workflow)."""
    ks = _nodes_by_class(graph, "KSampler")
    if not ks:
        raise ValueError("workflow hat keinen KSampler")
    kin = graph[ks[0]]["inputs"]
    kin["seed"] = seed
    pos_id = (kin.get("positive") or [None])[0]
    neg_id = (kin.get("negative") or [None])[0]
    if pos_id in graph:
        graph[pos_id]["inputs"]["text"] = prompt
    if neg_id in graph and negative:
        graph[neg_id]["inputs"]["text"] = negative
    for cid in _nodes_by_class(graph, "CheckpointLoaderSimple"):
        graph[cid]["inputs"]["ckpt_name"] = checkpoint
    return graph


def _fallback_workflow(prompt, negative, checkpoint, seed):
    """Minimaler SDXL-txt2img-Graph, falls das Template fehlt/kaputt ist (Netz)."""
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": checkpoint}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 1024, "height": 1024, "batch_size": 1}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["1", 1]}},
        "4": {"class_type": "KSampler", "inputs": {
            "seed": seed, "steps": 30, "cfg": 8.0, "sampler_name": "euler",
            "scheduler": "simple", "denoise": 1.0,
            "model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["5", 0]}},
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["4", 0], "vae": ["1", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "yuki_gedankenbild", "images": ["6", 0]}},
    }


def _first_image(outputs):
    for node in (outputs or {}).values():
        for img in (node or {}).get("images", []) or []:
            if img.get("filename"):
                return img
    return None


def generate(prompt, style=None):
    """Prompt -> PNG-Bytes via ComfyUI (POST /prompt -> poll /history -> GET /view).
    None bei Fehler/Timeout/Dienst-aus. Blocking (~60s) - Aufrufer laeuft im Thread."""
    prompt = (prompt or "").strip()
    if not prompt:
        return None
    cfg = load_imagegen_config()
    if not cfg.get("enabled", True):
        return None
    picked = pick_comfyui_server(cfg)
    if not picked:
        print("  [ImageGen: kein ComfyUI-Server erreichbar]", flush=True)
        return None
    name, server = picked
    if name:
        print(f"  [ImageGen: rendere auf '{name}' ({server})]", flush=True)
    ckpts = cfg.get("checkpoints") or {}
    style = (style or cfg.get("default_style") or "painterly")
    checkpoint = ckpts.get(style) or ckpts.get(cfg.get("default_style")) \
        or next(iter(ckpts.values()), None)
    if not checkpoint:
        print("  [ImageGen: kein Checkpoint konfiguriert]", flush=True)
        return None
    seed = random.randint(0, 2**32 - 1)
    negative = cfg.get("negative_prompt", "")
    tmpl = _load_template()
    try:
        graph = _patch_workflow(tmpl, prompt, negative, checkpoint, seed) if tmpl \
            else _fallback_workflow(prompt, negative, checkpoint, seed)
    except Exception as e:
        print(f"  [ImageGen: Template kaputt ({e}) -> Fallback-Graph]", flush=True)
        graph = _fallback_workflow(prompt, negative, checkpoint, seed)
    try:
        r = requests.post(server + "/prompt", json={"prompt": graph}, timeout=15)
        if r.status_code != 200:
            print(f"  [ImageGen: /prompt HTTP {r.status_code}]", flush=True)
            return None
        pid = (r.json() or {}).get("prompt_id")
        if not pid:
            return None
        deadline = time.time() + int(cfg.get("timeout_seconds", 120))
        while time.time() < deadline:
            h = requests.get(f"{server}/history/{pid}", timeout=10)
            entry = (h.json() or {}).get(pid) if h.status_code == 200 else None
            if entry and entry.get("outputs"):
                img = _first_image(entry["outputs"])
                if not img:
                    return None
                v = requests.get(server + "/view", params={
                    "filename": img["filename"], "subfolder": img.get("subfolder", ""),
                    "type": img.get("type", "output")}, timeout=30)
                return v.content if (v.status_code == 200 and v.content) else None
            time.sleep(1.0)
        print("  [ImageGen: Timeout beim Rendern]", flush=True)
        return None
    except Exception as e:
        print(f"  [ImageGen-Fehler: {e}]", flush=True)
        return None
