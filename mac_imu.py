"""Read the undocumented IMU (accelerometer + gyroscope) on Apple Silicon MacBooks.

The sensors live behind the SPU (Sensor Processing Unit) as vendor HID devices
(usage page 0xFF00, usage 3 = accelerometer, usage 9 = gyroscope). They are
asleep by default; we wake them by setting properties on AppleSPUHIDDriver,
then read raw HID input reports with IOKit via ctypes. Works without sudo on
an M4 MacBook Air; other models may need it.

Run directly to print live readings:  sudo python3 mac_imu.py
"""

import ctypes
import math
import struct
import sys
import threading
import time

_vp = ctypes.c_void_p
_u32 = ctypes.c_uint32
_REPORT_CB = ctypes.CFUNCTYPE(None, _vp, ctypes.c_int, _vp, ctypes.c_int, _u32,
                              ctypes.POINTER(ctypes.c_uint8), ctypes.c_long)
_kCFStringEncodingUTF8 = 0x08000100
_kCFNumberSInt32Type = 3
_iokit = _cf = _kCFRunLoopDefaultMode = None


def _load_frameworks():
    """Bind the IOKit / CoreFoundation calls we need. Only possible on macOS."""
    global _iokit, _cf, _kCFRunLoopDefaultMode
    _iokit = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/IOKit.framework/IOKit")
    _cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")

    _cf.CFStringCreateWithCString.restype = _vp
    _cf.CFStringCreateWithCString.argtypes = [_vp, ctypes.c_char_p, _u32]
    _cf.CFNumberCreate.restype = _vp
    _cf.CFNumberCreate.argtypes = [_vp, ctypes.c_int, _vp]
    _cf.CFNumberGetValue.restype = ctypes.c_bool
    _cf.CFNumberGetValue.argtypes = [_vp, ctypes.c_int, _vp]
    _cf.CFRelease.argtypes = [_vp]
    _cf.CFRunLoopGetCurrent.restype = _vp
    _cf.CFRunLoopRunInMode.restype = ctypes.c_int32
    _cf.CFRunLoopRunInMode.argtypes = [_vp, ctypes.c_double, ctypes.c_bool]

    _iokit.IOServiceMatching.restype = _vp
    _iokit.IOServiceMatching.argtypes = [ctypes.c_char_p]
    _iokit.IOServiceGetMatchingServices.restype = ctypes.c_int
    _iokit.IOServiceGetMatchingServices.argtypes = [_u32, _vp, ctypes.POINTER(_u32)]
    _iokit.IOIteratorNext.restype = _u32
    _iokit.IOIteratorNext.argtypes = [_u32]
    _iokit.IOObjectRelease.argtypes = [_u32]
    _iokit.IORegistryEntryCreateCFProperty.restype = _vp
    _iokit.IORegistryEntryCreateCFProperty.argtypes = [_u32, _vp, _vp, _u32]
    _iokit.IORegistryEntrySetCFProperty.restype = ctypes.c_int
    _iokit.IORegistryEntrySetCFProperty.argtypes = [_u32, _vp, _vp]
    _iokit.IOHIDDeviceCreate.restype = _vp
    _iokit.IOHIDDeviceCreate.argtypes = [_vp, _u32]
    _iokit.IOHIDDeviceOpen.restype = ctypes.c_int
    _iokit.IOHIDDeviceOpen.argtypes = [_vp, _u32]
    _iokit.IOHIDDeviceScheduleWithRunLoop.argtypes = [_vp, _vp, _vp]
    _iokit.IOHIDDeviceRegisterInputReportCallback.argtypes = [_vp, _vp, ctypes.c_long, _REPORT_CB, _vp]

    _kCFRunLoopDefaultMode = _vp.in_dll(_cf, "kCFRunLoopDefaultMode")


if sys.platform == "darwin":
    try:
        _load_frameworks()
    except (OSError, AttributeError, ValueError):
        _iokit = _cf = None

VENDOR_PAGE = 0xFF00
USAGE_ACCEL = 3
USAGE_GYRO = 9
SCALE = 65536.0  # raw int32 -> g (accel) or deg/s (gyro)


