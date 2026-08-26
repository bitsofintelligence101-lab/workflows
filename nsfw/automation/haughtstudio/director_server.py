"""
director_server.py - lite local Flask server for directorUI.html.

Serves the UI, executes MovieDirector runs in a background thread, streams
captured stdout + manifest status back to the browser, and serves generated
media (clips / final frames / input JSON) over HTTP so the page can link to
them (browsers block file:/// URLs from http-served pages).

Usage:
    pip install flask
    python director_server.py
    # then open http://127.0.0.1:5000
"""

import io
import json
import os
import sys
import threading
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(CURRENT_DIR)

from director import MovieDirector
from call_comfyui import ComfyUIlocal

HOST, PORT = "127.0.0.1", 5000
MAX_REF_IMAGES = 6  # hard limit imposed by the MiniMax H3 R2V node
LAST_CONFIG_PATH = os.path.join(CURRENT_DIR, "director_ui_last_config.json")
MEDIA_EXT = {".mp4", ".webm", ".mov", ".png", ".jpg", ".jpeg", ".webp",
             ".gif", ".json", ".txt", ".md"}

app = Flask(__name__)

LOG_LINES = deque(maxlen=2000)
RUN_LOCK = threading.Lock()
STOP_FLAG = threading.Event()
STATE = {"state": "idle", "error": None, "output_dir": None, "started": None,
         "phase": None, "phase_detail": ""}

# Single shared ComfyUI client for the life of the server. The quant passed as
# service_type is the only thing that controls model unloading: generate()
# compares it against COMFY.last_workflow and only runs aggressive_cleanup()
# when it changed (int8 <-> bf16). Same quant -> models stay loaded in VRAM.
COMFY = ComfyUIlocal(last_workflow=None)


# ---------------------------------------------------------------------------
# stdout capture
# ---------------------------------------------------------------------------
class LogStream(io.TextIOBase):
    """File-like sink that appends timestamped lines to LOG_LINES."""

    def __init__(self):
        self._buf = ""

    def write(self, s):
        if not s:
            return 0
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                LOG_LINES.append(f"{datetime.now():%H:%M:%S}  {line}")
        return len(s)

    def flush(self):
        if self._buf.strip():
            LOG_LINES.append(f"{datetime.now():%H:%M:%S}  {self._buf.strip()}")
            self._buf = ""


class Tee(io.TextIOBase):
    """Write to several streams at once (real stdout + log capture)."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, s):
        for st in self.streams:
            try:
                st.write(s)
            except Exception:
                pass
        return len(s)

    def flush(self):
        for st in self.streams:
            try:
                st.flush()
            except Exception:
                pass


# Thread-aware phase tracking. The diffusion worker runs on the run-worker
# thread; LLM calls (drafts / descriptions / adjusts) run on the director's
# internal pool threads. A single STATE pair would race, so each side writes
# its own key and the UI shows both.
PHASE_LOCK = threading.Lock()
STATE["ai_phase"] = None        # what the LLM pool is doing
STATE["ai_detail"] = ""


@contextmanager
def _phase(name, detail=""):
    """Mark the diffusion worker's current phase for the UI status panel."""
    prev = (STATE.get("phase"), STATE.get("phase_detail"))
    STATE["phase"] = name
    STATE["phase_detail"] = detail
    try:
        yield
    finally:
        STATE["phase"], STATE["phase_detail"] = prev


@contextmanager
def _ai_phase(detail):
    """Mark an LLM pool task for the UI status panel (thread-safe)."""
    with PHASE_LOCK:
        STATE["ai_phase"] = "ai"
        STATE["ai_detail"] = detail
    try:
        yield
    finally:
        with PHASE_LOCK:
            STATE["ai_phase"] = None
            STATE["ai_detail"] = ""


