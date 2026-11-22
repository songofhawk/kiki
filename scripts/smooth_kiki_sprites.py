#!/usr/bin/env python3
"""Build frame-by-frame animated Kiki WebPs using real hand-drawn in-betweens.

Source keyframes live in `assets/kiki/*.webp` (transparent RGBA).
Generated in-between frames live in `assets/kiki/interp/gen/*.jpg` (pure white
background). This script:
  1. Flood-fills the white background to clean anti-aliased transparency (protecting
     interior cream/white fur).
  2. Aligns each generated frame's scale and bottom-center ground anchor smoothly
     between its start and end keyframes so the cat never jumps or resizes.
  3. Assembles `frontend/public/kiki/*.webp` with natural holds on key poses and
     crisp frame-by-frame transitions (zero ghosting, zero artificial squash),
     and writes `frontend/src/components/kikiSpriteMeta.ts`.

Usage: python3 scripts/smooth_kiki_sprites.py
"""
from __future__ import annotations

from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "assets" / "kiki"
GEN = SRC / "interp" / "gen"
DST = ROOT / "frontend" / "public" / "kiki"
META_TS = ROOT / "frontend" / "src" / "components" / "kikiSpriteMeta.ts"

OUT_SIZE = 320  # rendered at <=144px CSS (2x retina crisp)


def load_keyframe(webp_name: str, frame_idx: int) -> Image.Image:
    im = Image.open(SRC / f"{webp_name}.webp")
    im.seek(frame_idx)
    return im.convert("RGBA").resize((OUT_SIZE, OUT_SIZE), Image.LANCZOS)


def bbox_metrics(im: Image.Image) -> tuple[float, float, float]:
    """Return (center_x, bottom_y, height) of non-transparent pixels in [0..OUT_SIZE]."""
    alpha = np.asarray(im)[..., 3]
    ys, xs = np.where(alpha > 20)
    if len(ys) == 0:
        return OUT_SIZE / 2, OUT_SIZE * 0.9, OUT_SIZE * 0.7
    return (float(xs.min() + xs.max()) / 2.0, float(ys.max()), float(ys.max() - ys.min()))


def cutout_white_bg(path: Path) -> Image.Image:
    """Flood-fill remove outer white background with clean anti-aliased edges."""
    rgb = np.asarray(Image.open(path).convert("RGB"), dtype=np.uint8)
    h, w = rgb.shape[:2]

    # Pixel is "near white background" if bright and low saturation
    r, g, b = rgb[..., 0].astype(np.int16), rgb[..., 1].astype(np.int16), rgb[..., 2].astype(np.int16)
    lum = (r + g + b) / 3.0
    chroma = np.maximum(np.maximum(r, g), b) - np.minimum(np.minimum(r, g), b)
    is_bg_cand = (lum >= 232) & (chroma <= 22)

    visited = np.zeros((h, w), dtype=bool)
    q: deque[tuple[int, int]] = deque()
    for x in range(w):
        for y in (0, h - 1):
            if is_bg_cand[y, x] and not visited[y, x]:
                visited[y, x] = True
                q.append((y, x))
    for y in range(h):
        for x in (0, w - 1):
            if is_bg_cand[y, x] and not visited[y, x]:
                visited[y, x] = True
                q.append((y, x))

    while q:
        cy, cx = q.popleft()
        for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
            if 0 <= ny < h and 0 <= nx < w and not visited[ny, nx] and is_bg_cand[ny, nx]:
                visited[ny, nx] = True
                q.append((ny, nx))

    alpha = np.where(visited, 0.0, 255.0).astype(np.float32)

    # Soften 2-pixel fringe bordering background to eliminate white halo
    fringe = np.zeros((h, w), dtype=bool)
    for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)):
        shifted = np.roll(np.roll(visited, dy, axis=0), dx, axis=1)
        fringe |= shifted & (~visited)
    fringe2 = fringe.copy()
    for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        shifted = np.roll(np.roll(fringe, dy, axis=0), dx, axis=1)
        fringe2 |= shifted & (~visited)

    # On fringe pixels that are bright & desaturated, compute partial alpha & de-matte white
    f_rgb = rgb.astype(np.float32)
    bright_fringe = fringe2 & (lum > 180) & (chroma < 38)
    t_alpha = np.clip((248.0 - lum) / 68.0, 0.0, 1.0)
    alpha = np.where(bright_fringe, t_alpha * 255.0, alpha)

    # De-fringe RGB against white background: C_fg = (C - (1-a)*255) / a
    a_norm = np.clip(alpha / 255.0, 0.0, 1.0)[..., None]
    de_rgb = np.where(
        (bright_fringe[..., None]) & (a_norm > 0.05),
        np.clip((f_rgb - (1.0 - a_norm) * 255.0) / np.maximum(a_norm, 0.05), 0.0, 255.0),
        f_rgb,
    )
    rgba = np.concatenate([de_rgb, alpha[..., None]], axis=-1).astype(np.uint8)
    return Image.fromarray(rgba, "RGBA")


