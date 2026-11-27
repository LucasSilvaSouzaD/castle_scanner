"""Map routes for the castle scanner.

A route only changes the ORDER in which the scanner visits its navigation
points; the set of points is never changed, so coverage and results are the
same as the fixed route. Used by gotwic_scanner_v2_2.py (--route / --pace).
"""
import math
import random

STEP_X, STEP_Y = 25, 13
CENTER = 360
ROUTES = ('rows', 'columns', 'spiral-out', 'spiral-in', 'blocks', 'diagonal')


def _serpentine(points, major, minor, flip_major=False, flip_minor=False):
    """Group by `major` coordinate, walk each group along `minor`, alternating direction."""
    lines = {}
    for p in points:
        lines.setdefault(major(p), []).append(p)
    out = []
    for i, key in enumerate(sorted(lines, reverse=flip_major)):
        line = sorted(lines[key], key=minor, reverse=flip_minor)
        out.extend(line if i % 2 == 0 else line[::-1])
    return out


def _rows(points, rng):
    return _serpentine(points, lambda p: p[1], lambda p: p[0], rng.random() < .5, rng.random() < .5)


def _columns(points, rng):
    return _serpentine(points, lambda p: p[0], lambda p: p[1], rng.random() < .5, rng.random() < .5)


def _diagonal(points, rng):
    fx, fy = rng.choice((1, -1)), rng.choice((1, -1))
    return _serpentine(points, lambda p: round(fx * p[0] / STEP_X + fy * p[1] / STEP_Y),
                       lambda p: p[0], rng.random() < .5, rng.random() < .5)


def _spiral(points, rng, outward=True):
    cx = CENTER + rng.randint(-60, 60)
    cy = CENTER + rng.randint(-60, 60)
    start = rng.uniform(0, 2 * math.pi)
    direction = rng.choice((1, -1))

    def key(p):
        dx, dy = (p[0] - cx) / STEP_X, (p[1] - cy) / STEP_Y
        ring = int(max(abs(dx), abs(dy)))
        angle = (direction * (math.atan2(dy, dx) - start)) % (2 * math.pi)
        return (ring if outward else -ring, angle)
    return sorted(points, key=key)


def _blocks(points, rng):
    size = rng.choice((120, 150, 180, 240))
    blocks = {}
    for p in points:
        blocks.setdefault((p[0] // size, p[1] // size), []).append(p)
    order = list(blocks)
    rng.shuffle(order)
    out = []
    for b in order:
        out.extend(_rows(blocks[b], rng) if rng.random() < .5 else _columns(blocks[b], rng))
    return out


_BUILDERS = {'rows': _rows, 'columns': _columns, 'diagonal': _diagonal, 'blocks': _blocks,
             'spiral-out': lambda pts, rng: _spiral(pts, rng, True),
             'spiral-in': lambda pts, rng: _spiral(pts, rng, False)}


def plan_route(grid_points, extra_points, route='random', seed=None):
    """Return (ordered points, route name, seed). `extra_points` are mixed in at random positions."""
    if seed is None:
        seed = random.SystemRandom().randrange(1, 2 ** 31)
    rng = random.Random(seed)
    name = rng.choice(ROUTES) if route == 'random' else route
    if name not in _BUILDERS:
        raise ValueError(f'Unknown route: {route}')
    ordered = _BUILDERS[name](list(grid_points), rng)
    for p in extra_points:
        ordered.insert(rng.randint(0, len(ordered)), p)
    if sorted(ordered) != sorted(list(grid_points) + list(extra_points)):  # never lose or add a point
        raise RuntimeError(f'Route {name} changed the set of scan points')
    return ordered, name, seed


def plan_batches(total, buffer, pace='normal', rng=None):
    """Split `total` points into (offset, size, pause_after_s) batches.

    normal: fixed batches of `buffer`, no pauses (the original behaviour).
    discreet: batch sizes vary between 40% and 100% of the buffer, a short random
    pause after most batches and, now and then, a slightly longer one.
    """
    out = []
    offset = 0
    if pace != 'discreet':
        while offset < total:
            size = min(buffer, total - offset)
            out.append((offset, size, 0.0))
            offset += size
        return out
    rng = rng or random.Random()
    low = max(1, math.ceil(buffer * 0.4))
    while offset < total:
        size = min(rng.randint(low, buffer), total - offset)
        pause = rng.uniform(0.05, 0.25)
        if rng.random() < 0.04:
            pause += rng.uniform(1.0, 2.5)
        offset += size
        out.append((offset - size, size, 0.0 if offset >= total else round(pause, 3)))
    return out
