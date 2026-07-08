# SnapMoGen BVH → OpenPose/DWPose for ComfyUI + LTX

Converts [SnapMoGen](https://github.com/snap-research/SnapMoGen) mocap BVH clips into DWPose-style pose control sequences
(PNG frames, optional MP4 and OpenPose JSON) that drive LTX-Video / LTX 2
pose conditioning or any OpenPose ControlNet in ComfyUI.

## Install
```
pip install numpy opencv-python
```

## Get the data
1. Download `renamed_bvhs.zip` and `all_caption_clean.json` from
   https://huggingface.co/datasets/Ericguo5513/SnapMoGen/tree/main and unzip.

2. Find a motion by searching `all_caption_clean.json` for keywords using the
 `search_captions.py` script, e.g. search for running jogging:
   ```
   python search_captions.py all_caption_clean.json running jogging --bvh-dir renamed_bvhs 
   ```
   You'll get 1 or many results. The key (e.g. `ep1_00042`) matches the BVH filename. Use That to generate the pose control sequence with `snapmogen2openpose.py`.

## Convert
```
# single clip, LTX-friendly defaults (24 fps, 1280x768, 8n+1 frames)
python snapmogen2openpose.py renamed_bvhs/renamed_bvhs/ep1_00003.bvh -o poses --ltx-frames --mp4 --follow

#single clip, clip to specific frame range (some mocap data is minutes long so clip to section you want 24frame per second)
python snapmogen2openpose.py renamed_bvhs/renamed_bvhs/ep1_00003.bvh -o poses --ltx-frames --mp4 --follow --start 4300 --end 4670

# whole folder
python snapmogen2openpose.py renamed_bvhs/ -o poses --fps 25 --ltx-frames

# character travels far (walking/running)? keep it centered:
python snapmogen2openpose.py clip.bvh -o poses --follow

# different camera angle / framing
python snapmogen2openpose.py clip.bvh -o poses --yaw 30 --pitch -10 --margin 0.7
```

Key flags:
- `--width/--height`  output size (keep divisible by 32 for LTX; default 1280x768)
- `--fps`             resample from source 30 fps (LTX likes 24/25)
- `--ltx-frames`      trim frame count to 8n+1 (LTX-Video requirement)
- `--follow`          camera tracks the pelvis (cancels root travel)
- `--yaw/--pitch/--fov/--margin`  camera setup; camera is static and auto-framed
- `--json`            also write OpenPose-format keypoint JSON per frame
- `--mp4`             also write a pose preview/control video
- `--start/--end`     trim in source frames

## In ComfyUI
1. **Load the pose sequence**: VHS "Load Images (Path)" pointed at
   `poses/<clip>/pose_frames/`, or "Load Video" on the MP4.
2. **Feed as pose control**: the frames are already DWPose-render format
   (black bg, standard OpenPose limb colors), so skip the DWPose estimator
   node entirely and plug the image batch straight into your pose control
   input — e.g. the LTXV pose/control conditioning (IC-LoRA pose) for LTX 2,
   or an OpenPose ControlNet apply node.
3. Match your LTX generation width/height/frame count to what you exported
   (see workflow in this repository).

## Notes
- Skeleton mapping: SnapMoGen's 24 joints → OpenPose BODY_18. Nose, eyes and
  ears don't exist in mocap, so they're synthesized from the head joint's
  orientation — they turn correctly when the head turns.
- OpenPose left/right convention is handled (character's right arm renders
  on image-left when facing camera).
- Motions face +Z at start after SnapMoGen's canonicalization; use `--yaw`
  to orbit the camera if a clip starts facing an odd direction.