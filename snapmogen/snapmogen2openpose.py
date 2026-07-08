#!/usr/bin/env python3
"""
snapmogen_to_openpose.py
========================
Convert SnapMoGen BVH mocap clips into OpenPose/DWPose-style pose control
sequences for ComfyUI (LTX-Video / LTX 2 pose conditioning, ControlNet, etc.)

Outputs (per BVH):
  1. A folder of DWPose-style skeleton PNGs (black background, standard
     OpenPose BODY_18 colors) -> load with "Load Images" / VHS in ComfyUI.
  2. Optional MP4 preview / pose video (--mp4).
  3. Optional OpenPose-format JSON keypoints per frame (--json), for nodes
     that consume raw keypoints instead of rendered images.

Usage:
  python snapmogen_to_openpose.py input.bvh -o out_dir
  python snapmogen_to_openpose.py bvh_folder/ -o out_dir --fps 24 --mp4
  python snapmogen_to_openpose.py input.bvh -o out --width 704 --height 1216 \
      --yaw 30 --follow --max-frames 121

Requires: numpy, opencv-python
"""

import argparse
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field

import numpy as np

try:
    import cv2
except ImportError:
    sys.exit("opencv-python is required:  pip install opencv-python")


# --------------------------------------------------------------------------
# BVH parsing
# --------------------------------------------------------------------------

@dataclass
class Joint:
    name: str
    parent: int
    offset: np.ndarray
    channels: list = field(default_factory=list)  # e.g. ['Xposition',...,'Zrotation']


def parse_bvh(path):
    """Minimal, robust BVH parser. Returns (joints, motion[F, C], frame_time)."""
    with open(path, "r") as f:
        text = f.read()

    tokens = re.split(r"\s+", text.strip())
    i = 0

    def tok():
        nonlocal i
        t = tokens[i]
        i += 1
        return t

    joints = []
    stack = []

    assert tok().upper() == "HIERARCHY", "Not a BVH file"

    while i < len(tokens):
        t = tok()
        tu = t.upper()
        if tu in ("ROOT", "JOINT"):
            name = tok()
            parent = stack[-1] if stack else -1
            joints.append(Joint(name=name, parent=parent, offset=np.zeros(3)))
            jidx = len(joints) - 1
            assert tok() == "{"
            stack.append(jidx)
        elif tu == "END":  # "End Site"
            tok()  # 'Site'
            assert tok() == "{"
            # consume OFFSET x y z
            assert tok().upper() == "OFFSET"
            off = np.array([float(tok()), float(tok()), float(tok())])
            # store as an end-site child (no channels)
            joints.append(Joint(name=joints[stack[-1]].name + "_end",
                                parent=stack[-1], offset=off))
            stack.append(len(joints) - 1)
        elif tu == "OFFSET":
            joints[stack[-1]].offset = np.array(
                [float(tok()), float(tok()), float(tok())])
        elif tu == "CHANNELS":
            n = int(tok())
            joints[stack[-1]].channels = [tok() for _ in range(n)]
        elif t == "}":
            stack.pop()
        elif tu == "MOTION":
            break

    assert tok().upper().startswith("FRAMES")
    nxt = tok()
    n_frames = int(nxt if nxt != ":" else tok())
    assert tok().upper() == "FRAME"
    assert tok().upper().startswith("TIME")
    nxt = tok()
    frame_time = float(nxt if nxt != ":" else tok())

    values = np.array([float(x) for x in tokens[i:]], dtype=np.float64)
    n_channels = sum(len(j.channels) for j in joints)
    if values.size < n_frames * n_channels:
        n_frames = values.size // n_channels
    motion = values[: n_frames * n_channels].reshape(n_frames, n_channels)
    return joints, motion, frame_time


# --------------------------------------------------------------------------
# Forward kinematics
# --------------------------------------------------------------------------

def _rot(axis, deg):
    """Rotation matrices for an array of angles (degrees). Returns (F,3,3)."""
    r = np.radians(deg)
    c, s = np.cos(r), np.sin(r)
    F = r.shape[0]
    M = np.zeros((F, 3, 3))
    if axis == "x":
        M[:, 0, 0] = 1
        M[:, 1, 1], M[:, 1, 2] = c, -s
        M[:, 2, 1], M[:, 2, 2] = s, c
    elif axis == "y":
        M[:, 1, 1] = 1
        M[:, 0, 0], M[:, 0, 2] = c, s
        M[:, 2, 0], M[:, 2, 2] = -s, c
    else:
        M[:, 2, 2] = 1
        M[:, 0, 0], M[:, 0, 1] = c, -s
        M[:, 1, 0], M[:, 1, 1] = s, c
    return M


