# painel-cameras-rpi

[Português](README.pt-BR.md) · English

Turns an old Raspberry Pi (tested on a **Pi 1 Model B, ARMv6, ~175 MB of RAM**) into an IP-camera panel
for an HDMI TV/monitor: it shows **rotating snapshots**, saves the photos to disk and deletes the old ones
automatically. No desktop, no browser, no Docker: a Python script that draws straight to the framebuffer,
with `ffmpeg` fetching the frames.

```
┌──────────────────────────────────────────────────────────────┐
│ Câmera 1        atualizado há 4s            14/11  22:26:38 │  ← bar: name, image age, clock
├──────────────────────────────────────────────────────────────┤
│                                                              │
│                  latest snapshot from the camera             │
│                  (the image is not video)                    │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

> The on-screen text ("atualizado há 4s" = "updated 4s ago", "SEM SINAL" = "no signal") is currently in
> Portuguese. Configuration keys and command-line flags (such as `--verificar`, "verify") are in Portuguese too.

## What it does

- **Rotating RTSP snapshots** across one or more cameras (a new image every ~8–15 s on a Pi 1).
- **Top bar** with the camera name, "updated Ns ago" and a clock. Shows **NO SIGNAL** if the image gets stale (60 s).
- **Saves the photos** to `/var/lib/painel/<camera>/<YYYY-MM-DD>/<HH-MM-SS>.jpg`, with **age-based retention**
  (default 7 days) and a **size cap** (default 8 GB, oldest photos deleted first).
  Photos are saved at the **camera's original resolution**; the TV gets a downscaled copy from the same capture.
- Only records when the **clock is NTP-synced** (the Pi has no battery-backed clock) and more than 2 GB is free.
- A single config file, `/etc/painel-cameras.env` (mode 600), keeps passwords out of the code.

> **This is not live video.** It is a still frame refreshed every few seconds. Continuous decoding is not
> viable on a Pi 1 (a single snapshot takes ~8 s just to connect and decode).

## Requirements

- A Raspberry Pi running **Raspberry Pi OS Lite** (Debian 12 *bookworm*) with HDMI output. Tested on a Pi 1 Model B (armv6).
- IP camera(s) with **RTSP** and **H.264** (tested with a Yoosee camera, `rtsp://IP:554/onvif1`, Digest auth).
- `sudo` access on the Pi. The dependencies (`ffmpeg`, `python3-pil`) are installed by `install.sh`.

## Installation

On the Pi:

```bash
git clone https://github.com/paulomerino/painel-cameras-rpi.git
cd painel-cameras-rpi
sudo ./install.sh
sudo nano /etc/painel-cameras.env                           # set the camera IP, user and password
sudo python3 /usr/local/bin/painel-cameras.py --verificar   # tests one capture per camera
sudo systemctl start painel-cameras
```

The service is enabled at boot. Follow it with `journalctl -u painel-cameras -f`.

## Configuration

Everything lives in `/etc/painel-cameras.env` (see [`painel-cameras.env.example`](painel-cameras.env.example)).
One camera per prefix: `CAM1_`, `CAM2_`, …. The password is read literally (no quotes or escaping needed).

| Key | Default | What it does |
|---|---|---|
| `CAMn_HOST` | — (required) | Camera IP or hostname |
| `CAMn_USER` / `CAMn_PASS` | — (required) | RTSP credentials |
| `CAMn_NOME` | `Câmera n` | Name shown in the bar |
| `CAMn_PORTA` | `554` | RTSP port |
| `CAMn_CAMINHO` | `/onvif1` | Stream path (varies by manufacturer) |
| `CAMn_TRANSPORTE` | `udp` | `udp` or `tcp` (see "Technical notes") |
| `PAINEL_ROTACAO` | `10` | Seconds each camera stays on screen |
| `PAINEL_PAUSA` | `2` | Pause between captures |
| `PAINEL_GRAVAR` | `1` | `0` turns recording off |
| `PAINEL_GRAVAR_DIR` | `/var/lib/painel` | Where photos are stored |
| `PAINEL_RETENCAO_DIAS` | `7` | Delete photos older than this many days |
| `PAINEL_LIMITE_GB` | `8` | Space cap; above it, the oldest photos are deleted |

After editing the file: `sudo systemctl restart painel-cameras`.

## How it works

