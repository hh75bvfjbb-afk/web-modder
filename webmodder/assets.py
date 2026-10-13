"""Free game assets without fal: each recipe walks a chain of sources and uses the first one that works.

Tuned for a mid-range PC (an 8 GB GPU such as an RTX 4060):

    sprite / image  FreeLLMAPI (free image providers) -> local ComfyUI (SDXL) -> fal
                    then: flood-fill cut-out (um sprite cutout) or the local ISNet model, and an exact fit
    texture         as image, then `um sprite seamless`
    rmbg            local ISNet (onnxruntime, CPU is enough)
    upscale         local Real-ESRGAN (ncnn/Vulkan: any GPU)
    sfx             Freesound (free API key, real CC recordings) -> ComfyUI Stable Audio Open -> fal
    music           ComfyUI ACE-Step -> fal
    voice           edge-tts (free, Russian voices) -> FreeLLMAPI speech -> fal
    model3d         ComfyUI Hunyuan3D-2 mini (shape) -> fal (textured)
    video           FreeLLMAPI video -> fal

Every file gets a line in <out>/assets_manifest.jsonl (source, prompt, licence), and Freesound sounds are
credited in <out>/CREDITS.md.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import random
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path

from webmodder import toolkit
from webmodder.config import data_dir

RECIPES = ["status", "sprite", "image", "texture", "rmbg", "upscale", "sfx", "music", "voice", "model3d", "video"]
UA = {"User-Agent": "web-modder"}


class Skip(Exception):
    """This source can't serve the request right now; the next one in the chain is tried."""


class AssetError(Exception):
    pass


# ============================================================================ plumbing

