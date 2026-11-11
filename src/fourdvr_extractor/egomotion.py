"""Camera ego-motion from tracked image points, for flat (pinhole), 180-degree and 360-degree video.

All projections are handled uniformly: image points are converted to unit *bearing vectors*
(camera frame: x right, y down, z forward). Between two frames, static scene points satisfy
b2 ~ R b1 - R T / depth. The rotation R is estimated robustly (2-point RANSAC + Kabsch); distant
points dominate the inlier set, so parallax from translation does not bias it. The translation
direction then follows from the epipolar constraint t . (R b1 x b2) = 0 (null vector, RANSAC),
its sign from the expansion of the de-rotated flow, and a scale-free speed (|T| / depth) from
the de-rotated flow magnitude. Monocular video cannot recover metric distances; the cueing stage
normalises these proxies.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

# camera (x right, y down, z forward) -> platform (X forward, Y left, Z up); det = +1
CAM_TO_PLATFORM = np.array([[0.0, 0.0, 1.0], [-1.0, 0.0, 0.0], [0.0, -1.0, 0.0]])


def bearings(points: np.ndarray, width: int, height: int, projection: str, hfov_deg: float = 75.0) -> np.ndarray:
    """Pixel coordinates (N, 2) -> unit bearing vectors (N, 3) in the camera frame."""
    u = points[:, 0].astype(np.float64)
    v = points[:, 1].astype(np.float64)
    if projection == "flat":
        f = (width / 2.0) / math.tan(math.radians(hfov_deg) / 2.0)
        vec = np.stack([(u - width / 2.0) / f, (v - height / 2.0) / f, np.ones_like(u)], axis=1)
    else:
        span = 2.0 * math.pi if projection == "360" else math.pi
        lon = (u / width - 0.5) * span
        lat = (0.5 - v / height) * math.pi
        vec = np.stack([np.cos(lat) * np.sin(lon), -np.sin(lat), np.cos(lat) * np.cos(lon)], axis=1)
    return vec / np.linalg.norm(vec, axis=1, keepdims=True)


def pixel_angle(width: int, projection: str, hfov_deg: float) -> float:
    """Approximate angle (rad) subtended by one pixel at the image centre."""
    if projection == "flat":
        f = (width / 2.0) / math.tan(math.radians(hfov_deg) / 2.0)
        return 1.0 / f
    span = 2.0 * math.pi if projection == "360" else math.pi
    return span / width


def kabsch(a: np.ndarray, b: np.ndarray, w: np.ndarray | None = None) -> np.ndarray:
    """Rotation R minimising sum w |R a_i - b_i|^2."""
    if w is None:
        w = np.ones(len(a))
    h = (a * w[:, None]).T @ b
    u, _s, vt = np.linalg.svd(h)
    d = np.sign(np.linalg.det(vt.T @ u.T)) or 1.0
    return vt.T @ np.diag([1.0, 1.0, d]) @ u.T


def rotvec(r: np.ndarray) -> np.ndarray:
    """Axis-angle vector of a rotation matrix."""
    cos_t = max(-1.0, min(1.0, (np.trace(r) - 1.0) / 2.0))
    theta = math.acos(cos_t)
    if theta < 1e-9:
        return np.array([r[2, 1] - r[1, 2], r[0, 2] - r[2, 0], r[1, 0] - r[0, 1]]) / 2.0
    axis = np.array([r[2, 1] - r[1, 2], r[0, 2] - r[2, 0], r[1, 0] - r[0, 1]]) / (2.0 * math.sin(theta))
    return axis * theta


@dataclass
class EgoMotion:
    rotation: np.ndarray  # rotation vector of the camera between the frames (camera frame, rad)
    direction: np.ndarray  # unit translation direction (camera frame), zeros if unobservable
    speed: float  # |T| / median depth per frame (scale-free)
    inliers: float  # fraction of points consistent with the rotation model
    parallax: float  # median de-rotated flow (rad)
    points: int
    valid: bool


def _essential_from(b1: np.ndarray, b2: np.ndarray) -> np.ndarray:
    """Linear 8-point essential matrix for bearing vectors (b2^T E b1 = 0), rank-2 enforced."""
    a = np.einsum("ni,nj->nij", b2, b1).reshape(len(b1), 9)
    _u, _s, vt = np.linalg.svd(a)
    e = vt[-1].reshape(3, 3)
    u, _sv, vt2 = np.linalg.svd(e)
    return u @ np.diag([1.0, 1.0, 0.0]) @ vt2


def _epipolar_error(e: np.ndarray, b1: np.ndarray, b2: np.ndarray) -> np.ndarray:
    """Sampson-type angular error of each correspondence."""
    eb1 = b1 @ e.T
    etb2 = b2 @ e
    num = np.einsum("ni,ni->n", b2, eb1)
    den = np.sum(eb1 * eb1, axis=1) + np.sum(etb2 * etb2, axis=1)
    return np.abs(num) / np.sqrt(np.maximum(den, 1e-18))


def _decompose(e: np.ndarray, b1: np.ndarray, b2: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(R, t) with x2 ~ R x1 + t, choosing the solution that puts most points in front (cheirality)."""
    u, _s, vt = np.linalg.svd(e)
    if np.linalg.det(u) < 0:
        u = -u
    if np.linalg.det(vt) < 0:
        vt = -vt
    w = np.array([[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])
    best = (np.eye(3), np.zeros(3))
    best_count = -1
    for r in (u @ w @ vt, u @ w.T @ vt):
        for t in (u[:, 2], -u[:, 2]):
            rb1 = b1 @ r.T
            # solve l2 * b2 - l1 * (R b1) = t for (l1, l2) per point (least squares)
            m11 = np.einsum("ni,ni->n", rb1, rb1)
            m12 = -np.einsum("ni,ni->n", rb1, b2)
            m22 = np.einsum("ni,ni->n", b2, b2)
            r1 = -np.einsum("ni,i->n", rb1, t)
            r2 = np.einsum("ni,i->n", b2, t)
            det = m11 * m22 - m12 * m12
            ok = np.abs(det) > 1e-12
            l1 = np.where(ok, (r1 * m22 - m12 * r2) / np.where(ok, det, 1), 0)
            l2 = np.where(ok, (m11 * r2 - m12 * r1) / np.where(ok, det, 1), 0)
            count = int(np.sum((l1 > 0) & (l2 > 0)))
            if count > best_count:
                best_count, best = count, (r, t)
    return best


def estimate(
    b1: np.ndarray,
    b2: np.ndarray,
    threshold: float,
    rng: np.random.Generator,
    iterations: int = 120,
) -> EgoMotion:
    """Robust ego-motion between two sets of corresponding bearing vectors.

    Model selection: a rotation-only model (2-point RANSAC + Kabsch) is used when it explains at
    least half of the points (distant scenery, pans, small translations); otherwise a general
    rotation + translation model (8-point essential matrix RANSAC + cheirality) is fitted, which
    stays correct for fast forward motion with strong parallax.
    """
    n = len(b1)
    zero = EgoMotion(np.zeros(3), np.zeros(3), 0.0, 0.0, 0.0, n, False)
    if n < 8:
        return zero
    best_inl: np.ndarray | None = None
    best_count = 0
    for _ in range(iterations):
        i, j = rng.choice(n, size=2, replace=False)
        a = np.stack([b1[i], b1[j], np.cross(b1[i], b1[j])])
        b = np.stack([b2[i], b2[j], np.cross(b2[i], b2[j])])
        if np.linalg.norm(a[2]) < 1e-3:
            continue
        r_try = kabsch(a, b)
        inl = np.linalg.norm(b1 @ r_try.T - b2, axis=1) < threshold
        count = int(inl.sum())
        if count > best_count:
            best_count, best_inl = count, inl
    rotation_ratio = best_count / n
    r: np.ndarray | None = None
    support = rotation_ratio
    if best_inl is not None and rotation_ratio >= 0.5:
        r = kabsch(b1[best_inl], b2[best_inl])
        inl = np.linalg.norm(b1 @ r.T - b2, axis=1) < threshold
        r = kabsch(b1[inl], b2[inl])
    else:
        best_e, best_c = None, 0
        for _ in range(2 * iterations):
            idx = rng.choice(n, size=8, replace=False)
            e = _essential_from(b1[idx], b2[idx])
            c = int(np.sum(_epipolar_error(e, b1, b2) < threshold))
            if c > best_c:
                best_c, best_e = c, e
        if best_e is None or best_c < max(8, int(0.3 * n)):
            return zero
        inl = _epipolar_error(best_e, b1, b2) < threshold
        e = _essential_from(b1[inl], b2[inl])
        r, _t = _decompose(e, b1[inl], b2[inl])
        support = best_c / n
    cam_rot = -rotvec(r)  # scene points rotate by R = Q^T when the camera rotates by Q

    # translation from the de-rotated flow (epipolar null vector, RANSAC), sign from expansion
    rb1 = b1 @ r.T
    flow = b2 - rb1
    parallax = float(np.median(np.linalg.norm(flow, axis=1)))
    direction = np.zeros(3)
    speed = 0.0
    if parallax > 0.35 * threshold:
        normals = np.cross(rb1, b2)
        norms = np.linalg.norm(normals, axis=1)
        keep = norms > 1e-9
        nrm = normals[keep] / norms[keep, None]
        best_t, best_c = None, 0
        m = len(nrm)
        for _ in range(iterations):
            i, j = rng.choice(m, size=2, replace=False)
            t = np.cross(nrm[i], nrm[j])
            tn = np.linalg.norm(t)
            if tn < 1e-6:
                continue
            t /= tn
            c = int(np.sum(np.abs(nrm @ t) < 0.05))
            if c > best_c:
                best_c, best_t = c, t
        if best_t is not None and best_c >= 6:
            sel = np.abs(nrm @ best_t) < 0.05
            _u, _s, vt = np.linalg.svd(nrm[sel])
            t = vt[-1]
            pts = b2[keep][sel]
            fl = flow[keep][sel]
            expected = (pts @ t)[:, None] * pts - t  # flow direction for camera motion along +t
            if float(np.sum(fl * expected)) < 0:
                t = -t
                expected = -expected
            exp_norm = np.linalg.norm(expected, axis=1)
            ok = exp_norm > 0.05
            if ok.sum() >= 4:
                speed = float(np.median(np.linalg.norm(fl[ok], axis=1) / exp_norm[ok]))
                direction = t
    return EgoMotion(cam_rot, direction, speed, support, parallax, n, True)


def to_platform(vec: np.ndarray) -> np.ndarray:
    return CAM_TO_PLATFORM @ vec