def _cfstr(s):
    return _cf.CFStringCreateWithCString(None, s.encode(), _kCFStringEncodingUTF8)


def _cfint(v):
    val = ctypes.c_int32(v)
    return _cf.CFNumberCreate(None, _kCFNumberSInt32Type, ctypes.byref(val))


def _int_prop(service, key):
    k = _cfstr(key)
    ref = _iokit.IORegistryEntryCreateCFProperty(service, k, None, 0)
    _cf.CFRelease(k)
    if not ref:
        return None
    out = ctypes.c_int32()
    _cf.CFNumberGetValue(ref, _kCFNumberSInt32Type, ctypes.byref(out))
    _cf.CFRelease(ref)
    return out.value


def _services(class_name):
    it = _u32()
    kr = _iokit.IOServiceGetMatchingServices(0, _iokit.IOServiceMatching(class_name.encode()), ctypes.byref(it))
    if kr != 0:
        return
    while True:
        svc = _iokit.IOIteratorNext(it.value)
        if not svc:
            break
        yield svc
    _iokit.IOObjectRelease(it.value)


class IMUError(RuntimeError):
    pass


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _unit(v):
    n = math.sqrt(_dot(v, v)) or 1.0
    return (v[0] / n, v[1] / n, v[2] / n)


def _matvec(m, v):
    return tuple(m[i][0] * v[0] + m[i][1] * v[1] + m[i][2] * v[2] for i in range(3))