def _http(url: str, body=None, headers: dict | None = None, timeout: float = 120, method: str | None = None) -> tuple[bytes, dict]:
    data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
    h = {**UA, **(headers or {})}
    if isinstance(body, (dict, list)):
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method=method or ("POST" if data is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        raise Skip(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise Skip(f"нет связи: {e}")


def _slug(text: str) -> str:
    keep = "".join(c if c.isalnum() else "_" for c in text.lower())[:40].strip("_")
    return keep or "asset"


class Ctx:
    def __init__(self, workdir: Path, pool=None, log=None, stop=None):
        self.workdir = Path(workdir)
        self.pool = pool
        self.log = log or (lambda m: None)
        self.stop = stop

    def key(self, provider: str) -> dict | None:
        return self.pool.acquire(provider) if self.pool else None

    def out(self, out: str | None) -> Path:
        p = Path(out or "assets/gen")
        p = p if p.is_absolute() else self.workdir / p
        p.mkdir(parents=True, exist_ok=True)
        return p


def _record(out: Path, **rec):
    rec = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), **rec}
    with open(out / "assets_manifest.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _um(args: list[str], ctx: Ctx, timeout: float = 1800) -> str:
    fal = ctx.key("fal")
    r = subprocess.run(toolkit.um_argv(args), cwd=str(ctx.workdir), env=toolkit.env(fal["secret"] if fal else None),
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if r.returncode:
        raise Skip((r.stderr or r.stdout).strip()[-500:])
    return r.stdout


def _unique(out: Path, name: str, ext: str) -> Path:
    p = out / f"{name}{ext}"
    i = 2
    while p.exists():
        p = out / f"{name}_{i}{ext}"
        i += 1
    return p


# ============================================================================ FreeLLMAPI media

def _fl(ctx: Ctx) -> tuple[str, dict]:
    k = ctx.key("freellmapi")
    if not k:
        raise Skip("нет ключа FreeLLMAPI")
    return (k.get("base") or "http://localhost:31415/v1").rstrip("/"), {"Authorization": "Bearer " + k["secret"]}


def fl_image(ctx: Ctx, prompt: str, size: str = "1024x1024") -> bytes:
    base, h = _fl(ctx)
    raw, _ = _http(base + "/images/generations", {"model": "auto", "prompt": prompt, "n": 1, "size": size,
                                                  "response_format": "b64_json"}, h, timeout=300)
    data = (json.loads(raw).get("data") or [{}])[0]
    if data.get("b64_json"):
        return base64.b64decode(data["b64_json"])
    if data.get("url"):
        return _http(data["url"], timeout=120)[0]
    raise Skip("FreeLLMAPI вернул пустой ответ")


def fl_speech(ctx: Ctx, text: str, voice: str | None) -> bytes:
    base, h = _fl(ctx)
    body = {"model": "auto", "input": text, "response_format": "mp3"}
    if voice:
        body["voice"] = voice
    return _http(base + "/audio/speech", body, h, timeout=300)[0]


def fl_video(ctx: Ctx, prompt: str, seconds: int) -> bytes:
    base, h = _fl(ctx)
    return _http(base + "/videos/generations", {"model": "auto", "prompt": prompt, "duration": int(seconds)}, h, timeout=330)[0]


# ============================================================================ ComfyUI (local GPU)

_COMFY: dict = {}


def comfy_url() -> str:
    """COMFYUI_URL, else whichever answers: ComfyUI Desktop listens on 8000, the portable/manual install on 8188."""
    if os.environ.get("COMFYUI_URL"):
        return os.environ["COMFYUI_URL"].rstrip("/")
    if _COMFY.get("url") and time.time() - _COMFY.get("t", 0) < 30:
        return _COMFY["url"]
    found = "http://127.0.0.1:8188"
    for port in (8188, 8000):
        url = f"http://127.0.0.1:{port}"
        try:
            raw, _ = _http(url + "/system_stats", timeout=2)
            if b"comfyui_version" in raw or b"devices" in raw:
                found = url
                break
        except Skip:
            continue
    _COMFY.update(url=found, t=time.time())
    return found


def comfy_up() -> bool:
    try:
        _http(comfy_url() + "/system_stats", timeout=3)
        return True
    except Skip:
        return False


def comfy_models(folder: str) -> list[str]:
    try:
        raw, _ = _http(comfy_url() + f"/models/{folder}", timeout=10)
        return [str(x) for x in json.loads(raw)]
    except (Skip, ValueError):
        return []


def _pick(names: list[str], want: tuple[str, ...], avoid: tuple[str, ...] = ()) -> str | None:
    low = [(n, n.lower()) for n in names]
    for w in want:
        for n, l in low:
            if w in l and not any(a in l for a in avoid):
                return n
    return None


AUDIO_3D = ("ace_step", "stable_audio", "hunyuan3d", "zero123", "svd", "wan", "ltx", "video", "audio")


def comfy_upload(path: Path) -> str:
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{path.name}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n").encode() + path.read_bytes() + \
        f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue\r\n--{boundary}--\r\n".encode()
    raw, _ = _http(comfy_url() + "/upload/image", body, {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    return json.loads(raw)["name"]


def comfy_run(wf: dict, timeout: float = 1800, ctx: Ctx | None = None) -> list[tuple[str, bytes]]:
    """Queue an API-format workflow, wait, return [(filename, bytes)] of everything it saved."""
    base = comfy_url()
    raw, _ = _http(base + "/prompt", {"prompt": wf, "client_id": uuid.uuid4().hex}, timeout=60)
    res = json.loads(raw)
    if res.get("node_errors"):
        raise Skip("ComfyUI отклонил схему: " + json.dumps(res["node_errors"], ensure_ascii=False)[:500])
    pid = res["prompt_id"]
    end = time.time() + timeout
    while time.time() < end:
        if ctx and ctx.stop and ctx.stop.is_set():
            _http(base + "/interrupt", {}, timeout=10)
            raise AssetError("остановлено пользователем")
        h = json.loads(_http(base + f"/history/{pid}", timeout=30)[0]).get(pid)
        if h:
            st = h.get("status", {})
            if st.get("status_str") == "error":
                msg = next((d for kind, d in st.get("messages", []) if kind == "execution_error"), {})
                raise Skip(f"ComfyUI: ошибка в узле {msg.get('node_type', '?')}: {msg.get('exception_message', '')[:300]}")
            if st.get("completed", True):
                files = []
                for node in sorted(h.get("outputs", {}), key=lambda k: (len(k), k)):
                    for items in h["outputs"][node].values():
                        for f in items if isinstance(items, list) else []:
                            if isinstance(f, dict) and f.get("filename"):
                                q = urllib.parse.urlencode({"filename": f["filename"], "subfolder": f.get("subfolder", ""),
                                                            "type": f.get("type", "output")})
                                files.append((f["filename"], _http(base + f"/view?{q}", timeout=600)[0]))
                if not files:
                    raise Skip("ComfyUI ничего не сохранил")
                return files
        time.sleep(1.5)
    raise Skip(f"ComfyUI не успел за {timeout:.0f} с")


def _seed() -> int:
    return random.randint(0, 2**31 - 1)


def comfy_image(ctx: Ctx, prompt: str, size: str = "1024x1024", negative: str = "") -> bytes:
    if not comfy_up():
        raise Skip(f"ComfyUI не запущен ({comfy_url()})")
    ckpt = _pick(comfy_models("checkpoints"), ("xl", "flux", "dream", "juggernaut", "pony", "illustrious", "sd", ""), AUDIO_3D)
    if not ckpt:
        raise Skip("в ComfyUI нет модели для картинок (models/checkpoints): нужна SDXL")
    w, h = (int(x) for x in size.lower().split("x"))
    wf = {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": negative or "blurry, low quality, watermark, text, signature",
                                                          "clip": ["4", 1]}},
        "3": {"class_type": "KSampler", "inputs": {"seed": _seed(), "steps": 28, "cfg": 6.5, "sampler_name": "dpmpp_2m",
                                                    "scheduler": "karras", "denoise": 1.0, "model": ["4", 0],
                                                    "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "wm", "images": ["8", 0]}},
    }
    return comfy_run(wf, 900, ctx)[0][1]


def comfy_sfx(ctx: Ctx, prompt: str, seconds: float) -> tuple[str, bytes]:
    """ComfyUI's own Stable Audio Open example graph (ComfyUI_examples/audio)."""
    if not comfy_up():
        raise Skip(f"ComfyUI не запущен ({comfy_url()})")
    ckpt = _pick(comfy_models("checkpoints"), ("stable_audio",))
    t5 = _pick(comfy_models("text_encoders") + comfy_models("clip"), ("t5_base", "t5-base", "t5"))
    if not ckpt or not t5:
        raise Skip("в ComfyUI нет Stable Audio Open (checkpoints/stable_audio_open_1.0.safetensors + "
                   "text_encoders/t5_base.safetensors)")
    wf = {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "10": {"class_type": "CLIPLoader", "inputs": {"clip_name": t5, "type": "stable_audio"}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["10", 0]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["10", 0]}},
        "11": {"class_type": "EmptyLatentAudio", "inputs": {"seconds": max(1.0, min(float(seconds), 47.0)), "batch_size": 1}},
        "3": {"class_type": "KSampler", "inputs": {"seed": _seed(), "steps": 50, "cfg": 4.98, "sampler_name": "dpmpp_3m_sde_gpu",
                                                    "scheduler": "exponential", "denoise": 1.0, "model": ["4", 0],
                                                    "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["11", 0]}},
        "12": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "13": {"class_type": "SaveAudio", "inputs": {"filename_prefix": "audio/wm", "audio": ["12", 0]}},
    }
    return comfy_run(wf, 900, ctx)[0]


def comfy_music(ctx: Ctx, tags: str, lyrics: str, seconds: float, bpm: int = 120) -> tuple[str, bytes]:
    """ACE-Step 1.5 turbo (8 steps) when present, else ACE-Step v1: ComfyUI's own template graphs."""
    if not comfy_up():
        raise Skip(f"ComfyUI не запущен ({comfy_url()})")
    ck = comfy_models("checkpoints")
    v15 = _pick(ck, ("ace_step_1.5", "ace_step_1_5", "ace-step-1.5"))
    v1 = _pick(ck, ("ace_step",), ("1.5", "1_5"))
    if not (v15 or v1):
        raise Skip("в ComfyUI нет ACE-Step (checkpoints/ace_step_1.5_turbo_aio.safetensors или ace_step_v1_3.5b.safetensors)")
    seconds = max(5.0, min(float(seconds), 240.0))
    seed = _seed()
    if v15:
        wf = {
            "97": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": v15}},
            "78": {"class_type": "ModelSamplingAuraFlow", "inputs": {"shift": 3.0, "model": ["97", 0]}},
            "94": {"class_type": "TextEncodeAceStepAudio1.5", "inputs": {
                "clip": ["97", 1], "tags": tags, "lyrics": lyrics or "[Instrumental]", "seed": seed, "bpm": int(bpm),
                "duration": seconds, "timesignature": "4", "language": "en", "keyscale": "C major",
                "generate_audio_codes": True, "cfg_scale": 2.0, "temperature": 0.85, "top_p": 0.9, "top_k": 0, "min_p": 0.0}},
            "47": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["94", 0]}},
            "98": {"class_type": "EmptyAceStep1.5LatentAudio", "inputs": {"seconds": seconds, "batch_size": 1}},
            "3": {"class_type": "KSampler", "inputs": {"seed": seed, "steps": 8, "cfg": 1.0, "sampler_name": "euler",
                                                        "scheduler": "simple", "denoise": 1.0, "model": ["78", 0],
                                                        "positive": ["94", 0], "negative": ["47", 0], "latent_image": ["98", 0]}},
            "18": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["3", 0], "vae": ["97", 2]}},
            "19": {"class_type": "SaveAudio", "inputs": {"filename_prefix": "audio/wm", "audio": ["18", 0]}},
        }
    else:
        wf = {
            "40": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": v1}},
            "45": {"class_type": "ModelSamplingSD3", "inputs": {"shift": 5.0, "model": ["40", 0]}},
            "61": {"class_type": "LatentOperationTonemapReinhard", "inputs": {"multiplier": 1.0}},
            "62": {"class_type": "LatentApplyOperationCFG", "inputs": {"model": ["45", 0], "operation": ["61", 0]}},
            "14": {"class_type": "TextEncodeAceStepAudio", "inputs": {"tags": tags, "lyrics": lyrics or "[instrumental]",
                                                                       "lyrics_strength": 0.99, "clip": ["40", 1]}},
            "47": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["14", 0]}},
            "17": {"class_type": "EmptyAceStepLatentAudio", "inputs": {"seconds": seconds, "batch_size": 1}},
            "3": {"class_type": "KSampler", "inputs": {"seed": seed, "steps": 50, "cfg": 5.0, "sampler_name": "euler",
                                                        "scheduler": "simple", "denoise": 1.0, "model": ["62", 0],
                                                        "positive": ["14", 0], "negative": ["47", 0], "latent_image": ["17", 0]}},
            "18": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["3", 0], "vae": ["40", 2]}},
            "19": {"class_type": "SaveAudio", "inputs": {"filename_prefix": "audio/wm", "audio": ["18", 0]}},
        }
    return comfy_run(wf, 1800, ctx)[0]


