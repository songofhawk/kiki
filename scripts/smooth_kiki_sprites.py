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

import math
import struct
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "assets" / "kiki"
DST = ROOT / "frontend" / "public" / "kiki"

OUT_SIZE = 256  # displayed at <=144px CSS, so 288px is plenty even at 2x
STEP_MS = 60  # duration of each in-between frame

# Poses to smooth. working/delivering are intentionally fast cycles and stay as-is.
POSES = ["idle", "idle_lazy", "idle_playful", "thinking", "waiting_input"]

# Slow the held key poses so the cat feels less twitchy.
HOLD_SCALE = {
    "idle": 1.0,
    "idle_lazy": 1.15,
    "idle_playful": 1.35,
    "thinking": 1.2,
    "waiting_input": 1.3,
}

# Every real pose must be held for a while before the next movement starts: the cat
# should not be moving all the time. Quick frames (blink halves) are exempt.
MIN_HOLD_MS = {
    "idle": 1600,
    "idle_lazy": 2600,
    "idle_playful": 2000,
    "thinking": 2200,
    "waiting_input": 1800,
}

META_TS = ROOT / "frontend" / "src" / "components" / "kikiSpriteMeta.ts"
LOOP_MS: dict[str, int] = {}


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


# Squash & stretch pose-change beat. Cross-dissolving two *different* poses leaves a
# ghost, so big changes are hidden behind a slow, eased body motion instead:
# settle down (anticipation) -> brief dissolve at the lowest point -> spring back up.
ANTICIPATE_MS = 480  # slow press-down before the pose changes
MID_DISSOLVE_STEPS = 2  # very short, only while the body is squashed low
RECOVER_MS = 620  # damped spring back to normal after the new pose appears
MAX_SQUASH = 0.075  # how far the body presses down (fraction of height)
STEP_MS_MOTION = 60


def _squash_scale(depth: float) -> tuple[float, float]:
    """depth>0 = pressed down & wider, depth<0 = stretched up & narrower."""
    return 1.0 + depth * MAX_SQUASH * 0.55, 1.0 - depth * MAX_SQUASH


def anticipate_curve() -> list[tuple[float, float]]:
    n = round(ANTICIPATE_MS / STEP_MS_MOTION)
    return [_squash_scale(smoothstep(k / n)) for k in range(1, n + 1)]


def recover_curve() -> list[tuple[float, float]]:
    n = round(RECOVER_MS / STEP_MS_MOTION)
    out = []
    for k in range(n):
        t = k / n
        depth = math.exp(-3.2 * t) * math.cos(5.0 * t)  # 1 -> overshoot (<0) -> settle
        out.append(_squash_scale(depth))
    return out


QUICK_HOLD_MS = 150  # blink half-frames etc.: plain cut, they are meant to be fast
SMALL_DIFF = 0.07  # nearly identical poses: a short dissolve shows no ghost


def build(name: str) -> None:
    src = SRC / f"{name}.webp"
    frames = load_frames(src)
    holds = []
    for d in read_durations(src):
        h = max(1, round(d * HOLD_SCALE[name]))
        if d >= QUICK_HOLD_MS:
            h = max(h, MIN_HOLD_MS[name])
        holds.append(h)
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
            for sx, sy in anticipate_curve():
                emit(squash(cur, sx, sy), STEP_MS_MOTION)
            low_x, low_y = _squash_scale(1.0)
            low_cur, low_nxt = squash(cur, low_x, low_y), squash(nxt, low_x, low_y)
            for k in range(1, MID_DISSOLVE_STEPS + 1):
                t = k / (MID_DISSOLVE_STEPS + 1)
                emit(low_cur * (1 - t) + low_nxt * t, STEP_MS_MOTION)
            for sx, sy in recover_curve():
                emit(squash(nxt, sx, sy), STEP_MS_MOTION)
    DST.mkdir(parents=True, exist_ok=True)
    dst = DST / f"{name}.webp"
    out_imgs[0].save(
        dst,
        save_all=True,
        append_images=out_imgs[1:],
        duration=out_durs,
        loop=0,
        quality=70,
        alpha_quality=85,
        method=4,
    )
    LOOP_MS[name] = sum(out_durs)
    print(f"{name}: {n} key poses -> {len(out_imgs)} frames, {sum(out_durs)}ms loop, {dst.stat().st_size // 1024}KB")


if __name__ == "__main__":
    for p in POSES:
        build(p)
    body = "\n".join(f"  {k}: {v}," for k, v in LOOP_MS.items())
    META_TS.write_text(
        "// Generated by scripts/smooth_kiki_sprites.py - do not edit by hand.\n"
        "/** One full loop of each smoothed animated WebP, in ms. */\n"
        f"export const KIKI_LOOP_MS = {{\n{body}\n}} as const;\n"
    )