def _solve(A, b):
    """Solve A x = b by Gaussian elimination with partial pivoting (A is modified)."""
    n = len(b)
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(A[r][c]))
        if abs(A[p][c]) < 1e-15:
            return None
        A[c], A[p] = A[p], A[c]
        b[c], b[p] = b[p], b[c]
        for r in range(c + 1, n):
            f = A[r][c] / A[c][c]
            if f:
                for k in range(c, n):
                    A[r][k] -= f * A[c][k]
                b[r] -= f * b[c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (b[r] - sum(A[r][k] * x[k] for k in range(r + 1, n))) / A[r][r]
    return x


class MacIMU:
    """Background reader.

    `accel` is (x, y, z) in g, `gyro` is (x, y, z) in deg/s, and `gravity` is a
    fused unit vector (device frame) from a complementary filter: the gyro
    rotates the estimate for fast, smooth response, the accelerometer slowly
    pulls it back so it never drifts. How the gyro's axes line up with the
    accelerometer's is learned on the fly; until then (or if the two never
    agree) the filter uses the accelerometer only.
    """

    TAU = 0.25           # s, how fast the accelerometer corrects gyro drift
    LEARN_WINDOW = 0.1   # s, window for comparing gyro-predicted vs measured tilt change

    def __init__(self, interval_us=10000, debug=False):
        self.interval_us = interval_us
        self.debug = debug
        self.accel = None
        self.gyro = None
        self.gravity = None
        self.accel_count = 0
        self.gyro_count = 0
        self.gyro_matrix = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
        self.fit_quality = 0.0
        self.fusion = "learning"  # -> "gyro+accel" or "accel only"
        self.raw = {}
        self._lock = threading.Lock()
        self._callbacks = []  # keep ctypes callbacks + buffers alive
        self._woken = []
        self._last_gyro_t = None
        self._win_start = None    # (t, accel) at start of learning window
        self._win_rot = [0.0, 0.0, 0.0]
        self._N = [[0.0] * 9 for _ in range(9)]
        self._b = [0.0] * 9
        self._yy = 0.0
        self._nwin = 0
        self._stop = False
        self._ready = threading.Event()
        self._error = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self, timeout=2.0):
        if _iokit is None:
            raise IMUError("The MacBook motion sensor is only available on macOS.")
        self._thread.start()
        self._ready.wait(timeout)
        if self._error:
            raise IMUError(self._error)
        return self

    def stop(self):
        self._stop = True
        if not self._woken:
            return
        off = _cfint(0)
        key = _cfstr("SensorPropertyReportingState")
        for svc in self._woken:  # put sensors back to sleep if they were asleep before
            _iokit.IORegistryEntrySetCFProperty(svc, key, off)

    def read(self):
        with self._lock:
            return self.accel, self.gyro

    def gravity_vector(self):
        with self._lock:
            return self.gravity

    def _wake_drivers(self):
        val = _cfint(self.interval_us)
        one = _cfint(1)
        keys = {k: _cfstr(k) for k in ("SensorPropertyReportingState", "SensorPropertyPowerState", "ReportInterval")}
        for svc in _services("AppleSPUHIDDriver"):
            if _int_prop(svc, "PrimaryUsagePage") == VENDOR_PAGE and _int_prop(svc, "PrimaryUsage") in (USAGE_ACCEL, USAGE_GYRO):
                if not _int_prop(svc, "ReportInterval"):
                    self._woken.append(svc)
                _iokit.IORegistryEntrySetCFProperty(svc, keys["SensorPropertyReportingState"], one)
                _iokit.IORegistryEntrySetCFProperty(svc, keys["SensorPropertyPowerState"], one)
                _iokit.IORegistryEntrySetCFProperty(svc, keys["ReportInterval"], val)
            else:
                _iokit.IOObjectRelease(svc)

    def _on_gyro(self, w_deg, t):
        self.gyro = w_deg
        g = self.gravity
        if g is not None and self._last_gyro_t is not None:
            dt = min(t - self._last_gyro_t, 0.05)
            rot = tuple(c * math.radians(dt) for c in w_deg)  # rotation this sample, raw gyro axes (rad)
            for i in range(3):
                self._win_rot[i] += rot[i]
            # Only steer with the gyro once its axes are known, so it never lurches the wrong way.
            if self.fusion == "gyro+accel":
                dg = _cross(g, _matvec(self.gyro_matrix, rot))  # world-fixed vector in a turning body: dv = v x dθ
                self.gravity = _unit((g[0] + dg[0], g[1] + dg[1], g[2] + dg[2]))
        self._last_gyro_t = t

    def _on_accel(self, a, t):
        self.accel = a
        n = math.sqrt(_dot(a, a))
        if n < 1e-6:
            return
        u = (a[0] / n, a[1] / n, a[2] / n)
        self._learn_gyro(u, t)
        g = self.gravity
        if g is None:
            self.gravity = u
            return
        # Trust the accelerometer less while the Mac is being jerked around.
        alpha = (0.01 / self.TAU) * (1.0 if abs(n - 1.0) < 0.15 else 0.2)
        if self.fusion != "gyro+accel":
            alpha *= 2.0
        self.gravity = _unit(tuple(g[i] + (u[i] - g[i]) * alpha for i in range(3)))

    def _learn_gyro(self, u, t):
        """Learn the 3x3 matrix A mapping raw gyro axes onto accelerometer axes.

        Over each short window the measured change in the gravity direction should
        equal g x (A . rotation). That is linear in A's 9 entries, so we keep
        least-squares normal equations and re-solve now and then. This copes with
        any axis order, sign or scale the gyro happens to use.
        """
        if self._win_start is None:
            self._win_start = (t, u)
            self._win_rot = [0.0, 0.0, 0.0]
            return
        t0, g0 = self._win_start
        if t - t0 < self.LEARN_WINDOW:
            return
        rot = self._win_rot
        if _dot(rot, rot) > 0.02 ** 2:  # only learn while actually rotating
            meas = (u[0] - g0[0], u[1] - g0[1], u[2] - g0[2])
            cols = [_cross(g0, e) for e in ((1, 0, 0), (0, 1, 0), (0, 0, 1))]
            for i in range(3):
                x = [cols[k][i] * rot[m] for k in range(3) for m in range(3)]
                for p in range(9):
                    self._b[p] += x[p] * meas[i]
                    row = self._N[p]
                    for q in range(9):
                        row[q] += x[p] * x[q]
                self._yy += meas[i] * meas[i]
            self._nwin += 1
            if self._nwin >= 12 and self._nwin % 4 == 0:
                self._fit_gyro()
            if self._nwin > 300:  # slowly forget so it keeps adapting
                self._N = [[v * 0.5 for v in row] for row in self._N]
                self._b = [v * 0.5 for v in self._b]
                self._yy *= 0.5
                self._nwin //= 2
        self._win_start = (t, u)
        self._win_rot = [0.0, 0.0, 0.0]

    def _fit_gyro(self):
        N = [row[:] for row in self._N]
        lam = 1e-3 * sum(N[i][i] for i in range(9)) / 9 + 1e-12  # pulls unobservable entries to 0
        for i in range(9):
            N[i][i] += lam
        a = _solve(N, self._b[:])
        if a is None or self._yy <= 0:
            return
        sse = self._yy - 2 * sum(a[i] * self._b[i] for i in range(9)) + \
            sum(a[i] * self._N[i][j] * a[j] for i in range(9) for j in range(9))
        self.fit_quality = 1 - sse / self._yy
        if self.fit_quality > 0.6:
            self.gyro_matrix = (tuple(a[0:3]), tuple(a[3:6]), tuple(a[6:9]))
            self.fusion = "gyro+accel"
        elif self._nwin >= 40 and self.fit_quality < 0.3:
            self.fusion = "accel only"

    def _make_cb(self, usage):
        def cb(ctx, result, sender, rtype, rid, report, length):
            data = bytes(report[:length])
            if self.debug:
                self.raw[usage] = data
            if length < 18:
                return
            x, y, z = struct.unpack_from("<iii", data, 6)
            vec = (x / SCALE, y / SCALE, z / SCALE)
            t = time.perf_counter()
            with self._lock:
                if usage == USAGE_ACCEL:
                    self.accel_count += 1
                    self._on_accel(vec, t)
                else:
                    self.gyro_count += 1
                    self._on_gyro(vec, t)
        return _REPORT_CB(cb)

    def _run(self):
        try:
            self._wake_drivers()
            loop = _cf.CFRunLoopGetCurrent()
            opened = 0
            failures = []
            for svc in _services("AppleSPUHIDDevice"):
                page, usage = _int_prop(svc, "PrimaryUsagePage"), _int_prop(svc, "PrimaryUsage")
                if page == VENDOR_PAGE and usage in (USAGE_ACCEL, USAGE_GYRO):
                    dev = _iokit.IOHIDDeviceCreate(None, svc)
                    kr = _iokit.IOHIDDeviceOpen(dev, 0) if dev else -1
                    if kr != 0:
                        failures.append(f"usage {usage}: IOHIDDeviceOpen=0x{kr & 0xffffffff:08x}")
                    else:
                        buf = (ctypes.c_uint8 * 4096)()
                        cb = self._make_cb(usage)
                        self._callbacks.append((dev, buf, cb))
                        _iokit.IOHIDDeviceRegisterInputReportCallback(dev, buf, len(buf), cb, None)
                        _iokit.IOHIDDeviceScheduleWithRunLoop(dev, loop, _kCFRunLoopDefaultMode)
                        opened += 1
                _iokit.IOObjectRelease(svc)
            if opened == 0:
                self._error = "No motion sensor could be opened. " + ("; ".join(failures) or "Sensor not found.") + \
                              " Try running with sudo."
                self._ready.set()
                return
            self._ready.set()
            while not self._stop:
                _cf.CFRunLoopRunInMode(_kCFRunLoopDefaultMode, 0.05, False)
        except Exception as e:  # surface errors to start()
            self._error = str(e)
            self._ready.set()


if __name__ == "__main__":
    imu = MacIMU(debug=True).start()
    t0 = time.time()
    try:
        while True:
            time.sleep(0.2)
            a, g = imu.read()
            raw_a = imu.raw.get(USAGE_ACCEL, b"").hex()
            print(f"{time.time()-t0:5.1f}s  grav={imu.gravity and tuple(round(v, 3) for v in imu.gravity)} [{imu.fusion}] accel={a and tuple(round(v, 3) for v in a)} "
                  f"gyro={g and tuple(round(v, 1) for v in g)}  n={imu.accel_count}/{imu.gyro_count}  raw={raw_a}")
    except KeyboardInterrupt:
        pass