def forward_kinematics(joints, motion):
    """Returns global positions (F, J, 3) and global rotations (F, J, 3, 3)."""
    F = motion.shape[0]
    J = len(joints)
    pos = np.zeros((F, J, 3))
    rot = np.tile(np.eye(3), (F, J, 1, 1))

    ch_index = 0
    for jidx, joint in enumerate(joints):
        # local rotation from channels (applied in listed order)
        local_R = np.tile(np.eye(3), (F, 1, 1))
        local_T = np.tile(joint.offset, (F, 1))
        for ch in joint.channels:
            vals = motion[:, ch_index]
            ch_index += 1
            cl = ch.lower()
            if cl.endswith("rotation"):
                local_R = local_R @ _rot(cl[0], vals)
            elif cl.endswith("position"):
                # Position channels replace the offset on that axis
                # (same convention as SnapMoGen's own bvh_io.py)
                local_T[:, {"x": 0, "y": 1, "z": 2}[cl[0]]] = vals

        p = joint.parent
        if p == -1:
            rot[:, jidx] = local_R
            pos[:, jidx] = local_T
        else:
            rot[:, jidx] = rot[:, p] @ local_R
            pos[:, jidx] = pos[:, p] + np.einsum("fij,fj->fi", rot[:, p], local_T)
    return pos, rot


# --------------------------------------------------------------------------
# SnapMoGen skeleton -> OpenPose BODY_18 mapping
# --------------------------------------------------------------------------
# OpenPose BODY_18 (COCO) order:
# 0 Nose, 1 Neck, 2 RShoulder, 3 RElbow, 4 RWrist, 5 LShoulder, 6 LElbow,
# 7 LWrist, 8 RHip, 9 RKnee, 10 RAnkle, 11 LHip, 12 LKnee, 13 LAnkle,
# 14 REye, 15 LEye, 16 REar, 17 LEar

SNAP_MAP = {
    "neck_base": "C_neck0001_bind_JNT",
    "head": "C_head_bind_JNT",
    "r_shoulder": "R_armUpper0001_bind_JNT",
    "r_elbow": "R_armLower0001_bind_JNT",
    "r_wrist": "R_hand0001_bind_JNT",
    "l_shoulder": "L_armUpper0001_bind_JNT",
    "l_elbow": "L_armLower0001_bind_JNT",
    "l_wrist": "L_hand0001_bind_JNT",
    "r_hip": "R_legUpper0001_bind_JNT",
    "r_knee": "R_legLower0001_bind_JNT",
    "r_ankle": "R_foot0001_bind_JNT",
    "l_hip": "L_legUpper0001_bind_JNT",
    "l_knee": "L_legLower0001_bind_JNT",
    "l_ankle": "L_foot0001_bind_JNT",
}


def find_joint(joints, name):
    for i, j in enumerate(joints):
        if j.name == name:
            return i
    # fuzzy fallback (in case dataset naming varies slightly)
    for i, j in enumerate(joints):
        if name.lower() in j.name.lower():
            return i
    raise KeyError(f"Joint '{name}' not found. Available: {[j.name for j in joints]}")


# ---------------------------------------------------------------------------
# Face keypoint offset multipliers (all values relative to head_len).
# Tune these to dial in where each landmark lands on the skull.
#   Each entry: (up, fwd, side)
#   'side' is applied as +side for the right keypoint, -side for left.
# ---------------------------------------------------------------------------
HEAD_KP_OFFSETS = {
    "nose": (0.55, 0.85, 0.00),
    "eye":  (0.80, 0.72, 0.30),
    "ear":  (0.65, 0.10, 0.62),
}


