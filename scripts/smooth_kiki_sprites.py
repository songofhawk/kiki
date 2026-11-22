#!/usr/bin/env python3
"""Rebuild Kiki animated WebPs with eased in-between frames.

The hand-drawn sprites are only a few key poses each, so playing them back
directly hard-cuts between very different poses (sitting -> licking paw ->
waving). This script reads the key-pose WebPs from `assets/kiki/` (source of
truth) and writes smoother versions to `frontend/public/kiki/` by inserting
eased cross-dissolve frames between consecutive key poses (including the
loop seam). Hold durations of the key poses are preserved.

Usage: python3 scripts/smooth_kiki_sprites.py
"""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "assets" / "kiki"
DST = ROOT / "frontend" / "public" / "kiki"

OUT_SIZE = 288  # displayed at <=144px CSS, so 288px is plenty even at 2x
STEP_MS = 60  # duration of each in-between frame
MIN_TRANSITION_MS = 240
MAX_TRANSITION_MS = 640

# Poses to smooth. working/delivering are intentionally fast cycles and stay as-is.
POSES = ["idle", "idle_lazy", "idle_playful", "thinking", "waiting_input"]

# Slow the held key poses slightly so the cat feels less twitchy.
HOLD_SCALE = {
    "idle": 1.0,
    "idle_lazy": 1.15,
    "idle_playful": 1.35,
    "thinking": 1.2,
    "waiting_input": 1.3,
}


def read_durations(path: Path) -> list[int]:
    b = path.read_bytes()
    i, out = 12, []
    while i < len(b):
        tag = b[i : i + 4]
        n = struct.unpack("<I", b[i + 4 : i + 8])[0]
        if tag == b"ANMF":
            out.append(int.from_bytes(b[i + 20 : i + 23], "little"))
        i += 8 + n + (n & 1)
    return out


def load_frames(path: Path) -> list[np.ndarray]:
    im = Image.open(path)
    frames = []
    for i in range(im.n_frames):
        im.seek(i)
        f = im.convert("RGBA").resize((OUT_SIZE, OUT_SIZE), Image.LANCZOS)
        a = np.asarray(f, dtype=np.float32) / 255.0
        a[..., :3] *= a[..., 3:4]  # premultiply so transparent edges don't fringe
        frames.append(a)
    return frames


def to_image(a: np.ndarray) -> Image.Image:
    a = a.copy()
    alpha = a[..., 3:4]
    rgb = np.where(alpha > 1e-4, a[..., :3] / np.maximum(alpha, 1e-4), 0.0)
    out = np.concatenate([rgb, alpha], axis=-1)
    return Image.fromarray((np.clip(out, 0, 1) * 255 + 0.5).astype(np.uint8), "RGBA")


def smoothstep(t: float) -> float:
    return t * t * (3 - 2 * t)


def build(name: str) -> None:
    src = SRC / f"{name}.webp"
    frames = load_frames(src)
    holds = [max(1, round(d * HOLD_SCALE[name])) for d in read_durations(src)]
    out_imgs: list[Image.Image] = []
    out_durs: list[int] = []
    n = len(frames)
    for i in range(n):
        cur, nxt = frames[i], frames[(i + 1) % n]
        out_imgs.append(to_image(cur))
        out_durs.append(holds[i])
        diff = float(np.abs(cur - nxt).mean())
        trans = int(np.clip(180 + diff * 5000, MIN_TRANSITION_MS, MAX_TRANSITION_MS))
        # Quick key poses (e.g. blink half-frames) must stay quick: never dissolve
        # for longer than the pose is held.
        trans = min(trans, holds[i])
        steps = max(0, round(trans / STEP_MS) - 1)
        for s in range(1, steps + 1):
            t = smoothstep(s / (steps + 1))
            out_imgs.append(to_image(cur * (1 - t) + nxt * t))
            out_durs.append(STEP_MS)
    DST.mkdir(parents=True, exist_ok=True)
    dst = DST / f"{name}.webp"
    out_imgs[0].save(
        dst,
        save_all=True,
        append_images=out_imgs[1:],
        duration=out_durs,
        loop=0,
        quality=70,
        alpha_quality=80,
        method=4,
        minimize_size=False,
    )
    print(f"{name}: {n} key poses -> {len(out_imgs)} frames, {sum(out_durs)}ms loop, {dst.stat().st_size // 1024}KB")


if __name__ == "__main__":
    for p in POSES:
        build(p)
