#!/usr/bin/env python3
"""Build silky ~18fps animated Kiki WebPs from dense hand-drawn keyframes + DIS optical flow.

Source transparent keyframes live in `assets/kiki/*.webp`.
Generated white-background hand-drawn in-betweens live in `assets/kiki/interp/gen/*.jpg`.

Pipeline:
  1. Flood-fill white backgrounds to clean anti-aliased RGBA (protecting cream/white fur).
  2. Align every generated frame's scale & bottom-center anchor smoothly between its
     start and end keyframes (or exact-lock to `idle_sit` for blink frames).
  3. Start & end both `idle_lazy` and `idle_playful` on the exact `idle_sit` pose so
     companion state transitions (`idle` <-> `idle_lazy` / `idle_playful`) have ZERO jump.
  4. Insert adaptive bidirectional DIS optical-flow warped frames between consecutive
     hand-drawn poses (~55ms per step = ~18fps) with automatic ghost-protection.
  5. Save optimized WebPs to `frontend/public/kiki/` and write loop durations to
     `frontend/src/components/kikiSpriteMeta.ts`.

Usage: python3 scripts/smooth_kiki_sprites.py
"""
from __future__ import annotations

from collections import deque
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "assets" / "kiki"
GEN = SRC / "interp" / "gen"
DST = ROOT / "frontend" / "public" / "kiki"
META_TS = ROOT / "frontend" / "src" / "components" / "kikiSpriteMeta.ts"

OUT_SIZE = 288  # displayed at <=144px CSS (2x retina crisp while keeping ~18fps WebPs compact)


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

    r = rgb[..., 0].astype(np.int16)
    g = rgb[..., 1].astype(np.int16)
    b = rgb[..., 2].astype(np.int16)
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

    fringe = np.zeros((h, w), dtype=bool)
    for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)):
        shifted = np.roll(np.roll(visited, dy, axis=0), dx, axis=1)
        fringe |= shifted & (~visited)
    fringe2 = fringe.copy()
    for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        shifted = np.roll(np.roll(fringe, dy, axis=0), dx, axis=1)
        fringe2 |= shifted & (~visited)

    f_rgb = rgb.astype(np.float32)
    bright_fringe = fringe2 & (lum > 180) & (chroma < 38)
    t_alpha = np.clip((248.0 - lum) / 68.0, 0.0, 1.0)
    alpha = np.where(bright_fringe, t_alpha * 255.0, alpha)

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
    if nw > OUT_SIZE - 8:
        clamp = (OUT_SIZE - 8) / nw
        nw = round(nw * clamp)
        nh = round(nh * clamp)

    resized = cropped.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGBA", (OUT_SIZE, OUT_SIZE), (0, 0, 0, 0))
    px = max(0, min(OUT_SIZE - nw, round(target_cx - nw / 2.0)))
    py = max(0, min(OUT_SIZE - nh, round(target_by - nh)))
    canvas.paste(resized, (px, py), resized)
    return canvas


_DIS = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)
_DIS.setFinestScale(0)


def _to_gray_on_white(rgba: np.ndarray) -> np.ndarray:
    a = rgba[..., 3:4].astype(np.float32) / 255.0
    rgb = rgba[..., :3].astype(np.float32) * a + 255.0 * (1.0 - a)
    return cv2.cvtColor(rgb.astype(np.uint8), cv2.COLOR_RGB2GRAY)


def _warp_premul(rgba: np.ndarray, flow: np.ndarray, scale: float) -> np.ndarray:
    h, w = rgba.shape[:2]
    gx, gy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    pm = rgba.astype(np.float32)
    a = pm[..., 3:4] / 255.0
    pm[..., :3] *= a
    return cv2.remap(
        pm,
        gx - flow[..., 0] * scale,
        gy - flow[..., 1] * scale,
        cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(0, 0, 0, 0),
    )


