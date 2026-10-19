#!/usr/bin/env python3
"""Ball Balance (terminal edition): tilt your MacBook to keep the ball on the plate.

Top-down view drawn with half-block characters. Three things tell you which
way is level:
  * the plate is shaded by height: the darker side is downhill,
  * a dotted arrow on the ball points where it is about to roll,
  * the tilt guide works like a spirit level: keep its dot in the green middle
    and the ball stays still. It also tells you which side to raise.

    python3 ball_balance.py          # tilt with the MacBook's motion sensor
    python3 ball_balance.py --keys   # arrow keys instead

Without a MacBook motion sensor (other computers, Linux, Windows) it uses the
arrow keys automatically. Best score and settings live in ~/.ball_balance.json.

Keys: Space start · C direction check · [ ] sensitivity · Q quit
"""

import json
import math
import os
import random
import shutil
import sys
import time
from collections import deque

WINDOWS = sys.platform == "win32"
if WINDOWS:
    import ctypes
    import msvcrt
else:
    import select
    import termios
    import tty

from mac_imu import IMUError, MacIMU

FPS = 30
SAVE_PATH = os.path.join(os.path.expanduser("~"), ".ball_balance.json")
LEGACY_SAVE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ball_balance_save.json")

# Gameplay tuning, in plate units (the plate starts 2 units across).
PLATE_START, PLATE_MIN, SHRINK_TIME = 1.0, 0.45, 120.0
BALL_R = 0.1
COIN_R = 0.08
GRAVITY = 2.6          # in-plane accel (units/s^2) per 1 g of tilt, rolling factor included
ROLL_RESIST = 0.05     # constant rolling resistance (units/s^2)
DAMPING = 0.45         # linear drag (1/s)
STATIC_ACCEL = 0.05    # a resting ball stays put below ~1 degree of tilt
FALL_TIME = 0.9
WIND_START = 15.0
GUST_WARNING = 1.2
COIN_POINTS = 10
LEVEL_DEG = 1.0        # inside this the ball doesn't move
GUIDE_RANGE = 10.0     # degrees at the rim of the tilt guide
DEFAULT_AXES = [[0, 1], [1, 1]]  # [sensor axis, sign] for plate x (right) and y (away from you)

VOID = (11, 13, 26)
PLATE = (46, 140, 158)
RIM = (150, 225, 235)
DANGER = (255, 80, 60)
BALL = (255, 118, 78)
GOLD, GOLD_DARK = (255, 200, 60), (170, 110, 20)
STREAK = (190, 220, 255)
ARROW = (255, 250, 225)
TEXT, DIM = (230, 236, 248), (120, 132, 160)
GOOD, WARN, BAD = (90, 220, 120), (255, 200, 70), (255, 95, 70)
BOX_BG = (24, 28, 50)

DEFAULTS = {"axes": None, "sens": 1.0, "best_score": 0, "best_time": 0.0}

# downhill direction (8 sectors, counter-clockwise from right) -> arrow, what to raise
DIRECTIONS = [("→", "right edge"), ("↗", "back-right corner"), ("↑", "back (hinge) edge"),
              ("↖", "back-left corner"), ("←", "left edge"), ("↙", "front-left corner"),
              ("↓", "front edge"), ("↘", "front-right corner")]


# ---------- small helpers ----------

def clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


def mix(c1, c2, t):
    return (int(c1[0] + (c2[0] - c1[0]) * t), int(c1[1] + (c2[1] - c1[1]) * t), int(c1[2] + (c2[2] - c1[2]) * t))


def shade(c, k):
    return (int(clamp(c[0] * k, 0, 255)), int(clamp(c[1] * k, 0, 255)), int(clamp(c[2] * k, 0, 255)))


def unit(a):
    n = math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2]) or 1.0
    return (a[0] / n, a[1] / n, a[2] / n)


def rot_align(a, b):
    """Shortest-arc rotation matrix taking unit vector a onto unit vector b."""
    vx, vy, vz = a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]
    c = a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
    if c < -0.9999:
        return ((1, 0, 0), (0, -1, 0), (0, 0, -1))
    k = 1.0 / (1.0 + c)
    return ((1 - k * (vy * vy + vz * vz), -vz + k * vx * vy, vy + k * vx * vz),
            (vz + k * vx * vy, 1 - k * (vx * vx + vz * vz), -vx + k * vy * vz),
            (-vy + k * vx * vz, vx + k * vy * vz, 1 - k * (vx * vx + vy * vy)))


def matvec(m, v):
    return tuple(m[i][0] * v[0] + m[i][1] * v[1] + m[i][2] * v[2] for i in range(3))


def direction(dx, dy):
    """(arrow, name) for a vector in plate coordinates (x right, y away from you)."""
    sector = round(math.atan2(dy, dx) / (math.pi / 4)) % 8
    return DIRECTIONS[sector]


