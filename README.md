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

- **Rotating RTSP snapshots** across one or more cameras. A new image arrives about every ~12 s on a Pi 1 with
  the test camera: the cadence follows the camera's keyframes (see "How it works").
- **Top bar** with the camera name, "updated Ns ago" and a clock. Shows **NO SIGNAL** if the image gets stale (60 s).
- **Saves the photos** to `/var/lib/painel/<camera>/<YYYY-MM-DD>/<HH-MM-SS>.jpg` (at most one every
  `PAINEL_GRAVAR_INTERVALO`, default 10 s), with **age-based retention** (default 5 days) and a **size cap**
  (default 8 GB, oldest photos deleted first).
  Photos are saved at the **camera's original resolution**; the TV gets a downscaled copy from the same frame.
- Only records when the **clock is NTP-synced** (the Pi has no battery-backed clock) and more than 2 GB is free.
- A single config file, `/etc/painel-cameras.env` (mode 600), keeps passwords out of the code.

> **This is not live video.** It is a still frame refreshed every ~10 s or more. The panel keeps one `ffmpeg`
> connected per camera and decodes **only the keyframes** (`-skip_frame nokey`). Decoding every frame was not
> tried on a Pi 1; if you turn keyframe-only off (`PAINEL_SO_QUADROS_CHAVE=0`), expect much higher CPU.

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
| `PAINEL_PAUSA` | `2` | Minimum seconds between the frames `ffmpeg` hands over (on a Pi 1, `10` is a good value; the real cadence follows the camera's keyframes) |
| `PAINEL_SO_QUADROS_CHAVE` | `1` | `1` decodes only keyframes (light, needed on a Pi 1); `0` decodes every frame |
| `PAINEL_GRAVAR` | `1` | `0` turns recording off |
| `PAINEL_GRAVAR_DIR` | `/var/lib/painel` | Where photos are stored |
| `PAINEL_GRAVAR_INTERVALO` | `10` | Minimum seconds between saved photos, per camera |
| `PAINEL_RETENCAO_DIAS` | `5` | Delete photos older than this many days |
| `PAINEL_LIMITE_GB` | `8` | Space cap; above it, the oldest photos are deleted |

After editing the file: `sudo systemctl restart painel-cameras`.

## How it works

```
 camera(s) ──RTSP/UDP──► ffmpeg (stays connected, keyframes only) ──► /run/painel/CAMn.jpg       (RAM, spares the SD card)
                                                                │         /run/painel/CAMn.full.jpg  (original resolution)
                                                                │                  │
                                           redraws the bar 1x/s │                  └─► copied to /var/lib/painel/... (if recording)
                                                                ▼
                                              PIL ──► /dev/fb0 (framebuffer) ──► HDMI
```

- One thread **per camera** keeps an `ffmpeg` connected. It decodes only keyframes (`-skip_frame nokey`),
  picks one frame every `PAINEL_PAUSA` seconds without ever repeating a frame (a repeated frame would look
  "updated now" while being stale) and writes it atomically (`-atomic_writing 1`), so the panel never reads a
  half-written JPEG.
- A watchdog restarts the `ffmpeg` if it exits or if no new frame arrives for `max(45, 4 × PAINEL_PAUSA)` s,
  with a back-off of up to 60 s while the camera is down.
- The main loop rewrites **only the 48-line top bar** once per second (clock and "updated Ns ago"). The whole
  screen is rewritten only when the image changes (new frame, camera switch, NO SIGNAL), plus a full refresh
  every 60 s as a safety net.
- With several cameras there is one `ffmpeg` per camera; this was only measured with one.
- The text console is switched to graphics mode (`KD_GRAPHICS`) so it does not draw over the panel.
- Photo cleanup runs every hour and **does not load the file list into memory**: file names already sort by
  date, so it just walks the folders in order.
- `--verificar` still does a single one-shot capture per camera.

## Measured performance (Pi 1 Model B, one camera)

| Item | Value |
|---|---|
| Test camera stream | H.264, 1280×720, 10 fps (`ffprobe`) |
| Time to the first frame after (re)start | ~18–24 s (connect + wait for a keyframe) |
| New frame | every ~12 s (follows the camera's keyframes) |
| `ffmpeg` CPU (continuous, keyframes only) | ~24 % of one core (90 s test) |
| Whole service CPU | ~27–29 % of one core (60 s windows); the previous one-capture-per-photo version used ~60 % at `PAINEL_PAUSA=10` |
| Load average | ~1 (it was ~3.5) |
| Average photo size | ~200 KB at the original resolution (sample of one frame: 198 KB; older sample of 6,447 photos: ~200 KB) |

RAM was not re-measured for this version. Decoder warnings such as `Too short data for FU-A H.264 RTP packet`
show up now and then (UDP packet loss); `ffmpeg` keeps going, and a damaged frame may show artifacts.

## Photo storage

Estimate at ~200 KB per photo (it varies with the scene; at night, with infrared, it may change). With the test
camera a photo is saved about every 12 s:

| Interval | Photos per day | Per day | 5 days (default) |
|---|---|---|---|
| ~12 s (every frame) | ~7,200 | ~1.4 GB | ~7.2 GB |
| 30 s | 2,880 | ~0.6 GB | ~2.9 GB |
| 1 min | 1,440 | ~0.3 GB | ~1.4 GB |

At the default 8 GB cap, 5 days of photos at ~12 s is right at the limit: if it goes over, the oldest photos are
deleted first. Raise `PAINEL_GRAVAR_INTERVALO` or lower `PAINEL_RETENCAO_DIAS` to keep more margin.

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
- **The password appears on `ffmpeg`'s command line**, and since `ffmpeg` now stays connected it is visible in
  `ps` to every user on the Pi **all the time**. See "Hiding the password from `ps`" below.
- **RTSP over `tcp` failed on the test camera** (`Nonmatching transport in server reply`), so the continuous
  connection also uses UDP. Packet loss shows up as decoder warnings, not as a dropped connection.

## Security and privacy

- Passwords live only in `/etc/painel-cameras.env` (mode 600, owned by root). `.gitignore` blocks `*.env`
  (except the `.example`), photos and images. **Never** commit that file.
- Error messages and the `--verificar` mode mask the password.
- The photos stay on the SD card: anyone with physical access to it can see the images.

### Hiding the password from `ps` (recommended)

`ffmpeg` only accepts the credentials inside the RTSP URL, so they end up in its arguments. Mounting `/proc`
with `hidepid=2` makes other users' processes invisible to non-root users. It does **not** protect against
root, and the password stays on `ffmpeg`'s command line.

```bash
sudo cp -p /etc/fstab /etc/fstab.bak
sudo groupadd -r proc                       # users in this group still see every process
GID=$(getent group proc | cut -d: -f3)
# systemd-logind must keep seeing all processes:
sudo mkdir -p /etc/systemd/system/systemd-logind.service.d
printf '[Service]\nSupplementaryGroups=proc\n' | sudo tee /etc/systemd/system/systemd-logind.service.d/hidepid.conf
sudo systemctl daemon-reload && sudo systemctl restart systemd-logind
# persist across reboots, then apply now:
sudo sed -i -E "s#^proc[[:space:]]+/proc[[:space:]]+proc[[:space:]]+defaults[[:space:]]#proc /proc proc defaults,hidepid=2,gid=$GID #" /etc/fstab
sudo mount -o remount,hidepid=2,gid=$GID /proc
```

Tested on Raspberry Pi OS (Debian 12, systemd 252): after it, `ps` as a normal user no longer shows the
`ffmpeg` of root, and the setting survived a reboot. To undo it:
`sudo mount -o remount,hidepid=0 /proc`, restore `/etc/fstab` and delete the `hidepid.conf` drop-in.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

They cover config parsing (including a password with `$`, `"` and `\`), numeric camera ordering, password
masking, photo cleanup by age and by size, the continuous `ffmpeg` command (keyframes only, atomic writes,
URL escaping), the bar-only redraw (the isolated bar is byte-identical to the top of a full frame), and the
interval between saved photos. They need Pillow (on the Pi: `python3-pil`) and **Linux**, because the script
imports `fcntl`; on Windows, run them on the Pi.

## Uninstall

```bash
sudo ./uninstall.sh          # removes the program and service; keeps config and photos
sudo ./uninstall.sh --tudo   # also removes /etc/painel-cameras.env and /var/lib/painel
```

## License

[MIT](LICENSE). `ffmpeg`, Pillow and the DejaVu font are **not** part of this repository: they are installed
through apt and have their own licenses.
