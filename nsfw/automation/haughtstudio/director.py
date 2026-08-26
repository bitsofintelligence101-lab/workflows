"""
director.py - 'Movie Making' workflow (v1).

Pipeline per scene:
  1. Send the scene's reference images (character sheet, face close-up, scene
     image(s)) plus the scene summary to the local LLM (tools/ai_call.py),
     which is driven by the FL2VA prompt-writer system prompt in
     director_system_prompt.md, and get back a structured MiniMax H3 FL2VA
     prompt (alignment line / summary / subject_definitions /
     integrated_multimodal_description / soundscape / music).
  2. Build a MiniMax H3 reference-to-video request (tools/iv2v_h3.py) from that
     prompt + the reference images and execute it on ComfyUI.
  3. Save the resulting clip and describe its ending state with a separate
     vision AI call (the FL2VA contract is text-only for videos); the
     description is recorded in the manifest for any scene that references
     this one.
  4. Repeat for the configured number of clips — every clip of a scene opens
     from the scene's own starting point (clips never chain into each other
     within a scene), and a scene whose "video" entry names another scene
     opens clip N from that scene's clip N — then move to the next scene.

Every scene is a self-contained dict so future versions can supply entirely
new image sets per scene without changing the control flow:

    SCENES = {
        "scene_01": {
            "summary": "text of what happens in the scene",
            "img_1": {"path": "path/to/charsheet.png",
                      "description": "subject sheet, three views of the woman"},
            "img_2": {"path": "path/to/closeup.png",
                      "description": "close-up of the same woman's face"},
            "img_3": {"path": "path/to/kitchen.png",
                      "description": "empty kitchen plate, morning light"},
            # optional: a scene that opens mid-action as a continuation.
            # "path" may be a direct file path OR the id of another scene.
            # A scene id resolves PER CLIP: clip N of this scene continues
            # from clip N of the referenced scene (as recorded in
            # generated_videos), so each clip index is its own parallel
            # take-line through the movie. A direct file path opens every
            # clip of the scene from that same file. Referenced scenes must
            # run BEFORE this scene in the same run; an unresolvable "path"
            # raises an error rather than guessing.
            "video": {"path": "scene_01",   # or "path/to/prev_clip.mp4"
                      # None -> reuse the referenced clip's recorded ending
                      # description (AI-generated for direct file paths)
                      "description": None},
            "clips": 2,          # DEPRECATED — accepted but ignored; every
                                 # scene generates clips_per_scene clips so
                                 # clip indices stay aligned across scenes
            "duration": 10.0,     # optional per-scene seconds per clip
        },
        ...
    }

img_N keys are attached to the LLM in numeric order (img 1 = first attached
image) exactly as the FL2VA prompt writer expects; each image's "description"
becomes its note in the <inputs> manifest. A scene's "video" entry makes its
clips Mode A continuations of that video — clip N of the scene continues
from clip N of the referenced scene (parallel take-lines), or from the same
file when a direct path is given. Each continuation needs a text description
of the prior clip's ending state: an explicit "description" wins, otherwise
the description recorded in the manifest when the referenced clip was
generated is reused, otherwise one is generated at runtime by a separate AI
call that watches the video (see MovieDirector.describe_video_end). iv2v_h3
supports a maximum of 6 reference images per clip; extra img_N entries are
truncated with a warning.

Two run modes resolve a continuation's prior-ending description differently
(see MovieDirector.run): the serial pipeline (parallel_llm_diff=False) always
resolves it against a real clip as above. The default parallel pipeline
(parallel_llm_diff=True) drafts a continuation scene's prompt one scene
ahead of the diffusion worker, directly in the finished v2v format, against
an ASSUMED prior ending — predicted from the referenced scene's own brief if
that clip doesn't exist yet, or described for real immediately for a direct
file reference. That assumed ending is used as the finished prompt as-is by
default; set reconcile_with_video=True to re-enable a vision pass
(MovieDirector.reconcile_prompt_with_video) that corrects the draft against
the real clip once it exists, before diffusion.

Scene chaining: by default (chain_scenes=False) a scene without its own
"video" entry opens cold — its first clip is a fresh images+summary
generation. With chain_scenes=True such a scene instead continues from the
previous scene's last generated clip (Mode A), reusing the ending-state
description already produced for that clip. An explicit "video" entry
always takes precedence over chaining.

A random seed is drawn per clip and recorded in the manifest for
reproducibility.

Output layout (under output_dir):
    <scene_id>/<scene_id>_clip01.mp4 ...            generated clips
    <scene_id>/<scene_id>_clip01_final_frame.png    each clip's final frame
                                                    (SaveImage node 209), used
                                                    for the ending-state vision
                                                    call instead of re-sampling
                                                    the video
    <scene_id>/<scene_id>_clip01_input.json         exact inputs per clip: the
                                                    brief and system prompt sent
                                                    to the prompt-writer AI, the
                                                    assets, and the returned
                                                    FL2VA prompt
    movie_manifest.json                             every clip with its prompt,
                                                    ref images, seed,
                                                    continuation source, final
                                                    frame, and ending
                                                    description; rewritten after
                                                    every clip so a crashed run
                                                    stays inspectable
    combined_clipNN.mp4                             one per clip index: at run
                                                    end, every scene's clip N is
                                                    concatenated in scene order
                                                    into the base output dir
"""

import json
import os
import random
import re
import subprocess
import sys
import tempfile
import threading
from datetime import datetime

# Make sibling packages importable when run as a script
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(CURRENT_DIR)

from tools.ai_call import AICall
from tools.iv2v_h3 import iv2v
from tools.v2v_h3 import v2v
from tools.ffmpeg_tools import combine_videos
from call_comfyui import ComfyUIlocal, WORKFLOWS

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
WORKFLOWS["iv2v_h3"] = os.path.join(CURRENT_DIR, "comfy_workflows", "minimax_h3_flex_cinematic.json")
WORKFLOWS["v2v_h3"] = os.path.join(CURRENT_DIR, "comfy_workflows", "minimax_h3_v2v_cinematic.json")


MAX_REF_IMAGES = 6  # hard limit imposed by the MiniMax H3 R2V node

# FL2VA prompt-writer system prompt (see that file for the full input/output
# contract: numbered images, <inputs> manifest, Mode A/B/C alignment, etc.)
SYSTEM_PROMPT_i2v_PATH = os.path.join(CURRENT_DIR, "director_system_prompt.md")
SYSTEM_PROMPT_v2v_PATH = os.path.join(CURRENT_DIR, "director_system_prompt_v2v.md")

def load_system_prompt_i2v(path=SYSTEM_PROMPT_i2v_PATH):
    """Load the FL2VA prompt-writer system prompt from disk."""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def load_system_prompt_v2v(path=SYSTEM_PROMPT_v2v_PATH):
    """Load the FL2VA prompt-writer system prompt for v2v from disk."""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# The parallel pipeline uses a single background LLM worker that buffers the
# NEXT scene's draft prompt while the diffusion worker generates the current
# clip (see MovieDirector._run_parallel). One scene of lookahead, one worker,
# no queue: the worker thread is created in _run_parallel and consumes
# buffered slots handed to it by the diffusion thread.

# System prompt for the draft's predicted prior-ending. When a continuation
# scene's prompt is drafted ahead of time, the prior clip does not exist yet
# — so the draft writes the ending it EXPECTS the prior scene to have, from
# the prior scene's brief. A later reconcile pass (RECONCILE_SYSTEM) checks
# that prediction against the actual video.
PREDICT_ENDING_SYSTEM = """You are predicting how a video clip will END, so the next scene's prompt can be drafted before the clip exists.

You are given the brief (summary) the clip is being generated from. Write 2-4 sentences describing the clip's most likely ending state, in the same concrete spatial style a video analyst would use: the final pose and frame position of each person, any motion still in progress as the clip ends, the final camera framing and movement, and the lighting.

Rules:
- Predict only what the brief strongly implies for the FINAL moment — if the brief states an ending beat, anchor on it; otherwise end on the last described action settling.
- Concrete and spatial: frame positions, held objects, settled or in-progress motion.
- No hedging ("probably", "might"), no preamble, no headings — flowing sentences stating the ending as fact.
- Do not invent new characters, actions, or set elements beyond the brief."""