def flow_bridge(
    im0: Image.Image,
    im1: Image.Image,
    max_steps: int = 3,
    step_ms: int = 55,
) -> list[tuple[Image.Image, int]]:
    """Generate adaptive bidirectional DIS optical-flow frames between im0 and im1."""
    if max_steps <= 0:
        return []
    f0 = np.asarray(im0, dtype=np.uint8)
    f1 = np.asarray(im1, dtype=np.uint8)
    g0 = _to_gray_on_white(f0)
    g1 = _to_gray_on_white(f1)

    flow_01 = cv2.GaussianBlur(_DIS.calc(g0, g1, None), (9, 9), 2.0)
    flow_10 = cv2.GaussianBlur(_DIS.calc(g1, g0, None), (9, 9), 2.0)

    mag = np.hypot(flow_01[..., 0], flow_01[..., 1])
    mask = (f0[..., 3] > 20) | (f1[..., 3] > 20)
    p90 = float(np.percentile(mag[mask], 90)) if mask.any() else 0.0

    # Measure photometric warp alignment error to strictly block any double-image ghosting
    h, w = g0.shape
    gx, gy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    warped_g0 = cv2.remap(
        g0, gx - flow_01[..., 0], gy - flow_01[..., 1], cv2.INTER_LINEAR, borderValue=255
    )
    warp_err = (
        float(np.abs(warped_g0.astype(np.float32) - g1.astype(np.float32))[mask].mean())
        if mask.any()
        else 99.0
    )

    # Strict ghost-free gate: only synthesize optical-flow frames when flow tracks cleanly
    if p90 < 2.0 or p90 > 26.0 or warp_err > 26.0:
        return []
    steps = max_steps if (p90 <= 18.0 and warp_err <= 20.0) else min(max_steps, 2)

    out: list[tuple[Image.Image, int]] = []
    for k in range(1, steps + 1):
        # Smoothstep easing feels more organic than linear spacing
        raw = k / (steps + 1)
        t = raw * raw * (3.0 - 2.0 * raw)
        w0 = _warp_premul(f0, flow_01, t)
        w1 = _warp_premul(f1, flow_10, 1.0 - t)
        pm = w0 * (1.0 - t) + w1 * t
        a = np.clip(pm[..., 3:4], 0.0, 255.0)
        rgb = np.where(
            a > 1.0,
            np.clip(pm[..., :3] / np.maximum(a / 255.0, 1e-4), 0.0, 255.0),
            0.0,
        )
        rgba = np.concatenate([rgb, a], axis=-1).astype(np.uint8)
        out.append((Image.fromarray(rgba, "RGBA"), step_ms))
    return out


def expand_sequence(
    keyframes: list[tuple[Image.Image, int, int, int]],
    close_loop: bool = True,
) -> tuple[list[Image.Image], list[int]]:
    """Expand `(image, hold_ms, flow_steps_to_next, flow_step_ms)` list into full frames."""
    frames: list[Image.Image] = []
    durs: list[int] = []
    n = len(keyframes)
    for i, (im, hold_ms, steps, step_ms) in enumerate(keyframes):
        frames.append(im)
        durs.append(hold_ms)
        if i + 1 < n or close_loop:
            nxt = keyframes[(i + 1) % n][0]
            for bf, bd in flow_bridge(im, nxt, max_steps=steps, step_ms=step_ms):
                frames.append(bf)
                durs.append(bd)
    return frames, durs


def save_webp(name: str, frames: list[Image.Image], durations: list[int]) -> int:
    DST.mkdir(parents=True, exist_ok=True)
    dst = DST / f"{name}.webp"
    frames[0].save(
        dst,
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        quality=76,
        alpha_quality=85,
        method=4,
    )
    total = sum(durations)
    print(f"{name}: {len(frames)} frames, {total}ms loop, {dst.stat().st_size // 1024}KB")
    return total