def build_openpose_3d(joints, pos, rot):
    """Map SnapMoGen joints to 18 OpenPose keypoints in 3D. Returns (F,18,3)."""
    idx = {k: find_joint(joints, v) for k, v in SNAP_MAP.items()}
    head_i = idx["head"]
    F = pos.shape[0]
    kp = np.zeros((F, 18, 3))

    # Head frame axes for synthesizing face keypoints.
    # SnapMoGen rest pose is T-pose facing +Z, Y-up, cm units.
    head_R = rot[:, head_i]            # (F,3,3)
    head_p = pos[:, head_i]            # (F,3)
    fwd = head_R[:, :, 2]              # local +Z = forward
    up = head_R[:, :, 1]               # local +Y = up
    right_of_char = -head_R[:, :, 0]   # character's right = -X (X points to char left)

    # Approximate head size from neck->head bone length
    neck_p = pos[:, idx["neck_base"]]
    head_len = np.linalg.norm(head_p - neck_p, axis=1, keepdims=True)
    head_len = np.clip(head_len, 6.0, 14.0)  # cm, sane bounds

    n_up,  n_fwd,  _      = HEAD_KP_OFFSETS["nose"]
    e_up,  e_fwd,  e_side = HEAD_KP_OFFSETS["eye"]
    ea_up, ea_fwd, ea_side = HEAD_KP_OFFSETS["ear"]

    nose  = head_p + up * head_len * n_up  + fwd * head_len * n_fwd
    r_eye = head_p + up * head_len * e_up  + fwd * head_len * e_fwd  + right_of_char * head_len * e_side
    l_eye = head_p + up * head_len * e_up  + fwd * head_len * e_fwd  - right_of_char * head_len * e_side
    r_ear = head_p + up * head_len * ea_up + fwd * head_len * ea_fwd + right_of_char * head_len * ea_side
    l_ear = head_p + up * head_len * ea_up + fwd * head_len * ea_fwd - right_of_char * head_len * ea_side

    # OpenPose "neck" = midpoint of shoulders
    neck = 0.5 * (pos[:, idx["r_shoulder"]] + pos[:, idx["l_shoulder"]])

    kp[:, 0] = nose
    kp[:, 1] = neck
    kp[:, 2] = pos[:, idx["r_shoulder"]]
    kp[:, 3] = pos[:, idx["r_elbow"]]
    kp[:, 4] = pos[:, idx["r_wrist"]]
    kp[:, 5] = pos[:, idx["l_shoulder"]]
    kp[:, 6] = pos[:, idx["l_elbow"]]
    kp[:, 7] = pos[:, idx["l_wrist"]]
    kp[:, 8] = pos[:, idx["r_hip"]]
    kp[:, 9] = pos[:, idx["r_knee"]]
    kp[:, 10] = pos[:, idx["r_ankle"]]
    kp[:, 11] = pos[:, idx["l_hip"]]
    kp[:, 12] = pos[:, idx["l_knee"]]
    kp[:, 13] = pos[:, idx["l_ankle"]]
    kp[:, 14] = r_eye
    kp[:, 15] = l_eye
    kp[:, 16] = r_ear
    kp[:, 17] = l_ear
    return kp


# --------------------------------------------------------------------------
# Camera + projection
# --------------------------------------------------------------------------

def project(kp3d, width, height, yaw_deg=0.0, pitch_deg=0.0,
            fov_deg=40.0, margin=0.82, follow=False):
    """
    Perspective-project 3D keypoints (F,18,3) to pixels (F,18,2) with a
    static (or root-following) camera that auto-frames the motion.
    Returns (kp2d, visible_mask) where behind-camera points are masked.
    """
    F = kp3d.shape[0]
    yaw = math.radians(yaw_deg)
    pitch = math.radians(pitch_deg)

    # world -> camera rotation (yaw around Y, then pitch around X)
    Ry = np.array([[math.cos(yaw), 0, math.sin(yaw)],
                   [0, 1, 0],
                   [-math.sin(yaw), 0, math.cos(yaw)]])
    Rx = np.array([[1, 0, 0],
                   [0, math.cos(pitch), -math.sin(pitch)],
                   [0, math.sin(pitch), math.cos(pitch)]])
    Rcam = Rx @ Ry

    pts = kp3d.copy()
    if follow:
        # cancel horizontal root travel so character stays centered
        pelvis = 0.5 * (pts[:, 8] + pts[:, 11])
        offset = pelvis.copy()
        offset[:, 1] = 0.0
        pts = pts - offset[:, None, :]

    # rotate world into camera orientation first
    pts_r = np.einsum("ij,fkj->fki", Rcam, pts)

    # bounding box of the whole (rotated) motion
    mn = pts_r.reshape(-1, 3).min(0)
    mx = pts_r.reshape(-1, 3).max(0)
    center = 0.5 * (mn + mx)
    span_x = mx[0] - mn[0]
    span_y = mx[1] - mn[1]

    f = 0.5 / math.tan(math.radians(fov_deg) / 2)  # focal (normalized)
    aspect = width / height
    # distance so bbox fits with margin in both axes
    d_x = (span_x / (2 * margin * aspect)) / (0.5 / f)
    d_y = (span_y / (2 * margin)) / (0.5 / f)
    depth_pad = 0.5 * (mx[2] - mn[2])
    dist = max(d_x, d_y) + depth_pad + 1e-6

    cam_pos = center + np.array([0, 0, dist])
    rel = pts_r - cam_pos
    z = -rel[:, :, 2]  # camera looks down -Z
    z = np.maximum(z, 1e-6)
    x_ndc = (rel[:, :, 0] / z) * f / aspect
    y_ndc = (rel[:, :, 1] / z) * f

    u = (x_ndc + 0.5) * width
    v = (0.5 - y_ndc) * height
    kp2d = np.stack([u, v], axis=-1)
    visible = (-rel[:, :, 2]) > 1e-4
    return kp2d, visible