# System prompt for the reconcile pass. A continuation scene's draft prompt
# was written ahead of time from a PREDICTED ending of the prior clip
# (PREDICT_ENDING_SYSTEM). Once the real prior clip exists, this pass is
# shown the actual video together with the draft and fixes only the places
# where the prediction was wrong, converting the draft into the exact
# continuation format (Mode A) in the same step.
RECONCILE_SYSTEM = """You finalize a DRAFT MiniMax H3 FL2VA continuation prompt. The draft was written from reference images, the scene brief, and a PREDICTED ending of the prior video. You are now shown the actual prior video. Compare its ending — especially the final frame — against the prediction embedded in the draft, and rewrite the draft into the EXACT continuation format below so the new clip opens from the prior video's real last frame.

Your entire reply is the prompt and nothing else — no preamble, no commentary, no code fences.

## OUTPUT SHAPE (always these blocks, this order, one blank line between blocks)

```
How the reference pictures align with the target video — there is no starting frame; the 0.00-second mark is the last frame of <Video 1>. Continue the exact motion already in progress, [short restatement of the prior video's ACTUAL end state]. Target duration S.SS seconds.

[Shot 1] Continue the exact motion in progress, then develop the next action naturally.

summary:
[reference generation] one paragraph

subject_definitions:
<Subject 1> is ...
<Subject 2> is ...

integrated_multimodal_description: [Shot 1] ...

overall_soundscape: ...

non_diegetic_music: ...
```

The alignment line and the `[Shot 1] Continue the exact motion...` directive line are fixed boilerplate — reproduce them exactly, filling in only the end-state clause and the duration. The alignment line says `reference pictures` even though a video is present; keep it as written. The summary tag is always `[reference generation]`. `S.SS` always has two decimals and matches the target duration.

## WHAT TO CHANGE (and what to keep)

1. Compare the video's ending against the prediction the draft assumed. Where they match, keep the draft's wording — no gratuitous rewrites. Where they differ, the VIDEO wins.
2. Alignment line: rewrite to the continuation boilerplate above, restating the prior video's ACTUAL end state in the blank.
3. Keep or add the `[Shot 1] Continue the exact motion...` directive line between the alignment line and the summary block.
4. summary: keep the draft's action; ensure it states the scene continues directly from the prior clip's end state.
5. subject_definitions: KEEP the draft's identity details verbatim. Change each person's closing split clause to `starting [the prior video's actual end state]`. Change the room definition's opening to `<Subject N> is the room from <Video 1> and <Picture M>: ...` (from `<Video 1>` alone if the draft has no room plate). <Video 1> never gets its own definition line.
6. integrated_multimodal_description opening: state the prior video's actual end state as a live position. If the clip ended mid-motion, carry it through (`that reach continues without pausing or resetting`); if it ended at rest, call the stillness held, then start the scene's first action. Do not pay the 0.5s motion-onset cost when motion carries through. NEVER "resumes from the final frame" or "with no cut". Keep all later beats, dialogue `<d>` blocks, camera intentions, and the final consistency sentence from the draft.
7. overall_soundscape: ensure it opens with `The low room tone from the preceding shot continues unbroken beneath...`; keep the draft's remaining sounds.
8. non_diegetic_music: keep as-is.

Do not invent new actions, dialogue, subjects, or beats beyond correcting the assumed end state. Identity, wardrobe, lighting, and room details from the draft stay unchanged."""


# System prompt for the separate vision call that describes a clip's ending
# state (used as the text description of <Video 1> in continuation briefs).
VIDEO_DESCRIPTION_SYSTEM = """You are a video analyst for a film director.

You receive the FINAL 2 seconds of a clip as frames at 4fps. The last image is the true final frame of the clip. Describe how the clip ends, anchored on that last frame. Use the earlier frames ONLY to identify motion that is still in progress (mid-turn, mid-step, hand lowering, settling to stillness).

Write 3-6 sentences, in this order:
1. PEOPLE — each person's position in the room and in the frame (left/right/center, near/far), pose, and facing direction on the last frame.
2. MOTION — what movement is in progress or just completing as the clip ends. If a subject has come to rest, say so and say what they settled from.
3. CAMERA — framing size (close-up/medium/wide), height and angle, and any camera movement in progress (static, handheld sway, pan, push-in, drift) with its direction.
4. LIGHTING — quality (hard/soft), direction, and color of the visible light sources.
5. SET/PROPS — the key visible set elements and prop states (held objects, open/closed doors, items on surfaces).

Rules:
- Concrete and spatial only. Name screen positions (frame left/right/center, foreground/background).
- Describe expressions only as visible facial states, not inner feelings.
- No preamble, no headings, no bullet points — flowing sentences.
- Do not guess what happens after the last frame.
- Do not describe anything not visible in the frames.

EXAMPLE_USER_TEXT = "Describe what happens in the video: position of the characters, action in progress at the final frames, camera framing and movement, and lighting."

EXAMPLE_ASSISTANT = A single woman fills the left-center of the frame from the chest up, near the camera, her torso angled slightly toward frame right and her head turned to face the lens directly; she holds a black-lidded glass coffee carafe in both hands at chest height near the bottom-center of frame. Over the final second she has just withdrawn her right hand from a reach toward the lens, and her open laugh is subsiding into a broad closed-mouth smile as her shoulders drop — she is nearly still on the last frame. The camera is a handheld medium close-up at eye level, with a subtle rightward drift that brings a little more of the upper cabinets into frame right by the final frame. Lighting is a mix of warm amber under-cabinet strip light glowing across the marble backsplash behind her and softer, cooler daylight from off-frame left that models her face and catches the stainless range hood in the upper-left corner. The set shows closed light-oak cabinet doors with brass pulls across the top of frame, a brass faucet behind her right shoulder, and a black espresso machine on the counter at frame right.
"""


