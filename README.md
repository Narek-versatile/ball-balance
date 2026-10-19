# Ball Balance

A terminal game: tilt your MacBook to keep a ball on a plate. It reads the
MacBook's built-in accelerometer and gyroscope, which macOS doesn't officially
expose. On any other computer you play with the arrow keys.

## Play

```bash
curl -fsSL https://raw.githubusercontent.com/Narek-versatile/ball-balance/main/play.sh | sh
```

This downloads the game into `~/.ball-balance` and starts it. Run it again to
update. The only thing it needs is Python 3.8 or newer (no packages).

To force arrow keys: `curl -fsSL https://raw.githubusercontent.com/Narek-versatile/ball-balance/main/play.sh | sh -s -- --keys`

From a clone: `python3 ball_balance.py`

## How to play

1. Hold your Mac the way you want to play and press **Space**. That position
   counts as level.
2. Keep the ball on the plate. You score 1 point per second and 10 per coin.
3. The plate shrinks over two minutes, and from 15 seconds wind gusts push the
   ball (you get a warning first).

How to read the tilt:

- **Plate shading:** the darker side is downhill. Bands run across the slope,
  and more bands means a steeper tilt.
- **Arrow on the ball:** where it's about to roll.
- **Tilt guide:** works like a spirit level. Keep the dot in the green middle
  and the ball stays still. The text under it says which edge to raise.

| Key | Action |
| --- | --- |
| Space | Start / play again |
| C | Direction check (if the plate tilts the wrong way) |
| `[` `]` | Sensitivity |
| Arrow keys | Tilt (when there's no motion sensor) |
| Q | Quit |

Your best score and settings are saved in `~/.ball_balance.json`.

## Where it works

| Computer | Control |
| --- | --- |
| Apple Silicon MacBook (tested on an M4 MacBook Air) | Tilt the Mac |
| Other Macs, Linux | Arrow keys |
| Windows 10+ (Windows Terminal) | Arrow keys. The one-line command needs a Unix shell, so run `python ball_balance.py` from a download of this repo instead. Untested. |

The terminal should be at least 76×22. Colours are 24-bit when the terminal
says it supports them (`COLORTERM=truecolor`), otherwise 256-colour.

## How it works

- `mac_imu.py` reads the IMU that sits behind Apple's Sensor Processing Unit.
  It shows up as vendor HID devices (usage page `0xFF00`: usage 3 is the
  accelerometer, usage 9 the gyroscope). The script wakes them through
  `AppleSPUHIDDriver` properties and reads raw reports with IOKit through
  `ctypes`. A complementary filter combines the two sensors: the gyroscope for
  fast, smooth response and the accelerometer so it never drifts. How the
  gyroscope's axes line up with the accelerometer's is learned while you play,
  using a least-squares fit.
- `ball_balance.py` is the game: rolling-ball physics, plus drawing with
  half-block characters (`▀`, two square pixels per character). It sticks to a
  small fixed colour palette, because endless colour gradients overflow some
  terminals' glyph caches and leave stray pixels behind.

## Troubleshooting

- **The plate tilts the wrong way:** press **C** and follow the direction check.
- **"motion sensor unavailable" on a MacBook:** some models may need
  `sudo python3 ball_balance.py`. Intel MacBooks don't have this sensor.
- **"Make the terminal bigger":** enlarge the window or zoom out.
