"""Helpers shared by the web app and static reports."""


def sparkline(points: list[int], w=320, h=60) -> str:
    if len(points) < 2:
        return ""
    step = w / (len(points) - 1)
    lo, hi = max(0, min(points) - 5), min(100, max(points) + 5)
    rng = max(hi - lo, 1)
    coords = " ".join(f"{i*step:.1f},{h - ((p - lo) / rng) * h:.1f}" for i, p in enumerate(points))
    return coords