# ---------------------------------------------------------------------------
# ffmpeg helpers
# ---------------------------------------------------------------------------
def _probe_duration(video_path):
    """Return the video duration in seconds via ffprobe, or None on failure."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", video_path],
            capture_output=True, text=True, check=True)
        return float(result.stdout.strip())
    except Exception as e:
        print(f"Warning: ffprobe could not read duration of {video_path} ({e})")
        return None


def _extract_last_frame(video_path):
    """Extract the exact final frame of a clip as a PNG via ffmpeg.

    Fallback for when the workflow did not produce a final-frame image
    (e.g. the SaveImage node is missing). Returns the temp-file path, or
    None on failure (caller falls back to the video tail).
    """
    fd, out_path = tempfile.mkstemp(prefix="director_lastframe_", suffix=".png")
    os.close(fd)

    cmd = [
        "ffmpeg", "-y",
        "-sseof", "-0.1",          # seek to just before the end (input seek)
        "-i", video_path,
        "-frames:v", "1",
        "-q:v", "2",
        out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
        print(f"Warning: ffmpeg last-frame extraction failed for {video_path}: {result.stderr[-500:]}")
        try:
            os.remove(out_path)
        except OSError:
            pass
        return None
    return out_path


def _trim_video_tail(video_path, tail_seconds=2.0):
    """Trim a clip to just its final `tail_seconds` (re-encoded, audio kept).

    The vision call only needs the ending state, so sending the whole clip
    wastes bandwidth and context. -ss is placed before -i for fast input
    seeking; since we re-encode, the cut is still frame-accurate.

    Returns the trimmed temp-file path, or None if the trim failed (caller
    should fall back to the full video).
    """
    duration = _probe_duration(video_path)
    if duration is None:
        return None

    start_time = max(0.0, duration - tail_seconds)
    clip_duration = duration - start_time
    if clip_duration <= 0:
        return None

    # No point re-encoding if the clip is already ~the tail length.
    if start_time <= 0.0:
        return video_path

    fd, out_path = tempfile.mkstemp(
        prefix="director_tail_", suffix=os.path.splitext(video_path)[1] or ".mp4")
    os.close(fd)

    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start_time:.6f}",
        "-i", video_path,
        "-t", f"{clip_duration:.6f}",
        "-map", "0:v:0",
        "-map", "0:a?",          # keep audio if the clip has any
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-ar", "44100",
        "-ac", "2",
        "-movflags", "+faststart",
        out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not os.path.isfile(out_path):
        print(f"Warning: ffmpeg tail trim failed for {video_path}: {result.stderr[-500:]}")
        try:
            os.remove(out_path)
        except OSError:
            pass
        return None
    return out_path


# ---------------------------------------------------------------------------
# Director
# ---------------------------------------------------------------------------
class MovieDirector:
    """Orchestrates LLM prompt-writing + iv2v clip generation across scenes."""

    def __init__(self,
                 scenes,
                 output_dir="movie_output",
                 clips_per_scene=2,
                 clip_duration=10.0,
                 megapixels=0.6,
                 aspect_ratio="16:9 (Widescreen)",
                 chain_scenes=False,
                 ai_client=None,
                 comfy_client=None,
                 turbo_lora=True,
                 ref_quality="match",
                 quant="int8",
                 ai_timeout=900,
                 service_type="h3",
                 parallel_llm_diff=True,
                 reconcile_with_video=False):
        """
        Args:
            scenes: dict of scene configs (see module docstring). Insertion
                    order defines shooting order.
            output_dir: root directory for clips + manifest.
            clips_per_scene: number of clips generated per scene. Every scene
                             generates exactly this many clips so that clip N
                             of one scene continues from clip N of the scene
                             it references. A per-scene "clips" value is
                             accepted for backwards compatibility but ignored.
            clip_duration: default seconds per clip (scene may override).
            megapixels / aspect_ratio: passed through to the iv2v workflow.
            chain_scenes: when True, a scene without its own "video" entry
                          continues from the previous scene's last clip
                          (Mode A). Default False — scenes open cold unless
                          they declare a "video" entry.
            service_type: "h3" if this changes between comfy runs, triggers vram clean, keep static when same models being used.
            parallel_llm_diff: when True (default), one background LLM
                thread buffers the next scene's draft prompt while the
                diffusion worker generates the current clip; a continuation
                scene's draft is written directly in the finished v2v
                continuation format against an assumed prior-clip ending
                (predicted from the referenced scene's brief, or described
                immediately for a direct file reference). When False, the
                original serial per-scene loop runs instead.
            reconcile_with_video: when False (default), the assumed ending
                embedded in a continuation draft is used as the finished
                prompt as-is — no vision call against the actual prior clip.
                Set True to re-enable the vision reconcile pass
                (reconcile_prompt_with_video), which compares the draft
                against the real video and corrects it before diffusion.
            ai_client / comfy_client: optional pre-built clients (for testing).
        """
        self.scenes = scenes
        self.output_dir = output_dir
        self.clips_per_scene = clips_per_scene
        self.clip_duration = clip_duration
        self.megapixels = megapixels
        self.aspect_ratio = aspect_ratio
        self.chain_scenes = chain_scenes
        self.turbo_lora = turbo_lora
        # "match": downscale reference images to match the output resolution;
        # "max": reference the input images at their full resolution.
        self.ref_quality = ref_quality if ref_quality in ("match", "max") else "match"
        self.quant = quant

        #used for vram flag to clear mem if new model being used
        self.service_type = service_type

        # Reasoning models need a big budget: their hidden reasoning tokens
        # count against max_tokens, and FL2VA prompts themselves are long.
        # Timeout must also cover slow prompt processing (large base64 images
        # on a 12B model), not just generation.
        self.ai = ai_client or AICall(max_tokens=50000, timeout=ai_timeout)
        self.comfy = comfy_client or ComfyUIlocal()

        # FL2VA system prompts, loaded once up front. Per-call selection is
        # explicit (never mutated on self), so the parallel LLM pool can run
        # prompt writes concurrently without a shared-state race.
        self.system_prompt_i2v = load_system_prompt_i2v()
        self.system_prompt_v2v = load_system_prompt_v2v()

        # Parallel-pipeline state (only used when parallel_llm_diff=True).
        # One background LLM worker buffers the next scene's draft prompt
        # while diffusion runs the current clip; the reconcile pass runs on
        # the diffusion thread once the real prior clip exists.
        self.parallel_llm_diff = parallel_llm_diff
        self.reconcile_with_video = reconcile_with_video
        self._llm_worker = None

        os.makedirs(self.output_dir, exist_ok=True)

        self._stop_flagged = False

        # Registry of every clip generated this run, keyed by scene id and
        # then a simple per-scene counter key:
        #     {scene_id: {"clip_1": path, "clip_2": path, ...}}
        # A scene's "video" entry may name a previous scene instead of a file
        # path (clip paths are created dynamically at runtime); clip N of the
        # dependent scene resolves to clip N of the referenced scene.
        self.generated_videos = {}

        # Manifest records everything we generate (useful for resume/debug)
        self.manifest_path = os.path.join(self.output_dir, "movie_manifest.json")
        self.manifest = {"scenes": {}, "created": datetime.now().isoformat()}

    # ------------------------------------------------------------------
    # Cooperative stop hook (base: never; server subclass overrides)
    # ------------------------------------------------------------------
    def _stop_requested(self):
        return False

    # ------------------------------------------------------------------
    # Scene asset extraction + video description
    # ------------------------------------------------------------------
    @staticmethod
    def _scene_images(scene_cfg):
        """Extract ordered image entries from a scene config.

        Returns a list of {"key", "path", "description"} dicts sorted by the
        numeric suffix of the img_N keys (img_1, img_2, ...), truncated to
        MAX_REF_IMAGES.
        """
        entries = []
        for key, value in scene_cfg.items():
            match = re.fullmatch(r"img_(\d+)", key)
            if match and isinstance(value, dict) and value.get("path"):
                entries.append({
                    "num": int(match.group(1)),
                    "path": value["path"],
                    "description": value.get("description") or os.path.basename(value["path"]),
                })
        entries.sort(key=lambda e: e["num"])

        if len(entries) > MAX_REF_IMAGES:
            print(f"Warning: scene supplies {len(entries)} images, "
                  f"truncating to {MAX_REF_IMAGES}.")
            entries = entries[:MAX_REF_IMAGES]
        if not entries:
            raise ValueError("Scene config must contain at least one img_N entry.")
        return entries

    def describe_video_end(self, video_path, fallback_text=None, final_frame_path=None):
        """
        Ask the vision LLM to describe a clip's ending state.

        When `final_frame_path` is given (the SaveImage output saved next to
        the clip), the true final frame is sent directly as an image — no
        ffmpeg trim and no video upload needed. Without it, the frame is
        extracted from the clip with ffmpeg, and if that fails the final few
        seconds are sent as a trimmed video (AICall samples frames at 4 fps,
        last frame always included). On failure falls back to
        `fallback_text` so a long run isn't killed after a clip was already
        generated.
        """
        temp_files = []
        chat_kwargs = {}
        if final_frame_path and os.path.isfile(final_frame_path):
            chat_kwargs["images"] = [final_frame_path]
        else:
            extracted = _extract_last_frame(video_path)
            if extracted:
                temp_files.append(extracted)
                chat_kwargs["images"] = [extracted]
            else:
                trimmed = _trim_video_tail(video_path, tail_seconds=2.0)
                if trimmed and trimmed != video_path:
                    temp_files.append(trimmed)
                chat_kwargs["video"] = trimmed or video_path
        try:
            desc = self.ai.chat(
                prompt=("Describe what happens in the video: position "
                        "of the characters, action in progress at the final frames, "
                        "camera framing and movement, and lighting."),
                system_prompt=VIDEO_DESCRIPTION_SYSTEM,
                **chat_kwargs,
            )
            desc = desc.strip()
            if desc:
                print(f"\nEnding description for {os.path.basename(video_path)}:\n{desc}\n")
                return desc
        except Exception as e:
            print(f"Warning: video description call failed for {video_path} ({e})")
        finally:
            # Clean up temp files (never the original clip or the saved
            # final-frame image next to it).
            for temp_path in temp_files:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
        return fallback_text or "The prior clip ends; resume from its final frame."

    @staticmethod
    def _classify_video_entry(video_entry):
        """Split a scene's "video" entry into (kind, value).

        Returns ("file", path) for a direct video file path, ("scene",
        scene_id) when it names another scene (resolved per clip at
        generation time), or (None, None) when there is no usable entry.
        """
        if not isinstance(video_entry, dict):
            return None, None
        path = video_entry.get("path")
        if not path:
            return None, None
        if os.path.isfile(path):
            return "file", path
        return "scene", path

    def _manifest_clip_description(self, scene_id, clip_key):
        """Look up a generated clip's recorded ending-state description.

        Every clip's ending description is stored in the manifest when it is
        generated, so a dependent scene reuses it instead of paying for a
        fresh vision call.
        """
        for entry in self.manifest["scenes"].get(scene_id, []):
            if entry.get("clip_key") == clip_key:
                return entry.get("video_description")
        return None

    def _resolve_scene_clip(self, ref_scene_id, clip_key, video_entry):
        """Resolve a scene-id "video" entry to one clip of the referenced scene.

        Clip N of the dependent scene continues from clip N of the referenced
        scene, keeping each take-line parallel through the movie. When the
        referenced scene produced fewer clips (a generation failed mid-scene),
        fall back to its last generated clip with a warning.

        Returns (video_path, ending_description). The description is the
        entry's explicit "description" when supplied, else the description
        recorded in the manifest for the referenced clip, else a fresh
        describe_video_end vision call.
        """
        scene_clips = self.generated_videos.get(ref_scene_id) or {}
        used_key = clip_key
        if used_key not in scene_clips and scene_clips:
            used_key = f"clip_{len(scene_clips)}"
            print(f"Warning: scene '{ref_scene_id}' has no {clip_key}; "
                  f"continuing from its last generated clip ({used_key}).")
        resolved = scene_clips.get(used_key)
        if not resolved:
            raise RuntimeError(
                f"No clips generated this run for scene '{ref_scene_id}' — "
                f"the referenced scene must run before this one.")

        desc = (video_entry.get("description")
                or self._manifest_clip_description(ref_scene_id, used_key)
                or self.describe_video_end(resolved))
        print(f"Resolved video entry '{ref_scene_id}' -> {resolved} ({used_key})")
        return resolved, desc

    # ------------------------------------------------------------------
    # User brief builders (FL2VA input contract)
    # ------------------------------------------------------------------
    def _inputs_manifest(self, image_entries, previous_video_desc=None):
        """Number the attached images in order and give their descriptions.

        img numbering follows attachment order, exactly as the FL2VA system
        prompt expects (img 1 = first attached image). A prior video is
        declared as vid 1 and described in text only.
        """
        lines = []
        if previous_video_desc:
            lines.append(f"vid 1 the prior clip — {previous_video_desc}")
        for i, entry in enumerate(image_entries, start=1):
            lines.append(f"img {i} {entry['description']}")
        return "\n".join(lines)

    def _build_brief(self, summary, image_entries, duration, previous_video_desc=None):
        """Assemble the full user brief for the FL2VA prompt writer."""
        return (
            f"{summary}\n\n"
            f"Target clip duration: {duration:.2f} seconds.\n\n"
            f"<inputs>\n{self._inputs_manifest(image_entries, previous_video_desc)}"
        )

    # ------------------------------------------------------------------
    # Scene-input tracking
    # ------------------------------------------------------------------
    def _save_scene_input(self, scene_dir, clip_name, *, scene_id, summary,
                          duration, image_entries, brief, prompt, system_prompt,
                          previous_video=None, previous_video_desc=None):
        """Save the exact inputs sent to the scene-builder AI as JSON.

        One ``{clip_name}_input.json`` file per clip, written next to the
        clips in the scene folder so every generated video can be traced
        back to the brief, system prompt, and assets that produced it.

        Returns the path of the saved JSON file.
        """
        data = {
            "scene_id": scene_id,
            "clip_name": clip_name,
            "created": datetime.now().isoformat(),
            "mode": "continuation" if previous_video else "first_clip",
            "summary": summary,
            "duration": duration,
            "images": [{"img": i, "path": e["path"], "description": e["description"]}
                       for i, e in enumerate(image_entries, start=1)],
            "previous_video": previous_video,
            "previous_video_description": previous_video_desc,
            "system_prompt_path": ("reconcile" if system_prompt == RECONCILE_SYSTEM
                                   else SYSTEM_PROMPT_v2v_PATH if previous_video is not None
                                   else SYSTEM_PROMPT_i2v_PATH),
            "system_prompt": system_prompt,
            "brief": brief,        # exact user message sent to the AI
            "fl2va_prompt": prompt,  # the AI's response (for convenience)
            "megapixels": self.megapixels,
            "ref_quality": self.ref_quality,
            "quant": self.quant,
        }
        path = os.path.join(scene_dir, f"{clip_name}_input.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return path

    # ------------------------------------------------------------------
    # LLM prompt generation
    # ------------------------------------------------------------------
    def write_scene_prompt(self, summary, image_entries, duration=None, predicted_ending=None):
        """Draft a scene's FL2VA prompt: images + summary -> prompt.

        When `predicted_ending` is given (a continuation scene drafted before
        the prior clip exists), it is embedded in the brief as the assumed
        prior-clip ending; the reconcile pass later corrects it against the
        actual video. Returns (fl2va_prompt, brief) — brief is the exact user
        message sent to the prompt-writer AI (tracked for reproducibility).
        """
        duration = duration or self.clip_duration
        brief = self._build_brief(summary, image_entries, duration)
        if predicted_ending:
            brief += (f"\n\nASSUMED PRIOR CLIP ENDING (predicted — the new clip "
                      f"must continue directly from this state):\n{predicted_ending}")
        prompt = self.ai.chat(
            prompt=brief,
            system_prompt=self.system_prompt_i2v,
            images=[e["path"] for e in image_entries],
        ).strip()
        if not prompt:
            raise RuntimeError("LLM returned an empty FL2VA prompt for a scene's first clip.")
        return prompt, brief

    def write_continuation_prompt(self, summary, image_entries, video_description,
                                  duration=None):
        """Continuation clip: prior clip's ending described in text -> FL2VA prompt.

        `video_description` comes from describe_video_end() (or the scene's
        explicit video entry) — the video itself is never sent to the LLM,
        only to iv2v as the motion reference.

        Returns (fl2va_prompt, brief) — brief is the exact user message
        sent to the prompt-writer AI (tracked for reproducibility).
        """
        brief = self._build_brief(
            summary,
            image_entries,
            duration or self.clip_duration,
            previous_video_desc=video_description,
        )
        prompt = self.ai.chat(
            prompt=brief,
            system_prompt=self.system_prompt_v2v,
            images=[e["path"] for e in image_entries],
        ).strip()
        if not prompt:
            raise RuntimeError("LLM returned an empty FL2VA prompt for a continuation clip.")
        return prompt, brief

    # ------------------------------------------------------------------
    # Parallel pipeline: single-scene lookahead (LLM overlaps diffusion)
    # ------------------------------------------------------------------
    # The LLM work is one heavy draft per scene (brief + reference images).
    # While the diffusion worker generates the current clip, a single
    # background thread drafts the NEXT scene's prompt — the pre-prompt. A
    # continuation scene is drafted directly in the finished v2v format
    # against an ASSUMED prior-clip ending: predicted from the referenced
    # scene's own brief when that clip doesn't exist yet, or described for
    # real immediately when the reference is a direct (already-existing)
    # file. reconcile_with_video (default False) can re-enable a vision pass
    # that corrects the draft against the real video once it exists.

    def _continuation_context(self, scene_cfg):
        """Resolve what a continuation scene's draft should assume for the
        prior clip's ending.

        Returns None for a cold open (no "video" entry). Otherwise returns
        a text description: the entry's explicit "description" always wins;
        a direct file reference is described for real right away (the file
        already exists); a scene-id reference is PREDICTED from the
        referenced scene's own brief, since that scene's clip doesn't exist
        yet when this draft is written.
        """
        video_entry = scene_cfg.get("video")
        if not video_entry:
            return None
        if video_entry.get("description"):
            return video_entry["description"]
        kind, ref = self._classify_video_entry(video_entry)
        if kind == "file":
            return self.describe_video_end(ref)
        if kind == "scene":
            ref_cfg = self.scenes.get(ref)
            if ref_cfg:
                return self._predict_ending(ref_cfg["summary"])
        return None

    def _draft_scene_prompt(self, scene_cfg, image_entries, summary, duration):
        """Draft one scene's FL2VA prompt, already in its finished shape.

        Returns (prompt, brief). A continuation scene is drafted directly
        with write_continuation_prompt against the assumed prior ending from
        _continuation_context; a cold open uses write_scene_prompt.
        """
        assumed_ending = self._continuation_context(scene_cfg)
        if assumed_ending:
            return self.write_continuation_prompt(
                summary, image_entries, assumed_ending, duration=duration)
        return self.write_scene_prompt(summary, image_entries, duration=duration)

    def _llm_worker_loop(self, buffer):
        """Background thread: consume buffered slots and fill their drafts.

        Each job is a (scene_id, scene_cfg, image_entries, summary,
        duration, slot) tuple; _draft_scene_prompt resolves cold open vs.
        continuation and drafts the finished prompt entirely on this
        background thread, off the diffusion path.
        """
        while True:
            job = buffer.get()
            if job is None:
                return
            scene_id, scene_cfg, image_entries, summary, duration, slot = job
            t0 = datetime.now()
            print(f"[llm] START draft {scene_id}  {t0:%H:%M:%S}")
            try:
                slot["draft"] = self._draft_scene_prompt(scene_cfg, image_entries, summary, duration)
            except Exception as e:
                slot["error"] = e
            slot["ready"].set()
            dt = (datetime.now() - t0).total_seconds()
            print(f"[llm] DONE  draft {scene_id}  ({dt:.1f}s)")

    def _submit_scene_draft(self, buffer, scene_id, scene_cfg, duration):
        """Buffer one scene's draft for the background LLM worker.

        Returns a slot dict: the diffusion thread later calls
        _take_draft(slot) to block until the draft is ready.
        """
        slot = {"ready": threading.Event(), "draft": None, "error": None}
        image_entries = self._scene_images(scene_cfg)
        summary = scene_cfg["summary"]
        print(f"[llm] queued draft for {scene_id}")
        buffer.put((scene_id, scene_cfg, image_entries, summary, duration, slot))
        return slot

    @staticmethod
    def _take_draft(slot):
        """Block until a buffered draft is ready; returns (prompt, brief)."""
        slot["ready"].wait()
        if slot["error"] is not None:
            raise slot["error"]
        return slot["draft"]

    def _predict_ending(self, summary):
        """Fast text-only guess at how a scene's clip will end, from its brief."""
        return self.ai.chat(
            prompt=(f"Scene brief the clip is being generated from:\n{summary}\n\n"
                    "Describe the clip's most likely ending state."),
            system_prompt=PREDICT_ENDING_SYSTEM,
        ).strip()

    def _reconcile_brief(self, summary, draft_prompt, duration):
        return (
            f"SCENE BRIEF (for context only — the draft already covers it):\n{summary}\n\n"
            f"TARGET DURATION: {duration:.2f} seconds.\n\n"
            f"DRAFT PROMPT (written against a PREDICTED prior ending — correct "
            f"it against the attached video):\n{draft_prompt}"
        )

    def reconcile_prompt_with_video(self, summary, draft_prompt, video_path, duration):
        """One vision pass: compare the draft against the actual prior video.

        The draft embedded an assumed prior ending; this shows the LLM the
        real clip and fixes only where the assumption was wrong, converting
        the draft into the finished continuation prompt. Returns (prompt,
        brief); on failure falls back to the draft unchanged. Only called
        when reconcile_with_video=True — bypassed by default (see
        _run_parallel).
        """
        brief = self._reconcile_brief(summary, draft_prompt, duration)
        try:
            prompt = self.ai.chat(
                prompt=brief,
                system_prompt=RECONCILE_SYSTEM,
                video=video_path,
            ).strip()
        except Exception as e:
            print(f"Warning: reconcile pass failed for {os.path.basename(video_path)} ({e}); "
                  "using the draft unchanged.")
            return draft_prompt, brief
        if not prompt:
            print("Warning: reconcile pass returned empty; using the draft unchanged.")
            return draft_prompt, brief
        return prompt, brief

    # ------------------------------------------------------------------
    # Clip generation (diffusion worker)
    # ------------------------------------------------------------------
    def generate_clip(self, prompt, ref_images, save_directory, file_name,
                      video_path=None, duration=None, seed=None):
        """Build the generation request and execute it on ComfyUI.

        Without `video_path` this is a fresh iv2v generation. With one, the
        v2v workflow extends the source video and produces TWO videos: the
        new extension clip, and the source video concatenated with the new
        clip. Only the new clip is persisted and returned — the assembled
        concatenation is left out of output_paths, so it is downloaded by
        the client but never written to disk.

        Returns (clip_path, final_frame_path) on success — the final frame
        is the SaveImage node output saved next to the clip (both iv2v and
        v2v workflows produce one). Returns (None, None) on failure.
        """
        overlap_offset = 2 #used to extend video to help account for lost frames in v2v from overlap
        #NOTE: Service type is FROZEN to avoid unloading comfy vram, but means must be handled outside of the workflow return
        if video_path is None:
            request = iv2v(
                prompt=prompt,
                image_path_list=ref_images,
                iv2v_workflow_path=WORKFLOWS["iv2v_h3"],
                save_directory=save_directory,
                duration=duration or self.clip_duration,
                megapixels=self.megapixels,
                aspect_ratio=self.aspect_ratio,
                turboLora=self.turbo_lora,
                ref_quality=self.ref_quality,
                quant=self.quant,
                seed=seed,
                file_name=file_name,
            )
        else:
            request = v2v(
                prompt=prompt,
                image_path_list=ref_images,
                video_path=video_path,
                v2v_workflow_path=WORKFLOWS["v2v_h3"],
                save_directory=save_directory,
                duration=(duration + overlap_offset) if duration is not None else self.clip_duration + overlap_offset,
                megapixels=self.megapixels,
                aspect_ratio=self.aspect_ratio,
                turboLora=self.turbo_lora,
                quant=self.quant,
                seed=seed,
                file_name=file_name,
            )
        if "error" in request:
            print(f"video request error: {request['error']}")
            return None, None

        # Persist only the NEW clip (request["node_id"]) and its
        # final-frame image; the v2v workflow's assembled source+extension
        # video (assemble_node_id) is deliberately left out of output_paths
        # so it is never written to disk, and the iv2v audio output is
        # likewise not persisted.
        output_paths = {request["node_id"]: os.path.join(request["save_path"], request["file_name"])}
        image_node = request.get("image_node_id")
        frame_name = request["file_prefix"].get(image_node) if image_node else None
        if image_node and frame_name:
            output_paths[image_node] = os.path.join(request["save_path"], frame_name)

        #Use a uniform service type here since service type is a flag to unload comfy vram
        #the different workflows use same exact models just different inputs, so don't unload
        result = self.comfy.generate(
            workflow=request["workflow"],
            service_type=self.service_type,
            input_files=request["input_files"],
            file_prefix=request["file_prefix"],
            output_paths=output_paths,
        )

        if not result.get("files"):
            print(f"Clip generation failed: {result.get('error', 'unknown error')}")
            return None, None

        # The new clip is the file we asked to persist for the save node —
        # match it by path so the v2v assembled source+clip video can never
        # be picked up by mistake (its saved_path is None anyway, since its
        # node isn't in output_paths; the audio entry's is None too).
        clip_path = os.path.join(request["save_path"], request["file_name"])
        saved = next((f.get("saved_path") for f in result["files"]
                      if f.get("saved_path") == clip_path), None)
        if saved is None:
            saved = next((f.get("saved_path") for f in result["files"]
                          if f.get("saved_path") and f.get("mime_type", "").startswith("video/")), None)
        frame = next((f.get("saved_path") for f in result["files"]
                      if f.get("saved_path") and f.get("mime_type", "").startswith("image/")), None)
        print(f"Clip saved: {saved}")
        if frame:
            print(f"Final frame saved: {frame}")
        return saved, frame

    # ------------------------------------------------------------------
    # Parallel pipeline: orchestrator
    # ------------------------------------------------------------------
    def _run_parallel(self):
        """Run the movie with the LLM drafting the next scene while the
        diffusion worker generates the current clip.

        One background LLM thread holds a single buffered pre-prompt: as the
        diffusion worker submits scene i's clip, the thread drafts scene
        i+1's prompt from its brief and reference images. A continuation
        scene's draft embeds a predicted ending of the prior clip; once the
        real prior clip exists, the diffusion thread runs one reconcile pass
        that compares the draft against the actual video and fixes the
        ending. The diffusion thread blocks only on the two things it truly
        needs — the current scene's draft and its referenced clip.
        """
        scene_ids = list(self.scenes.keys())
        from queue import Queue
        buffer = Queue()
        self._llm_worker = threading.Thread(
            target=self._llm_worker_loop, args=(buffer,), daemon=True,
            name="director-llm")
        self._llm_worker.start()

        dep_clips = {}    # scene_id -> {clip_key: (video_path, final_frame)}
        carry = None      # (video_path, desc) hand-off for chain_scenes
        self._stop_flagged = False

        # Scene 0 has nothing to overlap with — draft it inline right now.
        first_cfg = self.scenes[scene_ids[0]]
        first_duration = first_cfg.get("duration", self.clip_duration)
        first_summary = first_cfg["summary"]
        print(f"[llm] drafting prompt for {scene_ids[0]}")
        draft_slot = {"ready": threading.Event(), "draft": None, "error": None}
        try:
            draft_slot["draft"] = self._draft_scene_prompt(
                first_cfg, self._scene_images(first_cfg), first_summary, first_duration)
        except Exception as e:
            draft_slot["error"] = e
        draft_slot["ready"].set()

        for scene_index, scene_id in enumerate(scene_ids):
            if self._stop_flagged:
                break
            scene_cfg = self.scenes[scene_id]
            duration = scene_cfg.get("duration", self.clip_duration)
            summary = scene_cfg["summary"]
            scene_dir = os.path.join(self.output_dir, scene_id)
            os.makedirs(scene_dir, exist_ok=True)
            image_entries = self._scene_images(scene_cfg)
            ref_images = [e["path"] for e in image_entries]

            video_entry = scene_cfg.get("video")
            entry_kind, entry_ref = self._classify_video_entry(video_entry)

            if "clips" in scene_cfg:
                print(f"Warning: scene '{scene_id}' sets 'clips', which is ignored — "
                      f"all scenes generate clips_per_scene ({self.clips_per_scene}) clips.")
            if entry_kind == "scene" and entry_ref not in scene_ids[:scene_index]:
                raise ValueError(
                    f"Scene '{scene_id}' declares a 'video' entry with path "
                    f"'{entry_ref}', but that scene does not run before it.")

            inherited = carry if (self.chain_scenes and entry_kind is None) else None

            # Take this scene's draft (already buffered), then queue the next
            # scene's draft behind it so the LLM works through our diffusion.
            current_slot = draft_slot
            next_slot = None
            if scene_index + 1 < len(scene_ids):
                next_sid = scene_ids[scene_index + 1]
                next_cfg = self.scenes[next_sid]
                next_duration = next_cfg.get("duration", self.clip_duration)
                next_slot = self._submit_scene_draft(buffer, next_sid, next_cfg, next_duration)

            clips = []
            last_handoff = None
            for clip_idx in range(self.clips_per_scene):
                if self._stop_requested():
                    print("Stop requested - halting the run.")
                    self._stop_flagged = True
                    break
                clip_key = f"clip_{clip_idx + 1}"
                clip_name = f"{scene_id}_clip{clip_idx + 1:02d}"
                print(f"\n{'=' * 70}\n{scene_id} — clip {clip_idx + 1}/{self.clips_per_scene}\n{'=' * 70}")
                seed = random.randint(1, 1000000000)

                # --- this scene's draft (already buffered by the lookahead) ---
                print(f"[diff] {clip_name}: waiting for draft prompt")
                try:
                    draft_prompt, draft_brief = self._take_draft(current_slot)
                except Exception as e:
                    print(f"Draft prompt failed for {clip_name} ({e}); stopping scene.")
                    break
                print(f"[diff] {clip_name}: draft ready")

                # --- resolve the starting video ------------------------------
                previous_video = None
                if entry_kind == "file":
                    previous_video = entry_ref
                elif entry_kind == "scene":
                    dep_clip = dep_clips.get(entry_ref, {}).get(clip_key)
                    if not dep_clip or dep_clip[0] is None:
                        reason = f"referenced scene '{entry_ref}' did not produce {clip_key}"
                        print(f"Skipping {clip_name}: {reason}")
                        continue
                    previous_video = dep_clip[0]
                elif inherited is not None:
                    previous_video = inherited[0]

                # --- finalize the prompt ------------------------------------
                if previous_video is not None and self.reconcile_with_video:
                    # Reconcile the draft's assumed ending against the
                    # actual prior video (opt-in; see reconcile_with_video).
                    print(f"[llm] {clip_name}: reconcile pass (continuation from "
                          f"{os.path.basename(previous_video)})")
                    prompt, brief = self.reconcile_prompt_with_video(
                        summary, draft_prompt, previous_video, duration)
                    system_prompt = RECONCILE_SYSTEM
                else:
                    # Draft is already in its finished format (cold open, or
                    # a continuation drafted directly against an assumed
                    # ending) — used as-is, reconcile bypassed by default.
                    prompt, brief = draft_prompt, draft_brief
                    system_prompt = (self.system_prompt_v2v if previous_video is not None
                                     else self.system_prompt_i2v)
                previous_video_desc = None
                print(f"\nLLM prompt for {clip_name}:\n{prompt}\n")

                input_json = self._save_scene_input(
                    scene_dir, clip_name,
                    scene_id=scene_id, summary=summary, duration=duration,
                    image_entries=image_entries, brief=brief, prompt=prompt,
                    system_prompt=system_prompt,
                    previous_video=previous_video,
                    previous_video_desc=previous_video_desc,
                )

                # --- diffusion ----------------------------------------------
                print(f"[diff] {clip_name}: submitting to ComfyUI "
                      f"({'v2v' if previous_video else 'iv2v'}, seed={seed})")
                saved, gen_frame = self.generate_clip(
                    prompt=prompt, ref_images=ref_images,
                    save_directory=scene_dir, file_name=clip_name,
                    video_path=previous_video, duration=duration, seed=seed,
                )
                if saved is None:
                    print(f"Stopping scene {scene_id} after clip {clip_idx + 1} failed.")
                    break

                clips.append(saved)
                self.generated_videos.setdefault(scene_id, {})[clip_key] = saved
                self.manifest["scenes"].setdefault(scene_id, []).append({
                    "clip": saved,
                    "final_frame": gen_frame,
                    "clip_key": clip_key,
                    "clip_index": clip_idx + 1,
                    "input_json": input_json,
                    "prompt": prompt,
                    "ref_images": ref_images,
                    "continued_from": previous_video,
                    "seed": seed,
                    "turboLora": self.turbo_lora,
                    "quant": self.quant,
                })
                self._save_manifest()
                dep_clips.setdefault(scene_id, {})[clip_key] = (saved, gen_frame)
                last_handoff = (saved, (scene_id, clip_key))

            # Advance the lookahead: next scene's draft is the one we queued.
            draft_slot = next_slot

            # chain_scenes hand-off: describe this scene's last clip once for
            # the next scene to open from (only needed when chaining).
            if clips and last_handoff is not None and self.chain_scenes:
                path, (sid, ckey) = last_handoff
                carry = (path, self.describe_video_end(
                    path, final_frame_path=dep_clips[sid][ckey][1]))
            elif not clips:
                carry = None

        buffer.put(None)
        self._llm_worker.join()
        if not self._stop_flagged:
            self.assemble_clip_videos()
        print(f"\nMovie run complete. Manifest: {self.manifest_path}")
        return self.manifest

    def assemble_clip_videos(self):
        """Concatenate each clip index across scenes into one video per index.

        With S scenes generating N clips each, this produces N combined
        videos: combined_clip01.mp4 is every scene's clip 1 concatenated in
        scene order, combined_clip02.mp4 every scene's clip 2, and so on.
        Combined files are written to the base output directory and recorded
        in the manifest under "combined_videos".

        Uses combine_videos() mode="normalize": video is stream-copied (no
        quality loss) while audio is unified to 44.1kHz stereo AAC so clips
        with mismatched (or missing) audio still concatenate cleanly. A clip
        index that only one scene produced is skipped — a one-clip
        "combination" would just be a copy.

        Returns the list of combined video paths.
        """
        scene_ids = list(self.scenes.keys())
        max_clips = max(
            (len(self.generated_videos.get(sid, {})) for sid in scene_ids),
            default=0,
        )
        combined_paths = []
        for clip_idx in range(1, max_clips + 1):
            clip_key = f"clip_{clip_idx}"
            # Scene order = shooting order (scenes dict insertion order).
            video_list = [
                self.generated_videos[sid][clip_key]
                for sid in scene_ids
                if clip_key in self.generated_videos.get(sid, {})
            ]
            if len(video_list) < 2:
                if video_list:
                    print(f"\nOnly one clip for {clip_key} across all scenes - "
                          "skipping combine.")
                continue
            output_path = os.path.join(self.output_dir, f"combined_clip{clip_idx:02d}.mp4")
            print(f"\n{'=' * 70}\nCombining {len(video_list)} clips for "
                  f"{clip_key} -> {output_path}\n{'=' * 70}")
            try:
                combined = combine_videos(video_list, output_path, mode="normalize")
            except Exception as e:
                print(f"Warning: combining {clip_key} failed ({e}); "
                      "continuing with the next set.")
                continue
            combined_paths.append(combined)

        if combined_paths:
            self.manifest["combined_videos"] = combined_paths
            self._save_manifest()
        return combined_paths

    def run_scene(self, scene_id, scene_cfg, inherited_video=None):
        """Generate every clip for one scene.

        Args:
            inherited_video: optional (video_path, description) tuple carried
                over from the previous scene's last clip. Only used when the
                scene has no explicit "video" entry of its own.

        Returns:
            (clips, last_video) — clips is the list of saved clip paths;
            last_video is a (path, description) tuple of the final clip for
            chaining into the next scene, or None if nothing was generated.
        """
        summary = scene_cfg["summary"]
        # Per-scene "clips" overrides are deprecated: every scene generates
        # exactly clips_per_scene clips so that clip N of one scene can
        # always continue from clip N of the scene it references.
        if "clips" in scene_cfg:
            print(f"Warning: scene '{scene_id}' sets 'clips', which is ignored — "
                  f"all scenes generate clips_per_scene ({self.clips_per_scene}) clips.")
        n_clips = self.clips_per_scene
        duration = scene_cfg.get("duration", self.clip_duration)

        scene_dir = os.path.join(self.output_dir, scene_id)
        os.makedirs(scene_dir, exist_ok=True)

        image_entries = self._scene_images(scene_cfg)
        ref_images = [e["path"] for e in image_entries]

        # Starting video: the scene's explicit "video" entry wins over an
        # inherited clip from the previous scene. The entry's "path" may be a
        # direct file path (every clip of the scene opens from that file) or
        # the id of a previous scene — resolved PER CLIP below, so clip N of
        # this scene continues from clip N of the referenced scene and each
        # clip index forms its own parallel take-line through the movie.
        video_entry = scene_cfg.get("video")
        entry_kind, entry_ref = self._classify_video_entry(video_entry)

        ref_scene_id = None
        if entry_kind == "file":
            start_video = entry_ref
            # Describe the prior clip's ending state up front, before any
            # FL2VA prompt is requested — the contract is text-only for
            # videos, so the prompt writer needs this description.
            start_video_desc = (video_entry.get("description")
                                or self.describe_video_end(start_video))
        elif entry_kind == "scene":
            # A scene-id entry was declared but has no clips — fail loudly
            # rather than silently opening the scene cold.
            if not self.generated_videos.get(entry_ref):
                raise ValueError(
                    f"Scene '{scene_id}' declares a 'video' entry with path "
                    f"'{entry_ref}', but it could not be resolved "
                    f"(not a file, and no clips generated this run for that scene id). "
                    f"Make sure the referenced scene runs before this one.")
            ref_scene_id = entry_ref
            start_video, start_video_desc = None, None  # resolved per clip below
        elif inherited_video:
            start_video, start_video_desc = inherited_video
        else:
            start_video, start_video_desc = None, None

        clips = []
        for clip_idx in range(n_clips):
            clip_key = f"clip_{clip_idx + 1}"
            clip_name = f"{scene_id}_clip{clip_idx + 1:02d}"
            print(f"\n{'=' * 70}\n{scene_id} — clip {clip_idx + 1}/{n_clips}\n{'=' * 70}")

            # Each clip draws its own seed, recorded in the manifest.
            seed = random.randint(1, 1000000000)

            # Every clip in the scene is its own take — clips do NOT chain
            # into each other within a scene. A scene-id "video" entry
            # resolves to the referenced scene's clip at THIS clip's index
            # (take-lines stay parallel); a file entry, an inherited clip, or
            # a cold open is the same starting point for every clip.
            if ref_scene_id:
                previous_video, previous_video_desc = self._resolve_scene_clip(
                    ref_scene_id, clip_key, video_entry)
            else:
                previous_video, previous_video_desc = start_video, start_video_desc

            # 1. Get the FL2VA prompt from the LLM
            if previous_video is None:
                prompt, brief = self.write_scene_prompt(summary, image_entries, duration=duration)
            else:
                prompt, brief = self.write_continuation_prompt(
                    summary, image_entries, previous_video_desc, duration=duration)
            print(f"\nLLM prompt for {clip_name}:\n{prompt}\n")

            # 1b. Track the exact scene-builder inputs next to the clips
            input_json = self._save_scene_input(
                scene_dir, clip_name,
                scene_id=scene_id,
                summary=summary,
                duration=duration,
                image_entries=image_entries,
                brief=brief,
                prompt=prompt,
                system_prompt=(self.system_prompt_v2v if previous_video is not None
                               else self.system_prompt_i2v),
                previous_video=previous_video,
                previous_video_desc=previous_video_desc if previous_video else None,
            )

            # 2. Generate the clip (previous clip becomes the motion reference)
            saved, final_frame = self.generate_clip(
                prompt=prompt,
                ref_images=ref_images,
                save_directory=scene_dir,
                file_name=clip_name,
                video_path=previous_video,
                duration=duration,
                seed=seed
            )
            if saved is None:
                print(f"Stopping scene {scene_id} after clip {clip_idx} failed.")
                break

            clips.append(saved)
            # 3. Record this clip in the registry right away so downstream
            #    scenes can reference it by scene id and matching clip index.
            self.generated_videos.setdefault(scene_id, {})[clip_key] = saved

            # 4. Describe the new clip's ending state. It does not feed the
            #    next clip (every clip opens from the scene start) — it is
            #    recorded for the manifest and becomes this scene's chaining
            #    hand-off via the last successful clip. The saved final frame
            #    is sent to the vision call when the workflow produced one.
            previous_video_desc = self.describe_video_end(saved, final_frame_path=final_frame)

            # 5. Record in the manifest after every clip (crash-safe)
            self.manifest["scenes"].setdefault(scene_id, []).append({
                "clip": saved,
                "final_frame": final_frame,
                "clip_key": clip_key,
                "clip_index": clip_idx + 1,
                "input_json": input_json,
                "prompt": prompt,
                "ref_images": ref_images,
                # Clips don't chain within a scene: every clip opens from the
                # starting video resolved for THIS clip index (the matching
                # clip of the referenced scene, the scene's video file, the
                # inherited clip, or None for a cold open).
                "continued_from": previous_video,
                "video_description": previous_video_desc,
                "seed": seed,
                "turboLora": self.turbo_lora,
                "quant": self.quant,
            })
            self._save_manifest()

            # Register right away so dependent scenes can resolve this clip.
            self.generated_videos.setdefault(scene_id, {})[clip_key] = saved

        # chain_scenes hand-off: the scene's last generated clip plus its
        # ending description, for the next scene to open from.
        last_video = (clips[-1], previous_video_desc) if clips else None
        return clips, last_video

    def run(self):
        """Run the full movie: every scene in dict order.

        With parallel_llm_diff=True a background LLM thread buffers the next
        scene's draft while the diffusion worker generates the current clip;
        otherwise the original serial per-scene loop runs.
        """
        print(f"\nStarting movie run: {len(self.scenes)} scene(s) -> {self.output_dir}")
        if self.parallel_llm_diff:
            return self._run_parallel()
        carry = None  # (video_path, description) from the previous scene's last clip
        for scene_id, scene_cfg in self.scenes.items():
            inherited = carry if self.chain_scenes else None
            _, carry = self.run_scene(scene_id, scene_cfg, inherited_video=inherited)
        self.assemble_clip_videos()
        print(f"\nMovie run complete. Manifest: {self.manifest_path}")
        return self.manifest

    def _save_manifest(self):
        with open(self.manifest_path, "w") as f:
            json.dump(self.manifest, f, indent=2)