def comfy_model3d(ctx: Ctx, image: Path) -> tuple[str, bytes]:
    """Hunyuan3D-2 (hunyuan3d-dit-v2_fp16, 4.6 GB, fits 8 GB): image -> untextured mesh (GLB).
    Parameters from ComfyUI's template 3d_hunyuan3d_image_to_model."""
    if not comfy_up():
        raise Skip(f"ComfyUI не запущен ({comfy_url()})")
    ckpt = _pick(comfy_models("checkpoints"), ("hunyuan3d-dit-v2_fp16", "hunyuan3d-dit-v2", "hunyuan3d"), ("mv",))
    if not ckpt:
        raise Skip("в ComfyUI нет Hunyuan3D-2 (checkpoints/hunyuan3d-dit-v2_fp16.safetensors)")
    name = comfy_upload(image)
    wf = {
        "1": {"class_type": "ImageOnlyCheckpointLoader", "inputs": {"ckpt_name": ckpt}},
        "2": {"class_type": "LoadImage", "inputs": {"image": name}},
        "3": {"class_type": "CLIPVisionEncode", "inputs": {"clip_vision": ["1", 1], "image": ["2", 0], "crop": "none"}},
        "4": {"class_type": "Hunyuan3Dv2Conditioning", "inputs": {"clip_vision_output": ["3", 0]}},
        "5": {"class_type": "EmptyLatentHunyuan3Dv2", "inputs": {"resolution": 3072, "batch_size": 1}},
        "6": {"class_type": "ModelSamplingAuraFlow", "inputs": {"shift": 1.0, "model": ["1", 0]}},
        "7": {"class_type": "KSampler", "inputs": {"seed": _seed(), "steps": 20, "cfg": 8.0, "sampler_name": "euler",
                                                    "scheduler": "normal", "denoise": 1.0, "model": ["6", 0],
                                                    "positive": ["4", 0], "negative": ["4", 1], "latent_image": ["5", 0]}},
        "8": {"class_type": "VAEDecodeHunyuan3D", "inputs": {"samples": ["7", 0], "vae": ["1", 2], "num_chunks": 8000,
                                                              "octree_resolution": 256}},
        "9": {"class_type": "VoxelToMesh", "inputs": {"voxel": ["8", 0], "algorithm": "surface net", "threshold": 0.6}},
        "10": {"class_type": "SaveGLB", "inputs": {"mesh": ["9", 0], "filename_prefix": "mesh/wm"}},
    }
    return comfy_run(wf, 1800, ctx)[0]


