#!/usr/bin/env python3
"""
search_captions.py — find SnapMoGen clips by text description.

Usage:
  python search_captions.py all_caption_clean.json slip ice
  python search_captions.py all_caption_clean.json "loses footing"
  python search_captions.py all_caption_clean.json slip --any -n 30
  python search_captions.py all_caption_clean.json slip --bvh-dir renamed_bvhs

By default ALL terms must appear in a caption (case-insensitive substring).
Use --any to match captions containing ANY term.
--bvh-dir prints the full path to each matching .bvh and flags missing files.
"""

import argparse
import json
import os
import sys


def iter_captions(data):
    """Yield (clip_id, caption) regardless of JSON layout."""
    if isinstance(data, dict):
        for key, val in data.items():
            if isinstance(val, str):
                yield key, val
            elif isinstance(val, list):
                for v in val:
                    if isinstance(v, str):
                        yield key, v
                    elif isinstance(v, dict):
                        for tkey in ("caption", "text", "description"):
                            if tkey in v:
                                yield key, str(v[tkey])
                                break
            elif isinstance(val, dict):
                # SnapMoGen layout: {"clip_id": {"gpt": [...], "manual": [...]}}
                # Accept any string / list-of-strings values regardless of key.
                for inner in val.values():
                    if isinstance(inner, str):
                        yield key, inner
                    elif isinstance(inner, list):
                        for c in inner:
                            if isinstance(c, str):
                                yield key, c
    elif isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            cid = None
            for ik in ("id", "clip_id", "name", "motion_id", "file"):
                if ik in item:
                    cid = str(item[ik])
                    break
            cap = None
            for tk in ("caption", "text", "description"):
                if tk in item:
                    cap = str(item[tk])
                    break
            if cid and cap:
                yield cid, cap


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("json_file", help="path to all_caption_clean.json")
    p.add_argument("terms", nargs="+", help="search terms (substring, case-insensitive)")
    p.add_argument("--any", action="store_true",
                   help="match captions containing ANY term (default: ALL terms)")
    p.add_argument("-n", "--max", type=int, default=20, help="max clips to show")
    p.add_argument("--bvh-dir", default=None,
                   help="folder with .bvh files; prints full paths and checks existence")
    args = p.parse_args()

    with open(args.json_file, "r") as f:
        data = json.load(f)

    terms = [t.lower() for t in args.terms]
    hits = {}  # clip_id -> first matching caption
    total_captions = 0
    for cid, cap in iter_captions(data):
        total_captions += 1
        low = cap.lower()
        ok = any(t in low for t in terms) if args.any else all(t in low for t in terms)
        if ok and cid not in hits:
            hits[cid] = cap

    if total_captions == 0:
        sys.exit("Could not extract any captions — JSON layout unrecognized. "
                 "Open the file and check its structure.")

    print(f"{len(hits)} matching clips (searched {total_captions} captions)\n")
    for i, (cid, cap) in enumerate(sorted(hits.items())):
        if i >= args.max:
            print(f"... and {len(hits) - args.max} more (raise with -n)")
            break
        # IDs look like "ep1_00000#0#281" = clip ep1_00000, frames 0..281
        base, start, end = cid, None, None
        parts = cid.split("#")
        if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
            base, start, end = parts[0], int(parts[1]), int(parts[2])

        print(cid)
        if args.bvh_dir:
            # try exact-id filename first, then the base clip name
            for cand in (cid + ".bvh", base + ".bvh"):
                path = os.path.join(args.bvh_dir, cand)
                if os.path.exists(path):
                    cmd = f"python snapmogen2openpose.py \"{path}\" -o poses --ltx-frames --mp4"
                    if cand == base + ".bvh" and start is not None:
                        cmd += f" --start {start} --end {end}"
                    print(f"    {cmd}")
                    break
            else:
                print(f"    [BVH file: {base}.bvh in {args.bvh_dir}] use with snapmogen2openpose.py")
        elif start is not None:
            print(f"    file: {base}.bvh   frames {start}-{end}  "
                  f"(use --start {start} --end {end})")
        #caption snippet
        print(f"    {cap[:320]}{'...' if len(cap) > 320 else ''}\n")


if __name__ == "__main__":
    main()