# --------------------------------------------------------------------------
# DWPose / OpenPose rendering (matches controlnet_aux draw_bodypose style)
# --------------------------------------------------------------------------

LIMB_SEQ = [  # 0-indexed BODY_18 pairs, canonical OpenPose order
    (1, 2), (1, 5), (2, 3), (3, 4), (5, 6), (6, 7), (1, 8), (8, 9), (9, 10),
    (1, 11), (11, 12), (12, 13), (1, 0), (0, 14), (14, 16), (0, 15), (15, 17),
]

POSE_COLORS = [
    (255, 0, 0), (255, 85, 0), (255, 170, 0), (255, 255, 0), (170, 255, 0),
    (85, 255, 0), (0, 255, 0), (0, 255, 85), (0, 255, 170), (0, 255, 255),
    (0, 170, 255), (0, 85, 255), (0, 0, 255), (85, 0, 255), (170, 0, 255),
    (255, 0, 255), (255, 0, 170), (255, 0, 85),
]


def draw_pose(kp2d, visible, width, height, stickwidth=4):
    canvas = np.zeros((height, width, 3), dtype=np.uint8)
    for li, (a, b) in enumerate(LIMB_SEQ):
        if not (visible[a] and visible[b]):
            continue
        x1, y1 = kp2d[a]
        x2, y2 = kp2d[b]
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        length = math.hypot(x2 - x1, y2 - y1)
        if length < 1e-3:
            continue
        angle = math.degrees(math.atan2(y2 - y1, x2 - x1))
        poly = cv2.ellipse2Poly((int(mx), int(my)),
                                (int(length / 2), stickwidth),
                                int(angle), 0, 360, 1)
        color = POSE_COLORS[li % len(POSE_COLORS)]
        cv2.fillConvexPoly(canvas, poly, [int(c * 0.6) for c in color[::-1]])
    for ki in range(18):
        if not visible[ki]:
            continue
        x, y = kp2d[ki]
        cv2.circle(canvas, (int(x), int(y)), stickwidth,
                   POSE_COLORS[ki][::-1], thickness=-1)
    return canvas


# --------------------------------------------------------------------------
# Helpers: resampling, LTX frame counts, OpenPose JSON
# --------------------------------------------------------------------------

def resample(kp3d, src_fps, dst_fps):
    """Linear-interpolate keypoints in time."""
    if abs(src_fps - dst_fps) < 1e-6:
        return kp3d
    F = kp3d.shape[0]
    duration = (F - 1) / src_fps
    n_out = int(round(duration * dst_fps)) + 1
    t_out = np.clip(np.arange(n_out) * (src_fps / dst_fps), 0, F - 1)
    lo = np.floor(t_out).astype(int)
    hi = np.minimum(lo + 1, F - 1)
    w = (t_out - lo)[:, None, None]
    return kp3d[lo] * (1 - w) + kp3d[hi] * w