# ============================================================================ local tools

def _download(url: str, dest: Path, ctx: Ctx, what: str):
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    ctx.log(f"скачиваю {what} (один раз): {url}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=600) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
    os.replace(tmp, dest)
    return dest


ISNET = {"general": "https://github.com/danielgatis/rembg/releases/download/v0.0.0/isnet-general-use.onnx",
         "anime": "https://github.com/danielgatis/rembg/releases/download/v0.0.0/isnet-anime.onnx"}


def rmbg(ctx: Ctx, src: Path, dst: Path, model: str = "general") -> Path:
    """Background removal with ISNet (the model rembg uses), run on onnxruntime: CPU is fine for sprites."""
    import numpy as np
    from PIL import Image

    try:
        import onnxruntime as ort
    except ImportError:
        raise Skip("нет onnxruntime (pip install onnxruntime)")
    onnx = _download(ISNET.get(model, ISNET["general"]), data_dir() / "models" / f"isnet-{model}.onnx", ctx, "модель удаления фона")
    providers = [p for p in ("CUDAExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider")
                 if p in ort.get_available_providers()]
    sess = ort.InferenceSession(str(onnx), providers=providers)
    im = Image.open(src).convert("RGB")
    x = np.asarray(im.resize((1024, 1024), Image.LANCZOS), dtype=np.float32) / 255.0
    x = (x - 0.5) / 1.0
    x = x.transpose(2, 0, 1)[None]
    pred = sess.run(None, {sess.get_inputs()[0].name: x})[0][:, 0]
    pred = (pred - pred.min()) / max(float(pred.max() - pred.min()), 1e-6)
    mask = Image.fromarray((np.squeeze(pred) * 255).astype("uint8")).resize(im.size, Image.LANCZOS)
    out = Image.open(src).convert("RGBA")
    out.putalpha(mask)
    out.save(dst)
    return dst


ESRGAN = {"win32": "realesrgan-ncnn-vulkan-20220424-windows.zip", "linux": "realesrgan-ncnn-vulkan-20220424-ubuntu.zip",
          "darwin": "realesrgan-ncnn-vulkan-20220424-macos.zip"}


def upscale(ctx: Ctx, src: Path, dst: Path, scale: int = 4, anime: bool = True) -> Path:
    plat = "win32" if os.name == "nt" else ("darwin" if sys.platform == "darwin" else "linux")
    root = data_dir() / "tools" / "realesrgan"
    exe = root / ("realesrgan-ncnn-vulkan.exe" if os.name == "nt" else "realesrgan-ncnn-vulkan")
    if not exe.exists():
        z = _download("https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/" + ESRGAN[plat],
                      data_dir() / "tools" / ESRGAN[plat], ctx, "Real-ESRGAN")
        with zipfile.ZipFile(z) as zf:
            zf.extractall(root)
        found = next(root.rglob(exe.name), None)
        if found and found != exe:
            for item in found.parent.iterdir():
                shutil.move(str(item), root / item.name)
        try:
            exe.chmod(0o755)
        except OSError:
            pass
    model = "realesrgan-x4plus-anime" if anime else "realesrgan-x4plus"
    r = subprocess.run([str(exe), "-i", str(src), "-o", str(dst), "-n", model, "-s", str(scale)],
                       capture_output=True, text=True, errors="replace", timeout=900, cwd=str(root))
    if r.returncode or not dst.exists():
        raise Skip("Real-ESRGAN не сработал (нужна видеокарта с Vulkan): " + (r.stderr or r.stdout)[-300:])
    return dst


def _alpha_ok(path: Path) -> bool:
    """A sensible cut-out keeps 3-95 % of the pixels."""
    from PIL import Image

    a = Image.open(path).convert("RGBA").getchannel("A")
    hist = a.histogram()  # 256 bins of alpha values
    kept = sum(hist[17:]) / (a.width * a.height)
    return 0.03 < kept < 0.95


# ============================================================================ sounds and voice

def freesound(ctx: Ctx, query: str, seconds: float | None) -> tuple[str, bytes, dict]:
    k = ctx.key("freesound")
    if not k:
        raise Skip("нет ключа Freesound (бесплатно: freesound.org/apiv2/apply)")
    longest = max(1.0, float(seconds or 8)) * 1.5
    fields = "id,name,previews,license,username,duration,url"
    params = {"query": query, "token": k["secret"], "fields": fields, "page_size": 15, "sort": "score",
              "filter": f"duration:[0.1 TO {longest:.1f}]"}
    base = (k.get("base") or "https://freesound.org/apiv2").rstrip("/")
    raw, _ = _http(base + "/search/text/?" + urllib.parse.urlencode(params), timeout=60)
    results = json.loads(raw).get("results") or []
    if not results:
        raise Skip(f"на Freesound ничего не нашлось по «{query}» — попробуй английские слова")

    def rank(s):  # commercial-friendly licences first
        lic = s.get("license", "")
        return 0 if "publicdomain/zero" in lic else (1 if "/by/" in lic else (2 if "sampling" in lic else 3))

    best = sorted(results, key=rank)[0]
    url = best["previews"].get("preview-hq-ogg") or best["previews"].get("preview-hq-mp3")
    data = _http(url, timeout=120)[0]
    return url.rsplit(".", 1)[-1], data, best


EDGE_VOICES = {"ru-m": "ru-RU-DmitryNeural", "ru-f": "ru-RU-SvetlanaNeural", "en-m": "en-US-GuyNeural",
               "en-f": "en-US-JennyNeural"}


def edge_voice(ctx: Ctx, text: str, voice: str | None) -> bytes:
    try:
        import edge_tts
    except ImportError:
        raise Skip("нет edge-tts (pip install edge-tts)")
    v = EDGE_VOICES.get(voice or "", voice) or ("ru-RU-DmitryNeural" if any("а" <= c.lower() <= "я" for c in text)
                                                else "en-US-GuyNeural")

    async def go():
        buf = bytearray()
        async for chunk in edge_tts.Communicate(text, v).stream():
            if chunk.get("type") == "audio":
                buf += chunk["data"]
        return bytes(buf)

    try:
        data = asyncio.run(go())
    except Exception as e:  # network, voice name, service changes
        raise Skip(f"edge-tts: {type(e).__name__}: {e}")
    if not data:
        raise Skip("edge-tts вернул пустой звук")
    return data


def _ffmpeg_convert(src: Path, fmt: str) -> Path:
    if not fmt or src.suffix.lstrip(".") == fmt or not shutil.which("ffmpeg"):
        return src
    dst = src.with_suffix("." + fmt)
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), str(dst)], capture_output=True)
    return dst if r.returncode == 0 and dst.exists() else src