```
 camera(s) ──RTSP/UDP──► ffmpeg (1 frame) ──► /run/painel/CAMn.jpg   (RAM, spares the SD card)
                                                 │            │
                                   draws 1x/s    │            └─► copied to /var/lib/painel/... (if recording)
                                                 ▼
                              PIL ──► /dev/fb0 (framebuffer) ──► HDMI
```

- One thread captures (one camera at a time, to limit CPU and RAM); the main loop only draws.
- The text console is switched to graphics mode (`KD_GRAPHICS`) so it does not draw over the panel.
- Photo cleanup runs every hour and **does not load the file list into memory**: file names already sort by
  date, so it just walks the folders in order.

## Measured performance (Pi 1 Model B)

| Item | Value |
|---|---|
| Test camera stream | H.264 Main, 1280×720, 15 fps |
| Time per snapshot | ~8 s (up to ~13 s when the Pi was busy) |
| `ffmpeg` RAM | peak of ~46 MB |
| Panel (Python) RAM | ~14 MB |
| Python CPU | ~27–32 % |
| Average photo size | ~87 KB (small sample: 68–108 KB) |

## Photo storage

Estimate at ~87 KB per photo (it varies with the scene; at night, with infrared, it may change):

| Interval | Per day | 7 days |
|---|---|---|
| 10 s | ~730 MB | ~5 GB |
| 30 s | ~245 MB | ~1.7 GB |
| 1 min | ~120 MB | ~0.8 GB |

On a Pi 1 the real cycle is at least ~10 s (the capture alone takes ~8 s), so actual usage tends to be lower.

## Getting the photos

No need to install FTP: the Pi's SSH already provides **SFTP**, with the same SSH key and encryption.

```bash
scp -r pi@PI-IP:/var/lib/painel/CAM1/2026-11-14 ./photos
```

Or use a graphical client in **SFTP** mode (FileZilla, WinSCP). Avoid plain FTP: it sends the password
unencrypted. Do not expose the Pi to the internet; for remote access, use a VPN (WireGuard, Tailscale).

## Technical notes (pitfalls we ran into)

- **RTSP only worked over UDP** on the test camera. With `tcp`, `ffmpeg` fails with
  `Nonmatching transport in server reply`. If your camera is the other way around, set `CAMn_TRANSPORTE=tcp`.
- **A wrong password** on some cameras returns `400 Bad Request` (not `401`); check the username and password
  before suspecting the protocol. Only the right stream path works; others pass `DESCRIBE` and then
  fail on `SETUP`.
- **Framebuffer with padding (stride):** on a 1174×624 TV the stride was 4736 bytes (1184 px), not
  1174×4. The code reads the stride from `/sys/class/graphics/fb0/stride`.
- **Pixel format:** the framebuffer is BGRA (red at offset 16), so the panel writes `BGRX`.
- **The Pi 1 cannot detect under-voltage:** `vcgencmd get_throttled` shows `0x0` even with a weak supply, so it
  is **not proof that power is good**. In our case, powering the Pi from a TV's USB port made its internal USB
  hub (which holds the network chip) drop and reconnect under load, taking the network down for seconds
  (`Network is unreachable`) and causing full power losses. **Use a wall adapter: 5 V, ≥ 1 A (ideally 2 A),
  with a good cable.**
- **The TV can power the Pi off:** if HDMI is cut (for example by a screen-saving script), the TV may go on
  standby and cut the USB port that powers the Pi. Keep the screen on.
- **Recording to the SD card** writes hundreds of MB per day; a power loss during a write can corrupt files or
  the card. Another reason for a good power supply.
- **The password appears on `ffmpeg`'s command line** (visible in `ps` to other users on the Pi, for a few
  seconds per capture). On a personal, single-user Pi the risk is low, but it is good to know.

## Security and privacy

- Passwords live only in `/etc/painel-cameras.env` (mode 600, owned by root). `.gitignore` blocks `*.env`
  (except the `.example`), photos and images. **Never** commit that file.
- Error messages and the `--verificar` mode mask the password.
- The photos stay on the SD card: anyone with physical access to it can see the images.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

They cover config parsing (including a password with `$`, `"` and `\`), numeric camera ordering, password
masking, and photo cleanup by age and by size. They need Pillow (on the Pi: `python3-pil`).

## Uninstall

```bash
sudo ./uninstall.sh          # removes the program and service; keeps config and photos
sudo ./uninstall.sh --tudo   # also removes /etc/painel-cameras.env and /var/lib/painel
```

## License

[MIT](LICENSE). `ffmpeg`, Pillow and the DejaVu font are **not** part of this repository: they are installed
through apt and have their own licenses.