def snap_to_8n1(n):
    """Largest frame count <= n of form 8k+1 (LTX-Video requirement)."""
    if n < 9:
        return n
    return ((n - 1) // 8) * 8 + 1


def openpose_json_frame(kp2d, visible, width, height):
    flat = []
    for k in range(18):
        if visible[k]:
            flat += [float(kp2d[k, 0]), float(kp2d[k, 1]), 1.0]
        else:
            flat += [0.0, 0.0, 0.0]
    return {
        "version": 1.3,
        "canvas_width": width,
        "canvas_height": height,
        "people": [{"pose_keypoints_2d": flat,
                    "face_keypoints_2d": [], "hand_left_keypoints_2d": [],
                    "hand_right_keypoints_2d": []}],
    }


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def convert(bvh_path, out_dir, args):
    name = os.path.splitext(os.path.basename(bvh_path))[0]
    dest = os.path.join(out_dir, name)
    os.makedirs(dest, exist_ok=True)

    joints, motion, frame_time = parse_bvh(bvh_path)
    src_fps = 1.0 / frame_time
    pos, rot = forward_kinematics(joints, motion)
    kp3d = build_openpose_3d(joints, pos, rot)

    if args.start or args.end:
        kp3d = kp3d[args.start: args.end if args.end else None]

    kp3d = resample(kp3d, src_fps, args.fps)

    n = kp3d.shape[0]
    if args.ltx_frames:
        n = snap_to_8n1(min(n, args.max_frames or n))
    elif args.max_frames:
        n = min(n, args.max_frames)
    kp3d = kp3d[:n]

    kp2d, visible = project(kp3d, args.width, args.height,
                            yaw_deg=args.yaw, pitch_deg=args.pitch,
                            fov_deg=args.fov, margin=args.margin,
                            follow=args.follow)

    frames_dir = os.path.join(dest, "pose_frames")
    os.makedirs(frames_dir, exist_ok=True)
    writer = None
    if args.mp4:
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(os.path.join(dest, f"{name}_pose.mp4"),
                                 fourcc, args.fps, (args.width, args.height))
    json_frames = []
    for fi in range(n):
        img = draw_pose(kp2d[fi], visible[fi], args.width, args.height,
                        stickwidth=args.stickwidth)
        cv2.imwrite(os.path.join(frames_dir, f"{fi:05d}.png"), img)
        if writer is not None:
            writer.write(img)
        if args.json:
            json_frames.append(openpose_json_frame(kp2d[fi], visible[fi],
                                                   args.width, args.height))
    if writer is not None:
        writer.release()
    if args.json:
        with open(os.path.join(dest, f"{name}_openpose.json"), "w") as f:
            json.dump(json_frames, f)

    print(f"[{name}] {n} frames @ {args.fps}fps  ({args.width}x{args.height})"
          f"  src={src_fps:.0f}fps  ->  {frames_dir}")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", help="BVH file or folder of BVH files")
    p.add_argument("-o", "--out", default="pose_out", help="output directory")
    p.add_argument("--width", type=int, default=1280, help="output width (LTX: divisible by 32)")
    p.add_argument("--height", type=int, default=768, help="output height (LTX: divisible by 32)")
    p.add_argument("--fps", type=float, default=24, help="target fps (LTX default 24/25; source is 30)")
    p.add_argument("--yaw", type=float, default=0, help="camera yaw in degrees around the character")
    p.add_argument("--pitch", type=float, default=0, help="camera pitch in degrees")
    p.add_argument("--fov", type=float, default=40, help="camera vertical FOV")
    p.add_argument("--margin", type=float, default=0.82, help="frame fill fraction (smaller = more headroom)")
    p.add_argument("--follow", action="store_true",
                   help="camera tracks the character (cancels root travel); good for long walks/runs")
    p.add_argument("--stickwidth", type=int, default=4, help="limb thickness in px")
    p.add_argument("--start", type=int, default=0, help="start frame (source fps)")
    p.add_argument("--end", type=int, default=0, help="end frame (source fps, 0 = all)")
    p.add_argument("--max-frames", type=int, default=0, help="cap output frame count")
    p.add_argument("--ltx-frames", action="store_true",
                   help="trim frame count to 8n+1 as required by LTX-Video")
    p.add_argument("--mp4", action="store_true", help="also write a pose .mp4")
    p.add_argument("--json", action="store_true", help="also write OpenPose-format JSON keypoints")
    args = p.parse_args()

    if os.path.isdir(args.input):
        files = sorted(f for f in os.listdir(args.input) if f.lower().endswith(".bvh"))
        if not files:
            sys.exit(f"No .bvh files in {args.input}")
        for f in files:
            convert(os.path.join(args.input, f), args.out, args)
    else:
        convert(args.input, args.out, args)


if __name__ == "__main__":
    main()