# ============================================================================ recipes

def _chain(ctx: Ctx, steps: list[tuple[str, callable]]):
    """Run sources in order; returns (source, result). Raises AssetError with every reason if all fail."""
    why = []
    for name, fn in steps:
        try:
            return name, fn(), why
        except Skip as e:
            why.append(f"{name}: {e}")
            ctx.log(f"{name}: {e}")
    raise AssetError("ни один источник не сработал:\n- " + "\n- ".join(why))


def make(ctx: Ctx, recipe: str, prompt: str = "", input: str | None = None, name: str | None = None, out: str | None = None,
         size: str | None = None, pixel: bool = False, colors: int = 16, seconds: float | None = None,
         voice: str | None = None, lyrics: str | None = None, format: str | None = None, source: str = "auto",
         scale: int = 4) -> dict:
    """Returns {"files": [...], "source": ..., "skipped": [...], "note": ...}."""
    if recipe not in RECIPES:
        raise AssetError(f"неизвестный рецепт {recipe}; есть: {', '.join(RECIPES)}")
    if recipe == "status":
        return {"status": status(ctx)}
    o = ctx.out(out)
    stem = name or _slug(prompt or Path(input or "asset").stem)
    want = lambda s: source in ("auto", s)  # noqa: E731
    fal_ok = bool(ctx.key("fal"))

    def fal(args: list[str], ext: str):
        def run():
            if not fal_ok:
                raise Skip("нет ключа fal")
            _um(["fal", *args, "--out", str(o), "--name", stem + "_fal"], ctx)
            got = sorted(o.glob(stem + "_fal*"), key=lambda p: p.stat().st_mtime)
            if not got:
                raise Skip("fal ничего не вернул")
            return got[-1]
        return ("fal", run)

    if recipe in ("sprite", "image", "texture"):
        p = prompt
        if recipe == "sprite":
            p = f"{prompt}, single game sprite, isolated on a plain flat white background, centered, no shadow, no text"
            if pixel:
                p += ", pixel art"
        if recipe == "texture":
            p = f"{prompt}, seamless tileable texture, top-down, flat even lighting, no perspective"
        gen = (size if size and recipe != "sprite" else "1024x1024")
        steps = []
        if want("freellmapi"):
            steps.append(("FreeLLMAPI", lambda: fl_image(ctx, p, gen)))
        if want("comfy"):
            steps.append(("ComfyUI", lambda: comfy_image(ctx, p, gen)))
        if want("fal"):
            steps.append(fal(["sprite" if recipe == "sprite" else ("texture" if recipe == "texture" else "image"), prompt], ".png"))
        src, res, why = _chain(ctx, steps)
        raw = _unique(o, stem + ("_raw" if recipe != "image" else ""), ".png")
        if isinstance(res, Path):
            shutil.copy(res, raw)
        else:
            from PIL import Image

            Image.open(io.BytesIO(res)).save(raw)
        files = [raw]
        note = ""
        if recipe == "sprite":
            cut = _unique(o, stem, ".png")
            try:
                _um(["sprite", "cutout", str(raw), str(cut)], ctx)
                if not _alpha_ok(cut):
                    raise Skip("заливка фона съела спрайт")
                note = "фон вырезан заливкой (um sprite cutout)"
            except Skip:
                try:
                    rmbg(ctx, raw, cut, "anime" if pixel else "general")
                    note = "фон вырезан моделью ISNet"
                except Skip as e:
                    shutil.copy(raw, cut)
                    note = f"фон НЕ вырезан ({e}); вырежи вручную или установи onnxruntime"
            files.append(cut)
            if size:
                fit = _unique(o, f"{stem}_{size}", ".png")
                if pixel:
                    _um(["sprite", "pixelate", str(cut), str(fit), "--size", size, "--colors", str(colors), "--outline"], ctx)
                else:
                    _um(["sprite", "fit", str(cut), str(fit), "--size", size, "--smooth"], ctx)
                files.append(fit)
        if recipe == "texture":
            tile = _unique(o, stem, ".png")
            _um(["sprite", "seamless", str(raw), str(tile)], ctx)
            files.append(tile)
        _record(o, recipe=recipe, source=src, prompt=prompt, files=[str(f) for f in files])
        return {"files": [str(f) for f in files], "source": src, "skipped": why, "note": note}

    if recipe in ("rmbg", "upscale"):
        if not input:
            raise AssetError("нужен input: путь к картинке")
        srcp = Path(input) if Path(input).is_absolute() else ctx.workdir / input
        dst = _unique(o, Path(input).stem + ("_nobg" if recipe == "rmbg" else f"_x{scale}"), ".png")
        if recipe == "rmbg":
            steps = [("ISNet (локально)", lambda: rmbg(ctx, srcp, dst, "anime" if pixel else "general"))]
            if want("fal"):
                steps.append(fal(["rmbg", str(srcp)], ".png"))
        else:
            steps = [("Real-ESRGAN (локально)", lambda: upscale(ctx, srcp, dst, scale, anime=True))]
            if want("fal"):
                steps.append(fal(["upscale", str(srcp)], ".png"))
        src, res, why = _chain(ctx, steps)
        _record(o, recipe=recipe, source=src, input=str(srcp), files=[str(res)])
        return {"files": [str(res)], "source": src, "skipped": why}

    if recipe == "sfx":
        meta = {}

        def fs():
            ext, data, info = freesound(ctx, prompt, seconds)
            meta.update(info)
            p = _unique(o, stem, "." + ext)
            p.write_bytes(data)
            return p

        def sa():
            fname, data = comfy_sfx(ctx, prompt, seconds or 4)
            p = _unique(o, stem, Path(fname).suffix or ".flac")
            p.write_bytes(data)
            return p

        steps = ([("Freesound", fs)] if want("freesound") else []) + ([("ComfyUI Stable Audio", sa)] if want("comfy") else [])
        if want("fal"):
            steps.append(fal(["sfx", prompt] + (["--seconds", str(seconds)] if seconds else []), ".mp3"))
        src, res, why = _chain(ctx, steps)
        res = _ffmpeg_convert(res, format or "")
        rec = {"recipe": "sfx", "source": src, "prompt": prompt, "files": [str(res)]}
        if meta:
            rec.update(freesound_id=meta.get("id"), license=meta.get("license"), author=meta.get("username"), url=meta.get("url"))
            with open(o / "CREDITS.md", "a", encoding="utf-8") as f:
                f.write(f"- {res.name}: \"{meta.get('name')}\" by {meta.get('username')} ({meta.get('url')}), "
                        f"license {meta.get('license')}\n")
        _record(o, **rec)
        note = f"лицензия: {meta.get('license')} — автор {meta.get('username')}, записано в CREDITS.md" if meta else ""
        return {"files": [str(res)], "source": src, "skipped": why, "note": note}

    if recipe == "music":
        def ace():
            fname, data = comfy_music(ctx, prompt, lyrics or "", seconds or 60)
            p = _unique(o, stem, Path(fname).suffix or ".flac")
            p.write_bytes(data)
            return p

        steps = [("ComfyUI ACE-Step", ace)] if want("comfy") else []
        if want("fal"):
            steps.append(fal(["music", prompt, "--seconds", str(seconds or 60)], ".mp3"))
        src, res, why = _chain(ctx, steps)
        res = _ffmpeg_convert(res, format or "")
        _record(o, recipe="music", source=src, prompt=prompt, files=[str(res)])
        return {"files": [str(res)], "source": src, "skipped": why}

    if recipe == "voice":
        text = prompt

        def save(data: bytes):
            p = _unique(o, stem, ".mp3")
            p.write_bytes(data)
            return p

        steps = []
        if want("edge"):
            steps.append(("edge-tts", lambda: save(edge_voice(ctx, text, voice))))
        if want("freellmapi"):
            steps.append(("FreeLLMAPI", lambda: save(fl_speech(ctx, text, voice))))
        if want("fal"):
            steps.append(fal(["voice", text], ".mp3"))
        src, res, why = _chain(ctx, steps)
        res = _ffmpeg_convert(res, format or "")
        _record(o, recipe="voice", source=src, text=text, voice=voice, files=[str(res)])
        return {"files": [str(res)], "source": src, "skipped": why}

    if recipe == "model3d":
        if not input:
            raise AssetError("нужен input: картинка объекта (лучше на прозрачном или однотонном фоне)")
        srcp = Path(input) if Path(input).is_absolute() else ctx.workdir / input

        def hy():
            fname, data = comfy_model3d(ctx, srcp)
            p = _unique(o, stem, Path(fname).suffix or ".glb")
            p.write_bytes(data)
            return p

        steps = [("ComfyUI Hunyuan3D", hy)] if want("comfy") else []
        if want("fal"):
            steps.append(fal(["model3d", str(srcp)], ".glb"))
        src, res, why = _chain(ctx, steps)
        _record(o, recipe="model3d", source=src, input=str(srcp), files=[str(res)])
        note = "модель без текстуры: для спрайтов раскрась её в Blender или используй как основу" if src.startswith("ComfyUI") else ""
        return {"files": [str(res)], "source": src, "skipped": why, "note": note}

    if recipe == "video":
        def flv():
            p = _unique(o, stem, ".mp4")
            p.write_bytes(fl_video(ctx, prompt, int(seconds or 5)))
            return p

        steps = [("FreeLLMAPI", flv)] if want("freellmapi") else []
        if want("fal") and input:
            steps.append(fal(["video", input, prompt, "--seconds", str(int(seconds or 5))], ".mp4"))
        src, res, why = _chain(ctx, steps)
        _record(o, recipe="video", source=src, prompt=prompt, files=[str(res)])
        return {"files": [str(res)], "source": src, "skipped": why}
    raise AssetError(recipe)


