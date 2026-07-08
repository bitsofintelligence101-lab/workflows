#!/usr/bin/env python3
"""
snapmogen2openpose_camControl.py  (v2 — cinematic camera)
==================================================
Convert SnapMoGen BVH mocap clips into OpenPose/DWPose-style pose control
sequences for ComfyUI (LTX-Video / LTX 2 pose conditioning, ControlNet, etc.)

Shot framing presets (subject is auto-tracked for close/medium):
  --close-shot     head & shoulders
  --medium-shot    head / shoulders / waist / hips
  --wide-shot      full body (default)

Camera motions (combinable, e.g. --close-shot --pan-left --dolly-in):
  --dolly-in --dolly-out      move camera toward / away from subject
  --dolly-tracking            camera travels with the subject (auto for
                              close/medium; forces tracking on wide shots)
  --pan-left --pan-right      rotate camera horizontally in place
  --tilt-up --tilt-down       rotate camera vertically in place
  --roll-cw --roll-ccw        rotate the image around the view axis
  --truck-left --truck-right  slide camera sideways
  --pedestal-up --pedestal-down  raise / lower camera
  --motion-scale S            scale intensity of all motions (default 1.0)

Examples:
  python snapmogen2openpose_camControl.py run.bvh -o poses --close-shot --pan-left
  python snapmogen2openpose_camControl.py clip.bvh -o poses --medium-shot --dolly-in --mp4
  python snapmogen2openpose_camControl.py clip.bvh -o poses --wide-shot --dolly-tracking --truck-right --motion-scale 1.5 --ltx-frames

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
    channels: list = field(default_factory=list)


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
            assert tok().upper() == "OFFSET"
            off = np.array([float(tok()), float(tok()), float(tok())])
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
    r = np.radians(np.atleast_1d(deg).astype(float))
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
    F = motion.shape[0]
    J = len(joints)
    pos = np.zeros((F, J, 3))
    rot = np.tile(np.eye(3), (F, J, 1, 1))

    ch_index = 0
    for jidx, joint in enumerate(joints):
        local_R = np.tile(np.eye(3), (F, 1, 1))
        local_T = np.tile(joint.offset, (F, 1))
        for ch in joint.channels:
            vals = motion[:, ch_index]
            ch_index += 1
            cl = ch.lower()
            if cl.endswith("rotation"):
                local_R = local_R @ _rot(cl[0], vals)
            elif cl.endswith("position"):
                # position channels replace the offset on that axis
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
    for i, j in enumerate(joints):
        if name.lower() in j.name.lower():
            return i
    raise KeyError(f"Joint '{name}' not found. Available: {[j.name for j in joints]}")


def build_openpose_3d(joints, pos, rot):
    idx = {k: find_joint(joints, v) for k, v in SNAP_MAP.items()}
    head_i = idx["head"]
    F = pos.shape[0]
    kp = np.zeros((F, 18, 3))

    head_R = rot[:, head_i]
    head_p = pos[:, head_i]
    fwd = head_R[:, :, 2]
    up = head_R[:, :, 1]
    right_of_char = -head_R[:, :, 0]

    neck_p = pos[:, idx["neck_base"]]
    head_len = np.linalg.norm(head_p - neck_p, axis=1, keepdims=True)
    head_len = np.clip(head_len, 6.0, 14.0)

    nose = head_p + up * head_len * 0.55 + fwd * head_len * 0.85
    r_eye = head_p + up * head_len * 0.80 + fwd * head_len * 0.72 + right_of_char * head_len * 0.30
    l_eye = head_p + up * head_len * 0.80 + fwd * head_len * 0.72 - right_of_char * head_len * 0.30
    r_ear = head_p + up * head_len * 0.65 + fwd * head_len * 0.10 + right_of_char * head_len * 0.62
    l_ear = head_p + up * head_len * 0.65 + fwd * head_len * 0.10 - right_of_char * head_len * 0.62

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
# Cinematic camera
# --------------------------------------------------------------------------

# keypoints that define each shot's framing region
SHOT_REGION = {
    "close": [0, 1, 2, 5, 14, 15, 16, 17],                  # head & shoulders
    "medium": [0, 1, 2, 5, 8, 11, 14, 15, 16, 17],          # head -> hips
    "wide": list(range(18)),                                # full body
}
# default frame-fill fraction per shot (overridable with --margin)
SHOT_MARGIN = {"close": 0.55, "medium": 0.65, "wide": 0.82}
# extra vertical headroom (fraction of region height) so the head isn't clipped
SHOT_HEADROOM = {"close": 0.35, "medium": 0.22, "wide": 0.10}


def _smooth(x, win):
    """Centered moving-average smoothing along axis 0. x: (F, ...)"""
    F = x.shape[0]
    if F < 3 or win < 3:
        return x
    win = min(win | 1, F if F % 2 == 1 else F - 1)  # odd, <= F
    kernel = np.hanning(win + 2)[1:-1]
    kernel /= kernel.sum()
    pad_lo = np.repeat(x[:1], win // 2, axis=0)
    pad_hi = np.repeat(x[-1:], win // 2, axis=0)
    xp = np.concatenate([pad_lo, x, pad_hi], axis=0)
    flat = xp.reshape(xp.shape[0], -1)
    out = np.stack([np.convolve(flat[:, c], kernel, mode="valid")
                    for c in range(flat.shape[1])], axis=1)
    return out.reshape(x.shape)


def _ease(F):
    """Smoothstep 0..1 over F frames."""
    if F <= 1:
        return np.zeros(F)
    s = np.linspace(0.0, 1.0, F)
    return s * s * (3 - 2 * s)


def project(kp3d, args, fps):
    """
    Project (F,18,3) -> pixel coords (F,18,2) + visibility, with shot
    framing presets and animated camera motions.
    """
    F = kp3d.shape[0]
    W, H = args.width, args.height
    shot = args.shot
    region = SHOT_REGION[shot]
    margin = args.margin if args.margin else SHOT_MARGIN[shot]
    tracking = args.tracking or shot in ("close", "medium")

    yaw = math.radians(args.yaw)
    pitch = math.radians(args.pitch)
    Ry = np.array([[math.cos(yaw), 0, math.sin(yaw)],
                   [0, 1, 0],
                   [-math.sin(yaw), 0, math.cos(yaw)]])
    Rx = np.array([[1, 0, 0],
                   [0, math.cos(pitch), -math.sin(pitch)],
                   [0, math.sin(pitch), math.cos(pitch)]])
    Rcam = Rx @ Ry

    pts = np.einsum("ij,fkj->fki", Rcam, kp3d)      # world -> camera-aligned
    sub = pts[:, region]                            # (F, R, 3)

    tan_half = math.tan(math.radians(args.fov) / 2)
    aspect = W / H

    # per-frame framing region
    mn = sub.min(1)                                 # (F,3)
    mx = sub.max(1)
    headroom = SHOT_HEADROOM[shot] * (mx[:, 1] - mn[:, 1] + 1e-6)
    mx = mx.copy()
    mx[:, 1] += headroom                            # extend region upward
    center_f = 0.5 * (mn + mx)
    span_x = mx[:, 0] - mn[:, 0]
    span_y = mx[:, 1] - mn[:, 1]
    dist_f = np.maximum(span_x / (2 * margin * tan_half * aspect),
                        span_y / (2 * margin * tan_half))
    dist_f = dist_f + 0.5 * (mx[:, 2] - mn[:, 2])

    if tracking:
        aim = _smooth(center_f, int(round(fps * 0.5)))
        # fixed shot size: generous percentile so the subject stays inside
        dist = float(np.percentile(dist_f, 90))
    else:
        g_mn = mn.min(0)
        g_mx = mx.max(0)
        aim = np.tile(0.5 * (g_mn + g_mx), (F, 1))
        gspan_x = g_mx[0] - g_mn[0]
        gspan_y = g_mx[1] - g_mn[1]
        dist = max(gspan_x / (2 * margin * tan_half * aspect),
                   gspan_y / (2 * margin * tan_half)) + 0.5 * (g_mx[2] - g_mn[2])
    dist = max(dist, 1e-3)

    # ---- animated camera motions -------------------------------------
    ms = args.motion_scale
    e = _ease(F)                     # 0 -> 1
    ec = e - 0.5                     # -0.5 -> +0.5 (centered sweeps)

    # dolly: exponential distance scale (in: 1.25 -> 0.8, out: reverse)
    dolly = np.ones(F)
    if args.dolly_in:
        # start far (1.25^s), end near (0.8^s)
        dolly *= (1.25 ** ms) * (0.64 ** (e * ms))
    if args.dolly_out:
        dolly *= (0.8 ** ms) * ((1.5625) ** (e * ms))
    dist_anim = dist * dolly

    # pan / tilt: rotate view in place (degrees, centered sweep)
    pan = np.zeros(F)
    tilt = np.zeros(F)
    if args.pan_left:
        pan += np.radians(24.0 * ms) * ec       # subject drifts right
    if args.pan_right:
        pan -= np.radians(24.0 * ms) * ec
    if args.tilt_up:
        tilt += np.radians(16.0 * ms) * ec
    if args.tilt_down:
        tilt -= np.radians(16.0 * ms) * ec

    # truck / pedestal: translate camera, orientation fixed
    half_w_world = dist * tan_half * aspect
    half_h_world = dist * tan_half
    truck = np.zeros(F)
    ped = np.zeros(F)
    if args.truck_left:
        truck -= 1.0 * half_w_world * ms * ec   # camera moves left
    if args.truck_right:
        truck += 1.0 * half_w_world * ms * ec
    if args.pedestal_up:
        ped += 0.9 * half_h_world * ms * ec
    if args.pedestal_down:
        ped -= 0.9 * half_h_world * ms * ec

    # roll (applied in 2D after projection)
    roll = np.zeros(F)
    if args.roll_cw:
        roll += np.radians(14.0 * ms) * ec
    if args.roll_ccw:
        roll -= np.radians(14.0 * ms) * ec

    # ---- per-frame projection ------------------------------------------
    cam = aim.copy()
    cam[:, 0] += truck
    cam[:, 1] += ped
    cam[:, 2] += dist_anim

    rel = pts - cam[:, None, :]

    # pan / tilt rotate the view direction (camera stays in place)
    cp, sp = np.cos(pan), np.sin(pan)
    x, y, z = rel[:, :, 0], rel[:, :, 1], rel[:, :, 2]
    x2 = cp[:, None] * x + sp[:, None] * z
    z2 = -sp[:, None] * x + cp[:, None] * z
    ct, st = np.cos(tilt), np.sin(tilt)
    y2 = ct[:, None] * y - st[:, None] * z2
    z3 = st[:, None] * y + ct[:, None] * z2

    depth = -z3
    visible = depth > 1e-4
    depth = np.maximum(depth, 1e-6)
    f_norm = 0.5 / tan_half
    u = (x2 / depth) * f_norm / aspect
    v = (y2 / depth) * f_norm

    px = (u + 0.5) * W
    py = (0.5 - v) * H

    # roll: rotate pixels about canvas center (+ = clockwise on screen)
    cr, sr = np.cos(roll), np.sin(roll)
    dx = px - W / 2
    dy = py - H / 2
    px = W / 2 + cr[:, None] * dx - sr[:, None] * dy
    py = H / 2 + sr[:, None] * dx + cr[:, None] * dy

    kp2d = np.stack([px, py], axis=-1)
    # sanity clamp so extreme off-screen values can't overflow cv2 ints
    kp2d = np.clip(kp2d, -4 * max(W, H), 5 * max(W, H))
    return kp2d, visible


# --------------------------------------------------------------------------
# DWPose / OpenPose rendering
# --------------------------------------------------------------------------

LIMB_SEQ = [
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
# Helpers
# --------------------------------------------------------------------------

def resample(kp3d, src_fps, dst_fps):
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

    total = kp3d.shape[0]
    if args.start or args.end:
        s = args.start
        e = args.end if args.end else total
        if s >= total:
            print(f"[{name}] WARNING: file has only {total} frames "
                  f"(~{total / src_fps:.1f}s) but --start {s} was requested.\n"
                  f"    The renamed_bvhs files appear to be pre-trimmed to the "
                  f"captioned segment\n"
                  f"    (the #start#end in caption IDs refers to the original "
                  f"take, not this file).\n"
                  f"    Ignoring --start/--end and converting the whole file.")
            s, e = 0, total
        elif e > total:
            print(f"[{name}] WARNING: --end {e} exceeds file length {total}; "
                  f"clamping to {total}.")
            e = total
        kp3d = kp3d[s:e]
    if kp3d.shape[0] == 0:
        sys.exit(f"[{name}] No frames to convert (file has {total} frames).")

    kp3d = resample(kp3d, src_fps, args.fps)

    n = kp3d.shape[0]
    if args.ltx_frames:
        n = snap_to_8n1(min(n, args.max_frames or n))
    elif args.max_frames:
        n = min(n, args.max_frames)
    kp3d = kp3d[:n]

    kp2d, visible = project(kp3d, args, fps=args.fps)

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

    moves = [m for m in ("dolly_in", "dolly_out", "pan_left", "pan_right",
                         "tilt_up", "tilt_down", "roll_cw", "roll_ccw",
                         "truck_left", "truck_right", "pedestal_up",
                         "pedestal_down") if getattr(args, m)]
    print(f"[{name}] {n} frames @ {args.fps}fps  ({args.width}x{args.height})"
          f"  shot={args.shot}{' tracking' if (args.tracking or args.shot != 'wide') else ''}"
          f"  moves={'+'.join(moves) if moves else 'none'}  ->  {frames_dir}")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", help="BVH file or folder of BVH files")
    p.add_argument("-o", "--out", default="pose_out", help="output directory")
    p.add_argument("--width", type=int, default=1280, help="output width (LTX: divisible by 32)")
    p.add_argument("--height", type=int, default=768, help="output height (LTX: divisible by 32)")
    p.add_argument("--fps", type=float, default=24, help="target fps (source is 30)")

    shot = p.add_mutually_exclusive_group()
    shot.add_argument("--close-shot", dest="shot", action="store_const",
                      const="close", help="frame head & shoulders")
    shot.add_argument("--medium-shot", dest="shot", action="store_const",
                      const="medium", help="frame head to hips")
    shot.add_argument("--wide-shot", dest="shot", action="store_const",
                      const="wide", help="frame full body (default)")
    p.set_defaults(shot="wide")

    cam = p.add_argument_group("camera motions (combinable)")
    for flag, hlp in [
        ("--dolly-in", "camera moves toward subject over the clip"),
        ("--dolly-out", "camera moves away from subject"),
        ("--pan-left", "camera rotates left in place"),
        ("--pan-right", "camera rotates right in place"),
        ("--tilt-up", "camera rotates upward in place"),
        ("--tilt-down", "camera rotates downward in place"),
        ("--roll-cw", "image rolls clockwise"),
        ("--roll-ccw", "image rolls counter-clockwise"),
        ("--truck-left", "camera slides left"),
        ("--truck-right", "camera slides right"),
        ("--pedestal-up", "camera rises vertically"),
        ("--pedestal-down", "camera lowers vertically"),
    ]:
        cam.add_argument(flag, action="store_true", help=hlp)
    cam.add_argument("--dolly-tracking", dest="tracking", action="store_true",
                     help="camera travels with the subject (auto for close/medium shots)")
    cam.add_argument("--follow", dest="tracking", action="store_true",
                     help=argparse.SUPPRESS)  # v1 alias
    cam.add_argument("--motion-scale", type=float, default=1.0,
                     help="intensity multiplier for all camera motions")

    p.add_argument("--yaw", type=float, default=0, help="base camera yaw (deg)")
    p.add_argument("--pitch", type=float, default=0, help="base camera pitch (deg)")
    p.add_argument("--fov", type=float, default=40, help="camera vertical FOV")
    p.add_argument("--margin", type=float, default=0,
                   help="frame fill fraction; 0 = per-shot default")
    p.add_argument("--stickwidth", type=int, default=4, help="limb thickness in px")
    p.add_argument("--start", type=int, default=0, help="start frame (source fps)")
    p.add_argument("--end", type=int, default=0, help="end frame (source fps, 0 = all)")
    p.add_argument("--max-frames", type=int, default=0, help="cap output frame count")
    p.add_argument("--ltx-frames", action="store_true",
                   help="trim frame count to 8n+1 as required by LTX-Video")
    p.add_argument("--mp4", action="store_true", help="also write a pose .mp4")
    p.add_argument("--json", action="store_true", help="also write OpenPose JSON keypoints")
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