# ---------------------------------------------------------------------------
# Example / test entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    BASE = r"C:\Users\jared\Documents\code\haughtstudio\automation\luxuryAptEncounter"

    CHARACTER_FOLDER = "maya"

    SHEET = {"path": os.path.join(BASE, CHARACTER_FOLDER, "nsfw_char_sheet_1.png"),
             "description": "subject sheet, front three-quarter and full-body views of the woman"}
    SHEET_NSFW = {"path": os.path.join(BASE, CHARACTER_FOLDER, "nsfw_char_sheet.png"),
                 "description": "subject sheet, front three-quarter and full-body views of the woman"}
    CLOSEUP = {"path": os.path.join(BASE, CHARACTER_FOLDER, "close_up.png"),
               "description": "close-up of the same woman's face"}
    
    comfy_client = ComfyUIlocal()
    # Free VRAM before running
    comfy_client.aggressive_cleanup()


    director = MovieDirector(
        scenes={
            "scene_01_kitchen": {
                "summary": "The character makes coffee in the kitchen, humming to "
                           "herself in the distance, camera pushes towards her quickly then gets startled she notices the someone watching, and smiles and laughs flirtatiously, saying in a soft British accent 'Oh, you scared me!', then she swings her hand at the camera in a playful way and says 'don't do that!'",
                "img_1": SHEET,
                "img_2": CLOSEUP,
                "img_3": {"path": os.path.join(BASE, "apt_kitchen.png"),
                          "description": "empty modern apartment kitchen, morning light"},
                #"clips": 4,
            },
            "scene_02_livingroom": {
                "summary": "She walks into the living room, sits on the couch, and "
                           "in a soft British accent says 'do you want to feel my tits' she is looking directly at camera. a hand reaches forward from edge of frame, only their hand and arm are visible. they grab and fondle her left breast squeeze breast flesh deforms slightly, she moans when they do and smiles looking at the camera",
                "img_1": SHEET,
                "img_2": CLOSEUP,
                "img_3": {"path": os.path.join(BASE, "apt_living2bedroom_2.png"),
                          "description": "empty apartment living room with a sofa, morning light"},
                #"clips": 4,
                
            },
            "scene_03_livingroom": {
                "summary": "She lifts her shirt over her head, adjusting her shoulders, then her raised arms to get the garmet off, she throws it behind her on the ground. she has a white bra"
                            "in a soft British accent says 'I want you to' her tongue comes out of her mouth slightly and traces her top lip seductively moving from the right side to the left, then she continues speaking and says" 
                            "'fuck me right here on the couch', she is still centered in camera view half body shot, two arms come into frame from the edge of frame and grab her under her arms below her breasts, and she smiles and laughs as she lifts slightly into the air, scene ends there",
                "img_1": SHEET,
                "img_2": CLOSEUP,
                "img_3": {"path": os.path.join(BASE, "apt_living2bedroom_2.png"),
                                      "description": "empty apartment living room with a sofa, morning light"},
                #"clips": 4,
                
                "video": {"path": "scene_02_livingroom", "description": None},
                        },
            "scene_04_livingroom": {
                "summary": "Continuing from the playful lift — he lowers her back onto the sofa, her sheer bra barely containing her breasts, camera moves to view her from a high angle looking down at her POV, and she reaches up to pull him closer, whispering in a soft British accent 'don't keep me waiting'. His hands slide from under her arms down to her waist, she spreads her legs as she arches her back into the cushions, breathing heavier, eyes locked on the camera with a hungry smile. Scene ends with her pulling his face toward her chest.",
                "img_1": SHEET,
                "img_2": CLOSEUP,
                "img_3": {"path": os.path.join(BASE, "apt_living2bedroom_2.png"),
                          "description": "empty apartment living room with a sofa, morning light"},
                #"clips": 4,
                "video": {"path": "scene_03_livingroom", "description": None},
            },
            "scene_05_livingroom": {
                "summary": "Now fully reclined on the sofa, she unhooks her bra from the front and lets it fall, exposing her bare breasts to the camera, the move with soft body fluid dynamics recoiling from the motion, she then guides his head down to them while letting out a soft moan, his head between her breasts he moves his head side to side. In a breathy British accent she gasps 'yes, suck my titties, just like that', tangling her fingers in his hair as the camera slowly pushes in on her flushed face and heaving chest. Scene ends with her biting her lip and pulling him up for a kiss.",
                "img_1": SHEET_NSFW,
                "img_2": CLOSEUP,
                "img_3": {"path": os.path.join(BASE, "apt_living2bedroom_2.png"),
                          "description": "empty apartment living room with a sofa, morning light"},
                #"clips": 4,
                "video": {"path": "scene_04_livingroom", "description": None},
            },
        },
        comfy_client=comfy_client,
        output_dir=os.path.join(BASE, CHARACTER_FOLDER, "movie_output"),
        clips_per_scene=1,
        chain_scenes=False, #video chaining is handled manually in the scene configs above with the 'video' entry, so this is set to False
        clip_duration=10.0,
        turbo_lora=True,
        ref_quality="match",#max or match
        quant="int8",#int8 or bf16 only
        parallel_llm_diff=True,# LLM prompt pool overlaps diffusion GPU
    )

    director.run()