def status(ctx: Ctx) -> dict:
    """What can make assets right now: shown on the Keys page and given to the agent."""
    up = comfy_up()
    ck = comfy_models("checkpoints") if up else []
    te = (comfy_models("text_encoders") + comfy_models("clip")) if up else []

    def have(mod):
        try:
            __import__(mod)
            return True
        except ImportError:
            return False

    root = data_dir()
    return {
        "freellmapi": bool(ctx.key("freellmapi")),
        "freesound": bool(ctx.key("freesound")),
        "fal": bool(ctx.key("fal")),
        "edge_tts": have("edge_tts"),
        "onnxruntime": have("onnxruntime"),
        "rmbg_model": (root / "models" / "isnet-general.onnx").exists(),
        "realesrgan": any((root / "tools" / "realesrgan").glob("realesrgan-ncnn-vulkan*")),
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "comfy": {"url": comfy_url(), "up": up,
                  "image_model": _pick(ck, ("xl", "flux", "dream", "juggernaut", "pony", "illustrious", "sd", ""), AUDIO_3D),
                  "stable_audio": bool(_pick(ck, ("stable_audio",)) and _pick(te, ("t5",))),
                  "ace_step": bool(_pick(ck, ("ace_step",))),
                  "hunyuan3d": bool(_pick(ck, ("hunyuan3d",)))},
    }