# ---------------------------------------------------------------------------
# Run execution
# ---------------------------------------------------------------------------
class ServerDirector(MovieDirector):
    """MovieDirector with cooperative stop checks and UI phase reporting.

    With parallel_llm_diff=True the heavy FL2VA drafts run on the internal
    LLM pool while this worker thread drives diffusion; the phase wrappers
    below report both sides to the UI (thread-safe via _ai_phase).
    """

    def run(self):
        print(f"\nStarting movie run: {len(self.scenes)} scene(s) -> {self.output_dir}")
        if self.parallel_llm_diff:
            # Parallel path: stop checks happen per-clip inside generate_clip.
            manifest = self._run_parallel()
            print(f"\nMovie run complete. Manifest: {self.manifest_path}")
            return manifest
        carry = None  # serial path: stop checks between scenes
        for scene_id, scene_cfg in self.scenes.items():
            if STOP_FLAG.is_set():
                print("Stop requested - halting before the next scene.")
                break
            inherited = carry if self.chain_scenes else None
            _, carry = self.run_scene(scene_id, scene_cfg, inherited_video=inherited)
        if not STOP_FLAG.is_set():
            self.assemble_clip_videos()
        print(f"\nMovie run complete. Manifest: {self.manifest_path}")
        return self.manifest

    # --- LLM pool tasks (run on director-llm threads) ----------------------
    def write_scene_prompt(self, *args, **kwargs):
        with _ai_phase("AI drafting FL2VA prompts from the reference images"):
            return super().write_scene_prompt(*args, **kwargs)

    def write_continuation_prompt(self, *args, **kwargs):
        with _ai_phase("AI writing the FL2VA continuation prompt"):
            return super().write_continuation_prompt(*args, **kwargs)

    def reconcile_prompt_with_video(self, *args, **kwargs):
        with _ai_phase("AI reconciling the draft against the prior clip's ending"):
            return super().reconcile_prompt_with_video(*args, **kwargs)

    def describe_video_end(self, *args, **kwargs):
        with _ai_phase("Vision AI reviewing the clip's final frame"):
            return super().describe_video_end(*args, **kwargs)

    # --- diffusion worker (run-worker thread) ------------------------------
    def generate_clip(self, *args, **kwargs):
        if STOP_FLAG.is_set():
            print("Stop requested - skipping clip generation.")
            return None, None
        with _phase("diffusion", "Diffusion model generating the clip on ComfyUI"):
            return super().generate_clip(*args, **kwargs)

    def _stop_requested(self):
        # lambda assigned in __init__ is shadowed by this method lookup
        return STOP_FLAG.is_set()


def build_scenes(cfg):
    """Convert the UI config payload into director.py's SCENES dict."""
    scenes = {}
    for s in cfg.get("scenes", []):
        entry = {"summary": s.get("summary", "")}
        for i, im in enumerate(s.get("images", [])[:MAX_REF_IMAGES], start=1):
            entry[f"img_{i}"] = {
                "path": im["path"],
                "description": im.get("description") or os.path.basename(im["path"]),
            }
        video_from = s.get("videoFrom")
        if video_from == "__file__":
            entry["video"] = {"path": s.get("videoFilePath"),
                              "description": s.get("videoDesc") or None}
        elif video_from and video_from != "chain":
            # the UI resolves scene references to scene ids before posting
            entry["video"] = {"path": video_from,
                              "description": s.get("videoDesc") or None}
        if s.get("clips"):
            entry["clips"] = int(s["clips"])
        if s.get("duration"):
            entry["duration"] = float(s["duration"])
        scenes[s["id"]] = entry
    return scenes


def run_worker(cfg):
    old_stdout = sys.stdout
    capture = LogStream()
    sys.stdout = Tee(old_stdout, capture)
    try:
        STATE.update(state="running", error=None, started=time.time(),
                     phase=None, phase_detail="")
        # VRAM management: the quant doubles as the director's service_type.
        # The shared COMFY client remembers the last quant used
        # (COMFY.last_workflow) and generate() only unloads models when it
        # changes (int8 <-> bf16); an unchanged quant keeps models loaded.
        director = ServerDirector(
            scenes=build_scenes(cfg),
            output_dir=cfg.get("output_dir") or "movie_output",
            clips_per_scene=int(cfg.get("clips_per_scene") or 1),
            clip_duration=float(cfg.get("clip_duration") or 10.0),
            megapixels=float(cfg.get("megapixels") or 0.6),
            aspect_ratio=cfg.get("aspect_ratio") or "16:9 (Widescreen)",
            chain_scenes=bool(cfg.get("chain_scenes")),
            turbo_lora=bool(cfg.get("turbo_lora", True)),
            ref_quality=cfg.get("ref_quality") or "match",
            quant=cfg.get("quant") or "int8",
            ai_timeout=int(cfg.get("ai_timeout") or 900),
            comfy_client=COMFY,
            service_type=cfg.get("quant") or "int8",
            parallel_llm_diff=bool(cfg.get("parallel_llm_diff", True)),
            reconcile_with_video=bool(cfg.get("reconcile_with_video", False))
        )
        STATE["output_dir"] = os.path.abspath(director.output_dir)
        director.run()
        STATE["state"] = "stopped" if STOP_FLAG.is_set() else "done"
        print(f"Run finished with state: {STATE['state']}")
    except Exception as e:
        print(f"RUN ERROR: {e}")
        STATE.update(state="error", error=str(e))
    finally:
        STATE["phase"] = None
        STATE["phase_detail"] = ""
        capture.flush()
        sys.stdout = old_stdout
        RUN_LOCK.release()