# Palette. Every pixel colour comes from this small fixed set. Terminals cache one glyph image per colour pair, and
# endless gradients overflow that cache and leave stray pixels on screen.
BAND_LEVELS = 6
PLATE_BANDS = [shade(PLATE, 1 + 0.075 * i) for i in range(-BAND_LEVELS, BAND_LEVELS + 1)]
RING = mix(PLATE, (255, 255, 255), 0.25)
SHADOW = shade(PLATE, 0.5)
STREAK_DIM = mix(PLATE, STREAK, 0.5)
RIM_LEVELS = [mix(RIM, DANGER, i / 3) for i in range(4)]
BALL_SHADES = [shade(BALL, k) for k in (0.4, 0.58, 0.76, 0.94, 1.1)] + [mix(BALL, (255, 255, 255), 0.6)]


def load_settings():
    s = dict(DEFAULTS)
    for path in (SAVE_PATH, LEGACY_SAVE):
        try:
            with open(path) as f:
                saved = json.load(f)
            s.update({k: v for k, v in saved.items() if k in DEFAULTS})
            break
        except (OSError, ValueError):
            pass
    return s


def save_settings(s):
    try:
        with open(SAVE_PATH, "w") as f:
            json.dump(s, f, indent=2)
    except OSError:
        pass


# ---------- input ----------

class TiltInput:
    """Gravity direction in the plate's frame: x right, y away from you, z up."""

    def __init__(self, settings, use_sensor=True):
        self.s = settings
        self.imu = None
        self.error = None
        self.level = (0.0, 0.0, -1.0)
        self.key_tilt = [0.0, 0.0]  # degrees, keyboard mode
        self.accel = None           # lightly smoothed raw accelerometer (unit vector), for calibration
        if use_sensor:
            try:
                self.imu = MacIMU().start()
            except (IMUError, OSError) as e:
                self.error = str(e)

    @property
    def status(self):
        if self.imu:
            return {"learning": "sensor: accelerometer (gyro joins once you tilt)",
                    "gyro+accel": "sensor: gyro + accelerometer",
                    "accel only": "sensor: accelerometer only"}[self.imu.fusion]
        return "arrow keys" + (" (motion sensor unavailable)" if self.error else "")

    def nudge(self, dx, dy):
        self.key_tilt[0] = clamp(self.key_tilt[0] + dx * 3.0, -15, 15)
        self.key_tilt[1] = clamp(self.key_tilt[1] + dy * 3.0, -15, 15)

    def update(self, dt):
        if not self.imu:
            decay = math.exp(-dt * 0.7)
            self.key_tilt = [v * decay for v in self.key_tilt]
            return
        a = self.imu.read()[0]
        if a:
            u = unit(a)
            if self.accel is None:
                self.accel = u
            else:
                k = min(1.0, dt / 0.08)
                self.accel = unit(tuple(self.accel[i] + (u[i] - self.accel[i]) * k for i in range(3)))

    def device_gravity(self):
        return (self.imu.gravity_vector() if self.imu else None) or (0.0, 0.0, -1.0)

    def set_level(self):
        """The way the Mac is held right now counts as level."""
        if self.imu:
            self.level = unit(self.device_gravity())

    def plate_gravity(self):
        if not self.imu:
            gx, gy = math.sin(math.radians(self.key_tilt[0])), math.sin(math.radians(self.key_tilt[1]))
        else:
            g = matvec(rot_align(self.level, (0.0, 0.0, -1.0)), self.device_gravity())
            (ix, sx), (iy, sy) = self.s["axes"] or DEFAULT_AXES
            gx, gy = sx * g[ix], sy * g[iy]
        return (gx, gy, -math.sqrt(max(0.0, 1 - gx * gx - gy * gy)))

    def close(self):
        if self.imu:
            self.imu.stop()


# ---------- drawing surfaces ----------

class Canvas:
    """RGB pixel grid; two pixel rows share one terminal row via '▀'."""

    def __init__(self, w, h, color=VOID):
        self.w, self.h = w, h
        self.pix = [[color] * w for _ in range(h)]

    def put(self, x, y, color):
        if 0 <= x < self.w and 0 <= y < self.h:
            self.pix[y][x] = color

    def disc(self, cx, cy, r, color, squash=1.0):
        """Hard-edged filled ellipse with radii (r*squash, r). No anti-aliasing on purpose."""
        rx = max(r * squash, 0.5)
        for y in range(int(cy - r - 1), int(cy + r + 2)):
            for x in range(int(cx - rx - 1), int(cx + rx + 2)):
                if math.hypot((x + 0.5 - cx) / rx, (y + 0.5 - cy) / r) <= 1.0 + 0.35 / max(r, 1):
                    self.put(x, y, color)


