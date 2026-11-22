#!/usr/bin/env python3
"""Rebuild Kiki animated WebPs with eased in-between frames.

The hand-drawn sprites are only a few key poses each, so playing them back
directly hard-cuts between very different poses (sitting -> licking paw ->
waving). This script reads the key-pose WebPs from `assets/kiki/` (source of
truth) and writes smoother versions to `frontend/public/kiki/` by inserting
transition frames between consecutive key poses (including the loop seam):
a short squash-and-stretch beat for big pose changes (no ghosting), a brief
dissolve only for near-identical poses. Hold durations of the key poses are preserved.

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


def squash(a: np.ndarray, sx: float, sy: float) -> np.ndarray:
    """Scale a premultiplied frame around its bottom-centre (the cat's contact point)."""
    h, w = a.shape[:2]
    img = Image.fromarray((np.clip(a, 0, 1) * 255 + 0.5).astype(np.uint8), "RGBA")
    nw, nh = max(1, round(w * sx)), max(1, round(h * sy))
    img = img.resize((nw, nh), Image.BICUBIC)
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    canvas.paste(img, ((w - nw) // 2, h - nh))
    return np.asarray(canvas, dtype=np.float32) / 255.0


# Squash & stretch keyframes used to hide a pose change (sx, sy, ms).
# Cross-dissolving two *different* poses leaves a ghost, so big changes are cut
# on the beat of a tiny body squash: anticipate (press down) -> cut -> overshoot -> settle.
ANTICIPATE = [(1.015, 0.975, 70), (1.035, 0.94, 80)]
OVERSHOOT = [(0.985, 1.035, 80), (1.005, 0.99, 70)]

QUICK_HOLD_MS = 150  # blink half-frames etc.: plain cut, they are meant to be fast
SMALL_DIFF = 0.07  # nearly identical poses: a short dissolve shows no ghost


def build(name: str) -> None:
    src = SRC / f"{name}.webp"
    frames = load_frames(src)
    holds = [max(1, round(d * HOLD_SCALE[name])) for d in read_durations(src)]
    out_imgs: list[Image.Image] = []
    out_durs: list[int] = []

    def emit(arr: np.ndarray, ms: int) -> None:
        out_imgs.append(to_image(arr))
        out_durs.append(ms)

    n = len(frames)
    for i in range(n):
        cur, nxt = frames[i], frames[(i + 1) % n]
        diff = float(np.abs(cur - nxt).mean())
        if holds[i] < QUICK_HOLD_MS or diff < 0.01:
            emit(cur, holds[i])
        elif diff < SMALL_DIFF:
            emit(cur, holds[i])
            for s in (1, 2):
                t = smoothstep(s / 3)
                emit(cur * (1 - t) + nxt * t, STEP_MS)
        else:
            emit(cur, holds[i])
            for sx, sy, ms in ANTICIPATE:
                emit(squash(cur, sx, sy), ms)
            for sx, sy, ms in OVERSHOOT:
                emit(squash(nxt, sx, sy), ms)
    DST.mkdir(parents=True, exist_ok=True)
    dst = DST / f"{name}.webp"
    out_imgs[0].save(
        dst,
        save_all=True,
        append_images=out_imgs[1:],
        duration=out_durs,
        loop=0,
        quality=75,
        alpha_quality=85,
        method=4,
    )
    print(f"{name}: {n} key poses -> {len(out_imgs)} frames, {sum(out_durs)}ms loop, {dst.stat().st_size // 1024}KB")


if __name__ == "__main__":
    for p in POSES:
        build(p)