def main() -> None:
    loop_ms: dict[str, int] = {}

    # Shared anchor frame: exact sitting idle pose (frame 0 of idle.webp)
    k_sit = load_keyframe("idle", 0)
    k_sit_sway = load_keyframe("idle", 5)

    # 1. idle: calm sitting + natural eyes-only blink (NO tail whip!) + slow tail sway
    g_blink_half = align_gen_frame("idle_blink_half", k_sit, k_sit, 0.0)
    g_blink_closed = align_gen_frame("idle_blink_closed", k_sit, k_sit, 0.0)

    idle_spec = [
        (k_sit, 2900, 0, 50),          # calm sitting
        (g_blink_half, 65, 0, 50),     # natural blink down
        (g_blink_closed, 95, 0, 50),   # eyes closed
        (g_blink_half, 65, 0, 50),     # blink open
        (k_sit, 2400, 4, 75),          # calm hold -> slow lazy tail sway out
        (k_sit_sway, 1100, 4, 75),     # hold gently -> slow lazy tail sway back
    ]
    frames, durs = expand_sequence(idle_spec, close_loop=True)
    loop_ms["idle"] = save_webp("idle", frames, durs)

    # 2. idle_lazy: starts & ends on exact `k_sit`! Full sitting -> lying -> yawn -> nap -> sit up
    k_lying = load_keyframe("idle_lazy", 0)
    k_yawn = load_keyframe("idle_lazy", 1)
    k_relaxed = load_keyframe("idle_lazy", 2)
    k_sleep = load_keyframe("idle_lazy", 3)

    g_s2l_33 = align_gen_frame("sit_to_lying_33", k_sit, k_lying, 0.33)
    g_s2l_66 = align_gen_frame("sit_to_lying_66", k_sit, k_lying, 0.66)
    g_la33 = align_gen_frame("lazy_a_33", k_lying, k_yawn, 0.30)
    g_la50 = align_gen_frame("lazy_a_50", k_lying, k_yawn, 0.50)
    g_la66 = align_gen_frame("lazy_a_66", k_lying, k_yawn, 0.72)
    g_lb33 = align_gen_frame("lazy_b_33", k_yawn, k_relaxed, 0.28)
    g_lb50 = align_gen_frame("lazy_b_50", k_yawn, k_relaxed, 0.50)
    g_lb66 = align_gen_frame("lazy_b_66", k_yawn, k_relaxed, 0.75)
    g_lc33 = align_gen_frame("lazy_c_33", k_relaxed, k_sleep, 0.30)
    g_lc50 = align_gen_frame("lazy_c_50", k_relaxed, k_sleep, 0.52)
    g_lc66 = align_gen_frame("lazy_c_66", k_relaxed, k_sleep, 0.85)
    g_ld50 = align_gen_frame("lazy_d_50", k_sleep, k_lying, 0.35)
    g_ld75 = align_gen_frame("lazy_d_75", k_sleep, k_lying, 0.70)

    lazy_spec = [
        (k_sit, 220, 2, 55),           # seamless entry from idle
        (g_s2l_33, 90, 2, 55),
        (g_s2l_66, 90, 2, 55),
        (k_lying, 1700, 3, 55),        # settle lying drowsy
        (g_la33, 80, 2, 55),
        (g_la50, 80, 3, 55),
        (g_la66, 90, 3, 55),
        (k_yawn, 1350, 2, 55),         # big cozy yawn
        (g_lb33, 80, 2, 55),
        (g_lb50, 80, 2, 55),
        (g_lb66, 90, 3, 55),
        (k_relaxed, 1900, 3, 55),      # relaxed smile
        (g_lc33, 90, 3, 55),
        (g_lc50, 90, 2, 55),
        (g_lc66, 120, 3, 60),
        (k_sleep, 2600, 3, 75),        # deep nap with bubble
        (g_lc66, 260, 3, 75),          # gentle exhale (bubble pops)
        (k_sleep, 2400, 3, 65),        # nap continues
        (g_ld50, 120, 2, 55),          # waking up drowsily
        (g_ld75, 110, 2, 55),
        (k_lying, 1100, 2, 55),        # brief stretch before sitting back up
        (g_s2l_66, 90, 2, 55),
        (g_s2l_33, 90, 2, 55),
    ]
    frames, durs = expand_sequence(lazy_spec, close_loop=True)
    loop_ms["idle_lazy"] = save_webp("idle_lazy", frames, durs)

    # 3. idle_playful: starts & ends on exact `k_sit`! Sit -> tilt -> lick -> tilt -> paw -> wave -> sit
    k_tilt = load_keyframe("idle_playful", 0)
    k_lick = load_keyframe("idle_playful", 1)
    k_paw = load_keyframe("idle_playful", 3)
    k_wave = load_keyframe("idle_playful", 4)

    g_s2t_50 = align_gen_frame("sit_to_tilt_50", k_sit, k_tilt, 0.50)
    g_pa33 = align_gen_frame("play_a_33", k_tilt, k_lick, 0.33)
    g_pa66 = align_gen_frame("play_a_66", k_tilt, k_lick, 0.66)
    g_pb33 = align_gen_frame("play_b_33", k_tilt, k_paw, 0.25)
    g_pb50 = align_gen_frame("play_b_50", k_tilt, k_paw, 0.50)
    g_pb66 = align_gen_frame("play_b_66", k_tilt, k_paw, 0.75)
    g_pc50 = align_gen_frame("play_c_50", k_paw, k_wave, 0.45)
    g_pc75 = align_gen_frame("play_c_75", k_paw, k_wave, 0.78)

    play_spec = [
        (k_sit, 220, 2, 55),           # seamless entry from idle
        (g_s2t_50, 80, 2, 55),
        (k_tilt, 1400, 3, 55),         # playful head tilt
        (g_pa33, 75, 3, 50),
        (g_pa66, 75, 3, 50),
        (k_lick, 1450, 3, 50),         # licking paw
        (g_pa66, 75, 3, 50),
        (g_pa33, 75, 3, 55),
        (k_tilt, 1300, 2, 55),         # back to playful tilt
        (g_pb33, 75, 2, 55),
        (g_pb50, 75, 3, 55),
        (g_pb66, 75, 3, 55),
        (k_paw, 1050, 2, 55),          # paw pad raised
        (g_pc50, 70, 2, 50),
        (g_pc75, 70, 3, 50),
        (k_wave, 1250, 3, 50),         # excited high wave
        (g_pc75, 70, 2, 50),
        (g_pc50, 70, 2, 55),
        (k_paw, 850, 3, 55),
        (g_pb66, 75, 3, 55),
        (g_pb50, 75, 2, 55),
        (g_pb33, 75, 2, 55),
        (k_tilt, 700, 2, 55),
        (g_s2t_50, 80, 2, 55),         # return smoothly to idle_sit
    ]
    frames, durs = expand_sequence(play_spec, close_loop=True)
    loop_ms["idle_playful"] = save_webp("idle_playful", frames, durs)

    # 4. thinking & waiting_input: gentle optical-flow smoothing on original keyframes
    think_src = [load_keyframe("thinking", i) for i in range(6)]
    think_spec = [
        (think_src[0], 700, 2, 60),
        (think_src[1], 650, 2, 60),
        (think_src[2], 1100, 3, 60),
        (think_src[3], 850, 3, 60),
        (think_src[4], 850, 2, 60),
        (think_src[5], 650, 2, 60),
    ]
    frames, durs = expand_sequence(think_spec, close_loop=True)
    loop_ms["thinking"] = save_webp("thinking", frames, durs)

    wait_src = [load_keyframe("waiting_input", i) for i in range(4)]
    wait_spec = [
        (wait_src[0], 550, 2, 55),
        (wait_src[1], 480, 2, 55),
        (wait_src[2], 550, 2, 55),
        (wait_src[3], 620, 2, 55),
    ]
    frames, durs = expand_sequence(wait_spec, close_loop=True)
    loop_ms["waiting_input"] = save_webp("waiting_input", frames, durs)

    body = "\n".join(f"  {k}: {v}," for k, v in loop_ms.items())
    META_TS.write_text(
        "// Generated by scripts/smooth_kiki_sprites.py - do not edit by hand.\n"
        "/** One full loop of each animated WebP, in ms. */\n"
        f"export const KIKI_LOOP_MS = {{\n{body}\n}} as const;\n"
    )


if __name__ == "__main__":
    main()