class Screen:
    def __init__(self, cols, rows):
        self.cols, self.rows = cols, rows
        self.cells = [[[" ", TEXT, VOID, False] for _ in range(cols)] for _ in range(rows)]

    def text(self, row, col, s, fg=TEXT, bg=None, bold=False):
        if not 0 <= row < self.rows:
            return
        for i, ch in enumerate(s):
            x = col + i
            if 0 <= x < self.cols:
                cell = self.cells[row][x]
                cell[0], cell[1], cell[3] = ch, fg, bold
                if bg is not None:
                    cell[2] = bg

    def center(self, row, col, width, s, fg=TEXT, bg=None, bold=False):
        self.text(row, col + (width - len(s)) // 2, s, fg, bg, bold)

    def canvas(self, row, col, cv):
        for r in range(cv.h // 2):
            if not 0 <= row + r < self.rows:
                continue
            top, bot = cv.pix[2 * r], cv.pix[2 * r + 1]
            line = self.cells[row + r]
            for c in range(cv.w):
                if 0 <= col + c < self.cols:
                    # same colour top and bottom: a plain space draws no glyph at all
                    line[col + c] = [" " if top[c] == bot[c] else "▀", top[c], bot[c], False]

    def bg_at(self, row, col):
        return self.cells[row][col][2] if 0 <= row < self.rows and 0 <= col < self.cols else VOID

    def lines(self, truecolor):
        out = []
        for row in self.cells:
            parts, fg, bg, bold = [], None, None, None
            for ch, cfg, cbg, cbold in row:
                if cbold != bold:
                    parts.append("\x1b[1m" if cbold else "\x1b[22m")
                    bold = cbold
                if cfg != fg and ch != " ":
                    parts.append(_sgr(38, cfg, truecolor))
                    fg = cfg
                if cbg != bg:
                    parts.append(_sgr(48, cbg, truecolor))
                    bg = cbg
                parts.append(ch)
            out.append("".join(parts))
        return out


def _sgr(kind, c, truecolor):
    if truecolor:
        return f"\x1b[{kind};2;{c[0]};{c[1]};{c[2]}m"
    r, g, b = (0 if v < 48 else 1 if v < 115 else (v - 35) // 40 for v in c)
    return f"\x1b[{kind};5;{16 + 36 * r + 6 * g + b}m"


# ---------- game ----------

CALIB_STEPS = [
    ("flat", ["Put your Mac flat on the table", "(or hold it level) and keep it still."]),
    ("right", ["Tilt your Mac to the RIGHT:", "lift its LEFT edge a few cm and hold."]),
    ("front", ["Tilt your Mac TOWARD YOU:", "lift the BACK (hinge side) a few cm and hold."]),
    ("verify", ["Check: tilt it to the RIGHT again", "(lift the LEFT edge) and hold."]),
]


class Game:
    def __init__(self, tilt, settings):
        self.tilt = tilt
        self.s = settings
        self.anim = 0.0
        self.toast = None
        self.state = "ready"
        self.calib = None
        self.gravity = (0.0, 0.0, -1.0)
        self.reset()

    def reset(self):
        self.t = 0.0
        self.h = PLATE_START
        self.pos = [0.0, 0.0]
        self.vel = [0.0, 0.0]
        self.coins = []
        self.coin_count = 0
        self.wind = (0.0, 0.0)
        self.gust = None
        self.next_gust = WIND_START + random.uniform(0.0, 3.0)
        self.streaks = []
        self.streak_acc = 0.0
        self.particles = []
        self.popups = []
        self.fall_t = 0.0
        self.new_best = False

    @property
    def score(self):
        return int(self.t) + COIN_POINTS * self.coin_count

    def say(self, msg):
        self.toast = (msg, self.anim + 2.5)

    # ----- input -----

    def handle(self, key):
        """Returns False when the game should quit."""
        if key == "q":
            return False
        if key == "esc":
            if self.state == "calibrate":
                self.state = "ready"
                self.say("Calibration skipped")
                return True
            return False
        if key == " " and self.state in ("ready", "over"):
            self.tilt.set_level()
            self.reset()
            self.state = "playing"
            self.spawn_coin()
        elif key == "c" and self.tilt.imu:
            self.start_calibration()
        elif key in ("[", "]"):
            self.s["sens"] = round(clamp(self.s["sens"] + (0.1 if key == "]" else -0.1), 0.5, 2.0), 1)
            self.say(f"Sensitivity {self.s['sens']:.1f}x")
            save_settings(self.s)
        elif key in ("up", "down", "left", "right") and not self.tilt.imu:
            self.tilt.nudge(*{"up": (0, 1), "down": (0, -1), "left": (-1, 0), "right": (1, 0)}[key])
        return True

    # ----- calibration -----

    def start_calibration(self):
        self.state = "calibrate"
        self.calib = {"step": 0, "hold": 0.0, "hist": deque(), "g0": None, "x": None, "hint": ""}

    def update_calibration(self, dt):
        a = self.tilt.accel
        c = self.calib
        if a is None:
            return
        hist = c["hist"]
        hist.append((self.anim, a))
        while self.anim - hist[0][0] > 0.4:
            hist.popleft()
        wobble = max(math.dist(a, h) for _, h in hist)
        step = CALIB_STEPS[c["step"]][0]
        if step == "flat":
            ok = len(hist) > 5 and wobble < 0.02
            c["hint"] = "" if ok else "keep it still..."
        else:
            g0 = c["g0"]
            d = [a[i] - g0[i] for i in range(3)]
            vertical = max(range(3), key=lambda i: abs(g0[i]))
            axis, other = sorted((i for i in range(3) if i != vertical), key=lambda i: -abs(d[i]))
            big = abs(d[axis]) > 0.12          # about 7 degrees
            clean = abs(d[other]) < 0.4 * abs(d[axis])
            sideways = step == "front" and axis == c["x"][0]
            ok = big and clean and not sideways and wobble < 0.035
            c["hint"] = ("hold it there..." if ok else
                         "tilt a bit more" if not big else
                         "that's sideways, tilt it toward you" if sideways else
                         "tilt straight, not diagonally" if not clean else
                         "keep it steady")
        c["hold"] = c["hold"] + dt if ok else 0.0
        if c["hold"] < (1.0 if step == "flat" else 0.6):
            return
        if step == "flat":
            c["g0"] = a
        elif step == "right":
            c["x"] = (axis, 1 if d[axis] > 0 else -1)      # right side down: gravity goes +x
        elif step == "front":
            c["y"] = (axis, -1 if d[axis] > 0 else 1)      # front down: gravity goes -y
        else:
            # Confirm with the new mapping: tilting right must read as "right", not anything else.
            (ix, sx), (iy, sy) = c["x"], c["y"]
            right, away = sx * d[ix], sy * d[iy]
            if right > 0.1 and abs(away) < 0.5 * right:
                self.s["axes"] = [list(c["x"]), list(c["y"])]
                save_settings(self.s)
                self.state = "ready"
                self.say("Directions saved. Put your Mac back down.")
            else:
                c["step"], c["hold"] = 1, 0.0
                hist.clear()
                self.say("That didn't match. Let's redo the tilts.")
            return
        c["step"] += 1
        c["hold"] = 0.0
        hist.clear()

    # ----- simulation -----

    def update(self, dt):
        self.anim += dt
        self.tilt.update(dt)
        if self.state == "calibrate":
            self.gravity = (0.0, 0.0, -1.0)
            self.update_calibration(dt)
            return
        self.gravity = self.tilt.plate_gravity()
        if self.state == "playing":
            self.t += dt
            self.h = PLATE_START - (PLATE_START - PLATE_MIN) * min(1.0, self.t / SHRINK_TIME)
            self.update_wind()
            n = max(1, math.ceil(dt * 240))
            for _ in range(n):
                self.step_ball(dt / n)
            if abs(self.pos[0]) > self.h or abs(self.pos[1]) > self.h:
                self.start_fall()
            else:
                self.update_coins()
        elif self.state == "falling":
            self.fall_t += dt
            self.pos[0] += self.vel[0] * dt
            self.pos[1] += self.vel[1] * dt
            if self.fall_t > FALL_TIME:
                self.state = "over"
        self.update_fx(dt)

    def step_ball(self, dt):
        g = GRAVITY * self.s["sens"]
        ax = g * self.gravity[0] + self.wind[0]
        ay = g * self.gravity[1] + self.wind[1]
        vx, vy = self.vel
        speed = math.hypot(vx, vy)
        if speed < 0.02 and math.hypot(ax, ay) < STATIC_ACCEL:
            self.vel = [0.0, 0.0]
            return
        if speed > 1e-6:
            ax -= ROLL_RESIST * vx / speed
            ay -= ROLL_RESIST * vy / speed
        drag = 1.0 - DAMPING * dt
        vx = (vx + ax * dt) * drag
        vy = (vy + ay * dt) * drag
        self.vel = [vx, vy]
        self.pos[0] += vx * dt
        self.pos[1] += vy * dt

    def start_fall(self):
        self.state = "falling"
        self.fall_t = 0.0
        self.wind = (0.0, 0.0)
        self.gust = None
        if self.score > self.s["best_score"]:
            self.s["best_score"] = self.score
            self.new_best = True
        self.s["best_time"] = max(self.s["best_time"], round(self.t, 1))
        save_settings(self.s)

    def spawn_coin(self):
        lim = self.h * 0.8
        for _ in range(40):
            x, y = random.uniform(-lim, lim), random.uniform(-lim, lim)
            far_from_ball = math.hypot(x - self.pos[0], y - self.pos[1]) > 0.45 * self.h
            far_from_coins = all(math.hypot(x - c["x"], y - c["y"]) > 0.3 for c in self.coins)
            if far_from_ball and far_from_coins:
                break
        self.coins.append({"x": x, "y": y, "phase": random.uniform(0, math.tau)})

    def update_coins(self):
        for c in list(self.coins):
            if math.hypot(c["x"] - self.pos[0], c["y"] - self.pos[1]) < BALL_R + COIN_R:
                self.coins.remove(c)
                self.coin_count += 1
                self.burst(c)
            elif max(abs(c["x"]), abs(c["y"])) > self.h - COIN_R:
                self.coins.remove(c)  # plate shrank out from under it
        while len(self.coins) < (1 if self.t < 40 else 2):
            self.spawn_coin()

    def update_wind(self):
        self.wind = (0.0, 0.0)
        if self.t < WIND_START:
            return
        g = self.gust
        if g is None:
            if self.t >= self.next_gust - GUST_WARNING:
                ang = random.uniform(0, math.tau)
                self.gust = {"start": self.next_gust, "dur": random.uniform(1.2, 2.5),
                             "dir": (math.cos(ang), math.sin(ang)),
                             "strength": min(0.9, 0.3 + 0.006 * (self.t - WIND_START))}
            return
        u = (self.t - g["start"]) / g["dur"]
        if u > 1:
            calm = max(2.5, 6.0 - 0.03 * (self.t - WIND_START))
            self.next_gust = self.t + random.uniform(calm, calm + 4.0)
            self.gust = None
        elif u >= 0:
            env = math.sin(math.pi * u) ** 2
            self.wind = (g["dir"][0] * g["strength"] * env, g["dir"][1] * g["strength"] * env)

    def gust_phase(self):
        """(phase, envelope) where phase is None, 'warn' or 'blow'."""
        g = self.gust
        if not g or self.state != "playing":
            return None, 0.0
        u = (self.t - g["start"]) / g["dur"]
        if u < 0:
            return "warn", 0.0
        return "blow", math.sin(math.pi * clamp(u, 0, 1)) ** 2

    def burst(self, c):
        for _ in range(14):
            a = random.uniform(0, math.tau)
            sp = random.uniform(0.4, 1.2)
            self.particles.append({"pos": [c["x"], c["y"]], "vel": [math.cos(a) * sp, math.sin(a) * sp], "life": 0.5})
        self.popups.append({"pos": [c["x"], c["y"] + 0.12], "life": 0.9, "text": f"+{COIN_POINTS}"})

    def update_fx(self, dt):
        phase, env = self.gust_phase()
        if phase == "blow":
            dx, dy = self.gust["dir"]
            self.streak_acc += 45 * env * dt
            while self.streak_acc >= 1:
                self.streak_acc -= 1
                sp = random.uniform(2.2, 3.4)
                # start upwind, spread across the plate
                off = random.uniform(-1.3, 1.3)
                self.streaks.append({"pos": [-dx * 1.4 - dy * off, -dy * 1.4 + dx * off],
                                     "vel": [dx * sp, dy * sp], "life": 1.0})
        for s in self.streaks:
            s["pos"][0] += s["vel"][0] * dt
            s["pos"][1] += s["vel"][1] * dt
            s["life"] -= dt
        for p in self.particles:
            p["pos"][0] += p["vel"][0] * dt
            p["pos"][1] += p["vel"][1] * dt
            p["life"] -= dt
        for p in self.popups:
            p["pos"][1] += 0.25 * dt
            p["life"] -= dt
        self.streaks = [s for s in self.streaks if s["life"] > 0]
        self.particles = [p for p in self.particles if p["life"] > 0]
        self.popups = [p for p in self.popups if p["life"] > 0]

    # ----- tilt readout -----

    def tilt_degrees(self):
        gx, gy = self.gravity[0], self.gravity[1]
        return math.degrees(math.asin(clamp(gx, -1, 1))), math.degrees(math.asin(clamp(gy, -1, 1)))

    # ----- drawing -----

    def render(self, cols, rows):
        scr = Screen(cols, rows)
        panel_w = 34
        arena_rows = min(rows - 3, 40, (cols - panel_w - 3) // 2)
        if arena_rows < 16 or rows < 22:
            scr.center(rows // 2, 0, cols, "Make the terminal bigger (at least 76 x 22)", WARN)
            return scr
        arena_cols = arena_rows * 2
        left = max(0, (cols - arena_cols - 3 - panel_w) // 2)
        top = 1
        scale = (arena_cols / 2 - 2) / PLATE_START  # pixels per plate unit (pixels are square)

        cv = Canvas(arena_cols, arena_rows * 2)
        self.draw_arena(cv, scale)
        scr.canvas(top, left, cv)
        scr.center(0, left, arena_cols, "▲ back (hinge side)", DIM)
        scr.center(top + arena_rows, left, arena_cols, "▼ front (your side)", DIM)
        self.draw_popups(scr, top, left, cv, scale)
        self.draw_box(scr, top, left, arena_cols, arena_rows)
        self.draw_panel(scr, top, left + arena_cols + 3, panel_w, rows - 2)
        scr.text(rows - 1, 1, self.tilt.status, DIM)
        if self.toast and self.anim < self.toast[1]:
            scr.text(rows - 1, cols - len(self.toast[0]) - 2, self.toast[0], WARN, bold=True)
        return scr

    def to_px(self, cv, scale, x, y):
        return cv.w / 2 + x * scale, cv.h / 2 - y * scale

    def draw_arena(self, cv, scale):
        h, hp = self.h, self.h * scale
        gx, gy = self.gravity[0], self.gravity[1]
        cx, cy = cv.w / 2, cv.h / 2
        bx, by = self.pos
        playing = self.state == "playing"
        gaps = {"r": h - bx, "l": bx + h, "b": h - by, "f": by + h}

        # plate, shaded by height: the low (downhill) side is darker
        for py in range(cv.h):
            y = (cy - py - 0.5) / scale
            if abs(y) > h:
                continue
            row = cv.pix[py]
            for px in range(cv.w):
                x = (px + 0.5 - cx) / scale
                if abs(x) > h:
                    continue
                # height bands, like contour lines: tanh keeps steep tilts from turning into a hard split
                band = int(round(BAND_LEVELS * math.tanh(4.0 * -(gx * x + gy * y) / h)))
                ex, ey = hp - abs(x * scale), hp - abs(y * scale)
                if min(ex, ey) < 1.0:
                    side = ("r" if x > 0 else "l") if ex < ey else ("b" if y > 0 else "f")
                    danger = clamp(1 - gaps[side] / 0.3, 0, 1) if playing else 0.0
                    row[px] = RIM_LEVELS[int(round(danger * 3))]
                elif abs(math.hypot(x, y) - 0.15) * scale < 0.5:
                    row[px] = RING  # centre target ring
                else:
                    row[px] = PLATE_BANDS[band + BAND_LEVELS]

        for c in self.coins:
            if self.state in ("playing", "falling", "over"):
                px, py = self.to_px(cv, scale, c["x"], c["y"])
                cv.disc(px + 0.7, py + 0.7, COIN_R * scale, SHADOW)
                squash = max(0.3, abs(math.cos(self.anim * 3 + c["phase"])))
                cv.disc(px, py, COIN_R * scale, GOLD_DARK, squash)
                cv.disc(px, py, COIN_R * scale * 0.7, GOLD, squash)

        if self.state in ("ready", "playing"):
            mag = math.hypot(*self.tilt_degrees())
            if mag > LEVEL_DEG:  # arrow: where the ball will roll
                n = math.hypot(gx, gy) or 1.0
                ux, uy = gx / n, gy / n
                start = BALL_R + 1.2 / scale
                length = start + (3 + 7 * clamp(mag / GUIDE_RANGE, 0, 1)) / scale
                tip = None
                steps = max(2, int((length - start) * scale * 1.5))
                for i in range(steps + 1):
                    d = start + (length - start) * i / steps
                    tip = self.to_px(cv, scale, bx + ux * d, by + uy * d)
                    cv.put(int(tip[0]), int(tip[1]), ARROW)
                # arrowhead: two pixels swept back from the tip
                for side in (-1, 1):
                    hx = -ux * 1.6 + side * -uy * 1.4
                    hy = -uy * 1.6 + side * ux * 1.4
                    cv.put(int(tip[0] + hx), int(tip[1] - hy), ARROW)

        for s in self.streaks:
            n = math.hypot(*s["vel"])
            for i in range(1 + int(3 * clamp(s["life"] / 0.4, 0, 1))):
                px, py = self.to_px(cv, scale, s["pos"][0] - s["vel"][0] / n * i / scale * 1.2,
                                    s["pos"][1] - s["vel"][1] / n * i / scale * 1.2)
                cv.put(int(px), int(py), STREAK if i == 0 else STREAK_DIM)

        if self.state in ("ready", "playing", "falling"):
            u = clamp(self.fall_t / FALL_TIME, 0, 1) if self.state == "falling" else 0.0
            px, py = self.to_px(cv, scale, bx, by)
            r = BALL_R * scale * (1 - 0.75 * u)
            if u == 0:
                cv.disc(px + 0.9, py + 0.9, r, SHADOW)
            self.draw_ball(cv, px, py, r, int(u * 4))

        for p in self.particles:
            px, py = self.to_px(cv, scale, *p["pos"])
            cv.put(int(px), int(py), GOLD)

    def draw_ball(self, cv, cx, cy, r, dim):
        """Shaded with a handful of fixed colours (see the palette note at the top)."""
        r = max(r, 0.6)
        for y in range(int(cy - r - 1), int(cy + r + 2)):
            for x in range(int(cx - r - 1), int(cx + r + 2)):
                dx, dy = (x + 0.5 - cx) / r, (y + 0.5 - cy) / r
                d = math.hypot(dx, dy)
                if d > 1.0 + 0.35 / r:
                    continue
                nz = math.sqrt(max(0.0, 1 - min(1.0, d) ** 2))
                lam = clamp(-0.5 * dx - 0.5 * dy + 0.7 * nz, 0, 1)
                idx = 5 if lam > 0.93 else int(lam * 4.99)
                cv.put(x, y, BALL_SHADES[max(0, idx - dim)])

    def draw_popups(self, scr, top, left, cv, scale):
        for p in self.popups:
            px, py = self.to_px(cv, scale, *p["pos"])
            row, col = top + int(py) // 2, left + int(px) - len(p["text"]) // 2
            for i, ch in enumerate(p["text"]):
                scr.text(row, col + i, ch, GOLD, scr.bg_at(row, col + i), bold=True)

    def draw_box(self, scr, top, left, width, height):
        lines = []  # (text, colour, bold)
        if self.state == "calibrate":
            c = self.calib
            name, msg = CALIB_STEPS[c["step"]]
            need = 1.0 if name == "flat" else 0.6
            filled = int(16 * clamp(c["hold"] / need, 0, 1))
            lines = [(f"DIRECTION CHECK  ·  step {c['step'] + 1} of {len(CALIB_STEPS)}", WARN, True), ("", TEXT, False),
                     (msg[0], TEXT, True), (msg[1], TEXT, False), ("", TEXT, False),
                     ("█" * filled + "░" * (16 - filled), GOOD, False), (c["hint"], DIM, False),
                     ("", TEXT, False), ("Esc skip", DIM, False)]
        elif self.state == "ready" and self.tilt.imu:
            lines = [("Hold your Mac the way you want to play,", TEXT, False),
                     ("then press SPACE. That position = level.", GOLD, True),
                     ("darker side = downhill · keep the dot green", DIM, False),
                     ("Plate tilts the wrong way? Press C.", DIM, False)]
        elif self.state == "ready":
            lines = [("Tilt the plate with the arrow keys.", TEXT, False),
                     ("Press SPACE to start.", GOLD, True),
                     ("darker side = downhill · keep the dot green", DIM, False),
                     ("(no MacBook motion sensor found)", DIM, False)]
        elif self.state == "over":
            lines = [("THE BALL FELL!", BAD, True),
                     (f"time {self.t:.1f}s  ·  coins {self.coin_count}  ·  score {self.score}", TEXT, False)]
            if self.new_best:
                lines.append(("NEW BEST!", GOLD, True))
            lines += [("", TEXT, False), ("SPACE to play again", GOLD, True)]
        if not lines:
            return
        w = min(width - 2, max(len(t) for t, _, _ in lines) + 6)
        r0 = top + 1 if self.state == "ready" else top + (height - len(lines) - 2) // 2
        c0 = left + (width - w) // 2
        for i in range(len(lines) + 2):
            scr.text(r0 + i, c0, " " * w, TEXT, BOX_BG)
        for i, (t, color, bold) in enumerate(lines):
            scr.center(r0 + 1 + i, c0, w, t, color, BOX_BG, bold)

    def draw_panel(self, scr, top, col, width, height):
        r = top
        scr.text(r, col, "BALL BALANCE", TEXT, bold=True)
        r += 2
        for label, value in (("TIME", f"{self.t:.1f}s"), ("COINS", str(self.coin_count)),
                             ("SCORE", str(self.score)), ("BEST", str(self.s["best_score"]))):
            scr.text(r, col, f"{label:<7}", DIM)
            scr.text(r, col + 7, value, GOLD if label == "COINS" else TEXT, bold=True)
            r += 1
        r += 1

        guide_rows = int(clamp(height - 17, 6, 11))
        scr.text(r, col, "TILT GUIDE", DIM, bold=True)
        r += 1
        deg_x, deg_y = self.tilt_degrees()
        mag = math.hypot(deg_x, deg_y)
        state_color = GOOD if mag < LEVEL_DEG else WARN if mag < 4 else BAD
        cv = Canvas(guide_rows * 2, guide_rows * 2)
        self.draw_guide(cv, deg_x, deg_y, mag, state_color)
        scr.canvas(r, col + 1, cv)
        r += guide_rows
        if self.state == "calibrate":
            scr.text(r, col, "waiting for direction check", DIM)
        elif mag < LEVEL_DEG:
            scr.text(r, col, "● LEVEL", GOOD, bold=True)
            scr.text(r, col + 8, "ball stays still", DIM)
        else:
            arrow, name = direction(self.gravity[0], self.gravity[1])
            scr.text(r, col, f"rolling {arrow}  {mag:.1f}°", state_color, bold=True)
            scr.text(r + 1, col, f"raise the {name}", TEXT)
        r += 3

        phase, _ = self.gust_phase()
        if phase:
            arrow, _ = direction(*self.gust["dir"])
            if phase == "blow":
                scr.text(r, col, f"GUST {arrow}  push back!", STREAK, bold=True)
            elif int(self.anim * 5) % 2 == 0:
                scr.text(r, col, f"GUST {arrow}  incoming", STREAK, bold=True)
        r += 2
        keys = "SPACE start   C direction check" if self.tilt.imu else "SPACE start   arrows tilt"
        scr.text(min(r, top + height - 2), col, keys, DIM)
        scr.text(min(r + 1, top + height - 1), col, f"[ ] sensitivity {self.s['sens']:.1f}x   Q quit", DIM)

    def draw_guide(self, cv, deg_x, deg_y, mag, dot_color):
        """Spirit-level style: dot sits where the ball will roll; centre = level.
        Radius is sqrt-scaled so small tilts near level are easy to see."""
        c = cv.w / 2
        R = cv.w / 2 - 0.7
        level_r = R * math.sqrt(LEVEL_DEG / GUIDE_RANGE)
        mid_r = R * math.sqrt(4 / GUIDE_RANGE)
        is_level = mag < LEVEL_DEG and self.state != "calibrate"
        for py in range(cv.h):
            for px in range(cv.w):
                dx, dy = px + 0.5 - c, py + 0.5 - c
                d = math.hypot(dx, dy)
                if d > R + 0.6:
                    continue
                col = (22, 27, 48)
                if d < level_r:
                    col = (50, 150, 80) if is_level else (30, 66, 44)
                elif abs(d - mid_r) < 0.5:
                    col = (52, 60, 88)
                if abs(d - R) < 0.6:
                    col = (80, 90, 125)
                elif abs(dx) < 0.5 or abs(dy) < 0.5:
                    col = mix(col, (90, 100, 135), 0.4)
                cv.pix[py][px] = col
        if self.state == "calibrate":
            return
        r = R * math.sqrt(min(1.0, mag / GUIDE_RANGE))
        ang = math.atan2(deg_y, deg_x)
        tx, ty = c + r * math.cos(ang), c - r * math.sin(ang)
        cv.disc(tx, ty, max(1.6, cv.w / 13), dot_color)


# ---------- terminal plumbing ----------

class Terminal:
    """Raw keyboard input and flicker-free redraws: macOS, Linux and Windows 10+."""

    def __init__(self):
        self.truecolor = (os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit")
                          or "WT_SESSION" in os.environ)  # Windows Terminal
        self.prev = []
        self.size = None
        self.fd = None
        if not WINDOWS:
            # stdin may be a pipe (e.g. started with `curl ... | sh`): read keys from the terminal itself
            self.fd = sys.stdin.fileno() if sys.stdin.isatty() else os.open("/dev/tty", os.O_RDONLY)

    def __enter__(self):
        if WINDOWS:
            k32 = ctypes.windll.kernel32
            k32.SetConsoleOutputCP(65001)
            handle, mode = k32.GetStdHandle(-11), ctypes.c_uint32()
            if k32.GetConsoleMode(handle, ctypes.byref(mode)):
                k32.SetConsoleMode(handle, mode.value | 0x0004)  # turn on ANSI escape codes
            sys.stdout.reconfigure(encoding="utf-8")
        else:
            self.saved = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        self.write("\x1b[?1049h\x1b[?25l\x1b[2J")
        return self

    def __exit__(self, *exc):
        self.write("\x1b[0m\x1b[2J\x1b[?25h\x1b[?1049l")
        if not WINDOWS:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)

    def write(self, s):
        sys.stdout.write(s)
        sys.stdout.flush()

    def keys(self):
        return self._windows_keys() if WINDOWS else self._posix_keys()

    def _windows_keys(self):
        out = []
        while msvcrt.kbhit():
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):  # arrow keys arrive as a prefix + code
                out.append({"H": "up", "P": "down", "K": "left", "M": "right"}.get(msvcrt.getwch(), ""))
            elif ch == "\x03":
                raise KeyboardInterrupt
            else:
                out.append("esc" if ch == "\x1b" else ch.lower())
        return [k for k in out if k]

    def _posix_keys(self):
        data = b""
        while select.select([self.fd], [], [], 0)[0]:
            chunk = os.read(self.fd, 256)
            if not chunk:
                break
            data += chunk
        out, i = [], 0
        arrows = {b"A": "up", b"B": "down", b"C": "right", b"D": "left"}
        while i < len(data):
            if data[i:i + 1] == b"\x1b":
                if data[i + 1:i + 2] in (b"[", b"O") and data[i + 2:i + 3] in arrows:
                    out.append(arrows[data[i + 2:i + 3]])
                    i += 3
                    continue
                out.append("esc")
            else:
                out.append(chr(data[i]).lower())
            i += 1
        return out

    def draw(self, scr):
        if (scr.cols, scr.rows) != self.size:
            self.size = (scr.cols, scr.rows)
            self.prev = []
            self.write("\x1b[0m\x1b[2J")
        lines = scr.lines(self.truecolor)
        buf = []
        for r, line in enumerate(lines):
            if r >= len(self.prev) or self.prev[r] != line:
                buf.append(f"\x1b[{r + 1};1H{line}")
        if buf:
            self.write("".join(buf) + "\x1b[0m")
        self.prev = lines


def main():
    if "-h" in sys.argv or "--help" in sys.argv:
        print(__doc__.strip())
        return
    try:
        if not sys.stdout.isatty():
            raise OSError
        term = Terminal()
    except OSError:
        print("Ball Balance needs to run in a terminal window.")
        return
    settings = load_settings()
    tilt = TiltInput(settings, use_sensor="--keys" not in sys.argv)
    game = Game(tilt, settings)
    try:
        with term:
            last = time.perf_counter()
            while True:
                now = time.perf_counter()
                dt, last = min(now - last, 0.1), now
                if any(game.handle(k) is False for k in term.keys()):
                    break
                game.update(dt)
                cols, rows = shutil.get_terminal_size()
                term.draw(game.render(cols, rows))
                time.sleep(max(0.0, 1 / FPS - (time.perf_counter() - now)))
    except KeyboardInterrupt:
        pass
    finally:
        tilt.close()
    if game.s["best_score"]:
        print(f"Best score: {game.s['best_score']}  (longest run {game.s['best_time']}s)")


if __name__ == "__main__":
    main()