def align_gen_frame(
    gen_name: str,
    start_kf: Image.Image,
    end_kf: Image.Image,
    t: float,
) -> Image.Image:
    """Cut out `gen_name` and align its scale & bottom-center anchor between start_kf and end_kf."""
    cut = cutout_white_bg(GEN / f"{gen_name}.jpg")
    # Crop tight around non-transparent content
    alpha = np.asarray(cut)[..., 3]
    ys, xs = np.where(alpha > 20)
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()) + 1, int(ys.min()), int(ys.max()) + 1
    cropped = cut.crop((x0, y0, x1, y1))

    scx, sby, sh = bbox_metrics(start_kf)
    ecx, eby, eh = bbox_metrics(end_kf)
    target_cx = scx * (1 - t) + ecx * t
    target_by = sby * (1 - t) + eby * t
    target_h = sh * (1 - t) + eh * t

    scale = target_h / max(1, cropped.height)
    nw = max(1, round(cropped.width * scale))
    nh = max(1, round(cropped.height * scale))
    # Clamp if wider than canvas
    if nw > OUT_SIZE - 8:
        clamp = (OUT_SIZE - 8) / nw
        nw = round(nw * clamp)
        nh = round(nh * clamp)

    resized = cropped.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGBA", (OUT_SIZE, OUT_SIZE), (0, 0, 0, 0))
    px = round(target_cx - nw / 2.0)
    py = round(target_by - nh)
    px = max(0, min(OUT_SIZE - nw, px))
    py = max(0, min(OUT_SIZE - nh, py))
    canvas.paste(resized, (px, py), resized)
    return canvas


def save_webp(name: str, frames: list[Image.Image], durations: list[int]) -> int:
    DST.mkdir(parents=True, exist_ok=True)
    dst = DST / f"{name}.webp"
    frames[0].save(
        dst,
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        quality=82,
        alpha_quality=90,
        method=4,
    )
    total = sum(durations)
    print(f"{name}: {len(frames)} frames, {total}ms loop, {dst.stat().st_size // 1024}KB")
    return total