def validate_config(cfg):
    scenes = cfg.get("scenes") or []
    if not scenes:
        return "no scenes configured"
    for s in scenes:
        sid = s.get("id") or "?"
        if not (s.get("summary") or "").strip():
            return f"{sid}: summary is empty"
        imgs = [im for im in s.get("images", []) if (im.get("path") or "").strip()]
        if not imgs:
            return f"{sid}: needs at least one image"
        s["images"] = imgs[:MAX_REF_IMAGES]
        for im in s["images"]:
            if not os.path.isfile(im["path"]):
                return f"{sid}: image not found: {im['path']}"
        if s.get("videoFrom") == "__file__" and not os.path.isfile(s.get("videoFilePath") or ""):
            return f"{sid}: video file not found: {s.get('videoFilePath')}"
    return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return send_from_directory(CURRENT_DIR, "directorUI.html")


@app.get("/api/config")
def api_config():
    """Return the last run's config so the UI can preload it."""
    if os.path.isfile(LAST_CONFIG_PATH):
        with open(LAST_CONFIG_PATH, "r", encoding="utf-8") as f:
            return jsonify(json.load(f))
    return jsonify({})


@app.post("/api/run")
def api_run():
    if RUN_LOCK.locked():
        return jsonify({"error": "a run is already in progress"}), 409
    cfg = request.get_json(force=True)
    error = validate_config(cfg)
    if error:
        return jsonify({"error": error}), 400
    STOP_FLAG.clear()
    LOG_LINES.clear()   # fresh server log for each run
    STATE.update(state="starting", error=None, output_dir=cfg.get("output_dir"),
                 phase=None, phase_detail="")
    with open(LAST_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    RUN_LOCK.acquire(blocking=False)
    threading.Thread(target=run_worker, args=(cfg,), daemon=True).start()
    return jsonify({"ok": True})


@app.post("/api/stop")
def api_stop():
    """Cooperative stop: the run halts after the current clip/scene."""
    STOP_FLAG.set()
    return jsonify({"ok": True})


@app.get("/api/status")
def api_status():
    manifest = None
    out_dir = STATE.get("output_dir")
    if out_dir:
        mpath = os.path.join(out_dir, "movie_manifest.json")
        if os.path.isfile(mpath):
            try:
                with open(mpath, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
            except Exception:
                manifest = None
    return jsonify({
        "state": STATE["state"],
        "error": STATE["error"],
        "output_dir": out_dir,
        "log": list(LOG_LINES)[-400:],
        "manifest": manifest,
        "phase": STATE["phase"],
        "phase_detail": STATE["phase_detail"],
        "ai_phase": STATE.get("ai_phase"),
        "ai_detail": STATE.get("ai_detail"),
    })


@app.get("/media")
def media():
    """Serve a generated file (clip / frame / input JSON) by absolute path."""
    p = request.args.get("path", "")
    if not p:
        abort(404)
    p = os.path.abspath(p)
    if not os.path.isfile(p) or os.path.splitext(p)[1].lower() not in MEDIA_EXT:
        abort(404)
    return send_file(p)


if __name__ == "__main__":
    print(f"Director UI server - open http://{HOST}:{PORT}")
    app.run(host=HOST, port=PORT, threaded=True)
