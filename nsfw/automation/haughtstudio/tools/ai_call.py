"""
ai_call.py - Simple client for a locally running, OpenAI-compatible
llama.cpp server (default http://localhost:8080).

Supports:
  - system prompts
  - image inputs (base64 data URIs)
  - video inputs (ffmpeg samples frames at 2fps — always including the last
    frame — longest edge 640px, aspect preserved; each frame is sent as a
    base64 JPEG image_url because llama.cpp vision models only accept images,
    not raw video)

Intended to be imported as a module; see the __main__ section for usage.
"""

import base64
import mimetypes
import os
import shutil
import subprocess
import tempfile
import time

import requests


class AICall:
    """Client for a local OpenAI-compatible llama.cpp server."""

    def __init__(self,
                 base_url="http://localhost:8080",
                 model="local-model",
                 temperature=0.7,
                 max_tokens=1024,
                 timeout=600,
                 retries=3,
                 retry_backoff=20):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        # Retries cover transient llama.cpp stalls (slow prompt processing,
        # VRAM contention with ComfyUI) without killing long batch runs.
        self.retries = retries
        self.retry_backoff = retry_backoff

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def chat(self, prompt, system_prompt=None, images=None, video=None):
        """Send a chat completion request with optional media.

        Args:
            prompt:        user text prompt (required)
            system_prompt: optional system prompt
            images:        a path string or list of image paths
            video:         optional path to a video file — ffmpeg samples
                           frames at 2fps (longest edge 640, aspect kept;
                           last frame always included) sent as image_urls

        Returns:
            The assistant's reply text.
        """
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        # normalize images to a list
        if isinstance(images, str):
            images = [images]

        content = []
        if images:
            for image_path in images:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": self._image_to_data_uri(image_path)},
                })
        if video:
            frames = self._video_to_frame_uris(video)
            content.append({
                "type": "text",
                "text": f"<|video_start|> ({len(frames)} frames from video: "
                        f"{os.path.basename(video)})",
            })
            for uri in frames:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": uri},
                })
            content.append({"type": "text", "text": "<|video_end|>"})
        content.append({"type": "text", "text": prompt})

        # use plain string content when there is no media attached
        user_content = content if len(content) > 1 else prompt
        messages.append({"role": "user", "content": user_content})

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }

        response = None
        for attempt in range(self.retries + 1):
            try:
                response = requests.post(
                    f"{self.base_url}/v1/chat/completions",
                    json=payload,
                    timeout=self.timeout,
                )
                break
            except (requests.exceptions.Timeout,
                    requests.exceptions.ConnectionError) as e:
                if attempt >= self.retries:
                    raise
                wait = self.retry_backoff * (attempt + 1)
                print(f"Warning: LLM request failed ({e}); "
                      f"retrying in {wait}s (attempt {attempt + 1}/{self.retries})...")
                time.sleep(wait)

        response.raise_for_status()
        data = response.json()
        choice = data["choices"][0]
        message = choice.get("message") or {}
        content = message.get("content") or ""
        finish_reason = choice.get("finish_reason")

        if not content.strip():
            # Reasoning models (heretic/ablit, gpt-oss, qwen3, ...) put their
            # thinking in `reasoning_content`; if that burns the whole token
            # budget the server returns finish_reason="length" with empty
            # content. Surface that instead of silently returning "".
            reasoning = (message.get("reasoning_content") or "").strip()
            detail = (
                f"LLM returned empty content (finish_reason={finish_reason!r}, "
                f"max_tokens={self.max_tokens})."
            )
            if reasoning:
                detail += (" The model produced reasoning only — likely hit the "
                           "token limit before answering. Raise max_tokens.\n"
                           f"Reasoning tail: ...{reasoning[-400:]}")
            raise RuntimeError(detail)

        if finish_reason == "length":
            print(f"Warning: LLM reply truncated at max_tokens={self.max_tokens}.")
        return content

    # ------------------------------------------------------------------
    # media helpers
    # ------------------------------------------------------------------
    # Vision models Downscale to longest edge 640 (aspect kept) so multi-image
    # briefs don't blow the context. ffmpeg handles the resize + JPEG
    # re-encode; PNGs with alpha are flattened onto black first.
    IMAGE_LONGEST_EDGE = 800
    VIDEO_SAMPLE_FPS = 4.0
    VIDEO_LONGEST_EDGE = 640

    @staticmethod
    def _scale_expr(longest_edge):
        """ffmpeg scale expression: longest edge -> longest_edge, aspect kept
        (-2 keeps the other dimension even). Never upscales."""
        return (
            f"scale='if(gt(iw,ih),min({longest_edge},iw),-2)':"
            f"'if(gt(iw,ih),-2,min({longest_edge},ih))'"
        )

    @classmethod
    def _image_to_data_uri(cls, image_path, longest_edge=None):
        """Downscale an image via ffmpeg (longest edge `longest_edge`, aspect
        preserved) and encode it as a base64 JPEG data URI."""
        edge = longest_edge or cls.IMAGE_LONGEST_EDGE
        fd, tmp_path = tempfile.mkstemp(prefix="ai_call_img_", suffix=".jpg")
        os.close(fd)
        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-i", image_path,
                    "-vf", cls._scale_expr(edge),
                    "-frames:v", "1",
                    "-q:v", "4",
                    tmp_path,
                ],
                capture_output=True,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"ffmpeg failed on {image_path}:\n"
                    f"{result.stderr.decode(errors='replace')}"
                )
            with open(tmp_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            return f"data:image/jpeg;base64,{b64}"
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    @staticmethod
    def _video_to_frame_uris(video_path, sample_fps=None, longest_edge=None):
        """Sample frames from a video at `sample_fps` frames-per-second with
        ffmpeg, scaled so the longest edge is `longest_edge` px (aspect
        preserved), and return them as base64 JPEG data URIs.

        The TRUE last frame of the video is always appended if the 4fps
        sampling didn't already land on it.

        llama.cpp vision models can't ingest raw video — frames-as-images is
        the standard workaround (same pattern app.py uses).
        """
        if sample_fps is None:
            sample_fps = AICall.VIDEO_SAMPLE_FPS
        if sample_fps <= 0:
            return []

        # probe source fps + total frames so we can reason about the sampling
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=avg_frame_rate,nb_frames",
                "-of", "default=noprint_wrappers=1",
                video_path,
            ],
            capture_output=True, text=True,
        )
        src_fps, total_frames = 0.0, 0
        for line in probe.stdout.splitlines():
            if line.startswith("avg_frame_rate="):
                num, _, den = line.split("=", 1)[1].partition("/")
                try:
                    src_fps = float(num) / float(den or 1)
                except ValueError:
                    src_fps = 0.0
            elif line.startswith("nb_frames="):
                try:
                    total_frames = int(line.split("=", 1)[1])
                except ValueError:
                    total_frames = 0

        # effective sampling rate: never upsample above the source fps
        fps = min(sample_fps, src_fps) if src_fps > 0 else sample_fps

        # longest edge -> longest_edge, other dimension auto (-2 keeps it even)
        if longest_edge is None:
            longest_edge = AICall.VIDEO_LONGEST_EDGE
            
        scale = AICall._scale_expr(longest_edge)

        tmp_dir = tempfile.mkdtemp(prefix="ai_call_frames_")
        out_pattern = os.path.join(tmp_dir, "frame_%04d.jpg")
        try:
            cmd = [
                "ffmpeg", "-y",
                "-i", video_path,
                "-vf", f"fps={fps},{scale}",
                "-q:v", "4",
                "-an",
                out_pattern,
            ]
            result = subprocess.run(cmd, capture_output=True)
            if result.returncode != 0:
                raise RuntimeError(
                    f"ffmpeg failed on {video_path}:\n"
                    f"{result.stderr.decode(errors='replace')}"
                )

            frame_files = sorted(
                n for n in os.listdir(tmp_dir) if n.lower().endswith(".jpg")
            )

            # ---- always include the TRUE last frame ----
            # the fps filter's final output frame sits at
            # floor((total_frames-1) / src_fps * fps) — if that isn't the last
            # source frame, extract it explicitly and append it.
            need_last = total_frames > 0
            if need_last and frame_files and src_fps > 0:
                last_sampled_src = int((len(frame_files) - 1) * src_fps / fps)
                need_last = last_sampled_src < total_frames - 1

            if need_last:
                last_path = os.path.join(tmp_dir, "zzzz_last.jpg")
                last_cmd = [
                    "ffmpeg", "-y",
                    "-sseof", "-0.1",          # seek to just before the end
                    "-i", video_path,
                    "-vf", scale,
                    "-frames:v", "1",
                    "-q:v", "4",
                    "-an",
                    last_path,
                ]
                last_res = subprocess.run(last_cmd, capture_output=True)
                if last_res.returncode == 0 and os.path.exists(last_path):
                    frame_files.append("zzzz_last.jpg")

            uris = []
            for name in frame_files:
                with open(os.path.join(tmp_dir, name), "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("ascii")
                uris.append(f"data:image/jpeg;base64,{b64}")
            return uris
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    ai = AICall()

    # --- text-only sanity check ---
    print(ai.chat(
        prompt="Say hello in one sentence.",
        system_prompt="You are a helpful assistant. Answer concisely.",
    ))

    # --- media test (replace placeholders with real paths) ---
    reply = ai.chat(
         prompt="Describe what you see in the provided media.",
         system_prompt="You are a helpful vision assistant.",
         images=[
             r"C:\Users\jared\Documents\code\haughtstudio\automation\luxuryAptEncounter\fiona\1_nsfw_1.png",
            
         ],
         video=r"C:\Users\jared\Documents\code\haughtstudio\automation\luxuryAptEncounter\wanI2V_00011.mp4",
     )
    print(reply)