def main() -> None:
    loop_ms: dict[str, int] = {}

    # 1. idle: crisp original frames (sit, blink, ear/tail micro-motion) with calm holds
    idle_src = [load_keyframe("idle", i) for i in range(7)]
    idle_durs = [2800, 95, 130, 95, 2200, 850, 1500]
    loop_ms["idle"] = save_webp("idle", idle_src, idle_durs)

    # 2. idle_lazy: full hand-drawn frame-by-frame sequence
    k_lying = load_keyframe("idle_lazy", 0)
    k_yawn = load_keyframe("idle_lazy", 1)
    k_relaxed = load_keyframe("idle_lazy", 2)
    k_sleep = load_keyframe("idle_lazy", 3)

    g_la33 = align_gen_frame("lazy_a_33", k_lying, k_yawn, 0.33)
    g_la66 = align_gen_frame("lazy_a_66", k_lying, k_yawn, 0.66)
    g_lb33 = align_gen_frame("lazy_b_33", k_yawn, k_relaxed, 0.33)
    g_lb66 = align_gen_frame("lazy_b_66", k_yawn, k_relaxed, 0.66)
    g_lc33 = align_gen_frame("lazy_c_33", k_relaxed, k_sleep, 0.33)
    g_lc66 = align_gen_frame("lazy_c_66", k_relaxed, k_sleep, 0.66)
    g_ld50 = align_gen_frame("lazy_d_50", k_sleep, k_lying, 0.50)

    lazy_seq = [
        (k_lying, 2400),
        (g_la33, 170),
        (g_la66, 170),
        (k_yawn, 1500),
        (g_lb33, 170),
        (g_lb66, 180),
        (k_relaxed, 2200),
        (g_lc33, 180),
        (g_lc66, 220),
        (k_sleep, 2800),
        (g_lc66, 360),   # gentle sleep exhale (bubble pops)
        (k_sleep, 2600), # deep nap continues
        (g_ld50, 240),   # drowsy wake-up back to lying
    ]
    loop_ms["idle_lazy"] = save_webp(
        "idle_lazy", [f for f, _ in lazy_seq], [d for _, d in lazy_seq]
    )

    # 3. idle_playful: full hand-drawn frame-by-frame sequence
    k_tilt = load_keyframe("idle_playful", 0)
    k_lick = load_keyframe("idle_playful", 1)
    k_paw = load_keyframe("idle_playful", 3)
    k_wave = load_keyframe("idle_playful", 4)

    g_pa33 = align_gen_frame("play_a_33", k_tilt, k_lick, 0.33)
    g_pa66 = align_gen_frame("play_a_66", k_tilt, k_lick, 0.66)
    g_pb33 = align_gen_frame("play_b_33", k_tilt, k_paw, 0.33)
    g_pb66 = align_gen_frame("play_b_66", k_tilt, k_paw, 0.66)
    g_pc50 = align_gen_frame("play_c_50", k_paw, k_wave, 0.50)

    play_seq = [
        (k_tilt, 1900),
        (g_pa33, 150),
        (g_pa66, 150),
        (k_lick, 1600),
        (g_pa66, 150),
        (g_pa33, 150),
        (k_tilt, 1700),
        (g_pb33, 150),
        (g_pb66, 150),
        (k_paw, 1200),
        (g_pc50, 140),
        (k_wave, 1350),
        (g_pc50, 140),
        (k_paw, 950),
        (g_pb66, 150),
        (g_pb33, 150),
    ]
    loop_ms["idle_playful"] = save_webp(
        "idle_playful", [f for f, _ in play_seq], [d for _, d in play_seq]
    )

    # 4. thinking & waiting_input: crisp original frames with natural holds
    think_src = [load_keyframe("thinking", i) for i in range(6)]
    think_durs = [700, 650, 1100, 800, 850, 650]
    loop_ms["thinking"] = save_webp("thinking", think_src, think_durs)

    wait_src = [load_keyframe("waiting_input", i) for i in range(4)]
    wait_durs = [550, 480, 550, 620]
    loop_ms["waiting_input"] = save_webp("waiting_input", wait_src, wait_durs)

    body = "\n".join(f"  {k}: {v}," for k, v in loop_ms.items())
    META_TS.write_text(
        "// Generated by scripts/smooth_kiki_sprites.py - do not edit by hand.\n"
        "/** One full loop of each animated WebP, in ms. */\n"
        f"export const KIKI_LOOP_MS = {{\n{body}\n}} as const;\n"
    )


if __name__ == "__main__":
    main()
