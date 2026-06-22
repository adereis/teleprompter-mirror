# Teleprompter Mirror

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Mirror a laptop window to a tablet placed near the camera, designed for
eye contact during video calls.

Look at the tablet (right under your webcam) instead of at the meeting window,
and you appear to make eye contact. The laptop captures a window over WebRTC and
streams it to a tablet over a low-latency USB link; the tablet shows it
mirrored, like a teleprompter. Optional extras control a Sony A6300 camera (zoom,
refocus) over WiFi while it feeds clean HDMI video.

> **This is a personal project, shared to be forked.** It solves my specific
> setup and I don't maintain it as a general-purpose tool or take contributions.
> If it's useful, [fork it and make it your own](#forking-and-adapting).

## Quick start

1. Connect the tablet via USB (data cable, not charge-only) with ADB debugging enabled
2. Start the server with USB tethering:
   ```bash
   ./bin/start-mirror.sh usb
   ```
3. Open the cast page — either from GNOME's app launcher ("Teleprompter Mirror") or:
   ```bash
   ./bin/open-cast.sh
   ```
4. On the tablet, open `http://localhost:8047/view` in Chrome

The server binds to `localhost:8047` — the tablet reaches it via ADB reverse port
forwarding (set up automatically by `start-mirror.sh`).

## Pages

| Path | Purpose |
|------|---------|
| `/cast` | Share a window and stream it to the tablet (optional crop mode) |
| `/view` | Tablet viewer (mirrored, fullscreen, auto-reconnect) |
| `/latency` | Visual latency measurement tool |

## Configuration

Everything has a sensible built-in default, so the tools run out of the box.
To adapt the setup to a different machine — a different browser, port, or camera
WiFi profile — copy the example config and edit it:

```bash
mkdir -p ~/.config/teleprompter-mirror
cp config.example.env ~/.config/teleprompter-mirror/config.env
```

| Setting | Default | Used for |
|---------|---------|----------|
| `TELEPROMPTER_PORT` | `8047` | Signaling server port |
| `TELEPROMPTER_BIND` | `127.0.0.1` | Server bind address (keep on localhost) |
| `TELEPROMPTER_BROWSER` | `google-chrome` | Browser for the cast app window |
| `TELEPROMPTER_CAMERA_CONNECTION` | `Camera-A6300` | NetworkManager profile for the camera AP |
| `TELEPROMPTER_CAMERA_ENDPOINT` | `http://192.168.122.1:8080/sony` | Camera Remote API base URL |

The same file is read by the bash scripts, the Python tools, and the systemd
user service. Values resolve as **environment variable > config file > default**,
so a one-off `TELEPROMPTER_PORT=9000 ./bin/start-mirror.sh` still works. The config
file lives outside the repo (`~/.config`), so machine-specific values never get
committed. Re-run `sudo ./system/install.sh` after changing
`TELEPROMPTER_CAMERA_CONNECTION`, since that value is baked into the installed
NetworkManager dispatcher.

## USB tethering

USB tethering provides lower latency (~90ms) and a secure direct link.
Use a **data-capable USB cable** — charge-only cables silently fail.

`bin/start-mirror.sh usb` handles everything: tethering, routing, firewall, ADB reverse,
and server startup. If the ADB USB function switch doesn't work (common on Samsung),
the script opens the tethering settings on the tablet for manual toggle.

### KVM switch recovery

If the tablet is connected through a USB KVM switch, switching away and back resets
the USB tethering. Install the system hooks to automate recovery:

```bash
sudo ./system/install.sh
```

After a KVM switch: accept the USB debugging prompt on the tablet → tethering settings
open automatically → tap the USB tethering toggle → routing/firewall/ADB reverse are
configured automatically. Or manually: `./bin/start-mirror.sh reconnect`.

<details>
<summary>Manual USB tethering setup</summary>

1. Connect the tablet via USB with ADB/debug enabled
2. Enable USB tethering on the tablet (Settings → Connections → Tethering), or:
   `adb shell svc usb setFunctions rndis,adb`
3. Fix the default route so the laptop keeps its internet:
   ```bash
   nmcli connection modify "<usb-connection>" ipv4.never-default yes
   ```
4. Trust the USB interface for WebRTC media:
   ```bash
   sudo firewall-cmd --zone=trusted --change-interface=usb0
   ```
5. Set up ADB reverse: `adb reverse tcp:8047 tcp:8047`
6. Start the server: `./app/mirror-server.py`
7. On the tablet, open `http://localhost:8047/view`

</details>

## Camera control

The Sony A6300 camera can be controlled (zoom, refocus) from the command line
while HDMI capture stays active. A dedicated USB WiFi adapter connects to the
camera's WiFi AP, leaving the main WiFi free for internet.

```bash
./camera/camera-control.py zoom in     # zoom in one step
./camera/camera-control.py zoom out 2s # smooth zoom out for 2 seconds
./camera/camera-control.py zoom set    # go to default framing position
./camera/camera-control.py refocus     # nudge zoom to trigger AF-C refocus
./camera/camera-control.py zoom        # show current zoom position
```

See [docs/CAMERA.md](docs/CAMERA.md) for setup instructions, API details, and
troubleshooting.

## How it works

- `cast.html` uses `getDisplayMedia()` to capture a window, then sends it via WebRTC.
  Optional crop mode lets you draw a rectangle to stream only a region.
- `view.html` receives the stream and displays it fullscreen with horizontal mirror
  (teleprompter effect) and screen wake lock
- `mirror-server.py` handles signaling (SDP offer/answer exchange) and rewrites
  Chrome's mDNS ICE candidates to real LAN IPs so ICE can connect
- `camera-control.py` controls the camera via Sony's Camera Remote API (JSON-RPC
  over WiFi) — zoom, refocus, and status queries

## Tuning

The cast page is configured for video call mirroring:
- VP8 preferred (Chrome's libvpx encoder is optimized for real-time WebRTC)
- 8 Mbps max bitrate, resolution downscaled to match the tablet's viewport
- 60 fps capture, `contentHint = 'motion'` (smooth faces over sharp text)
- `jitterBufferTarget = 0` on viewer (minimizes receive-side delay on USB)
- Live encoder stats shown on the cast page after connection

## Requirements

- Linux with GNOME/Wayland
- Python 3 (standard library only) — for the mirror server and camera control
- Chrome or Chromium on both laptop and tablet
- `adb` — for USB tethering setup and ADB reverse port forwarding
- USB WiFi adapter (MT7601U or similar) — for camera control (optional)

## Repository layout

```
app/        WebRTC cast web app + signaling server (mirror-server.py, *.html, assets)
camera/     Sony A6300 control over the Camera Remote API (camera-control.py)
lib/        shared config loaded by both the scripts and the Python tools
bin/        user-facing launchers (start-mirror.sh, open-cast.sh)
system/     OS integration installed by system/install.sh:
              udev/           USB device-detection rules
              systemd/        services
              networkmanager/ dispatcher hooks
docs/        supplementary docs (CAMERA.md)
tests/       stdlib unit tests (run with ./run-tests.sh)
```

## Forking and adapting

This project is built around one specific setup — a Samsung Galaxy Tab, a Sony
A6300, an MT7601U WiFi adapter, Fedora/GNOME — and it's deliberately small. I'm
not growing it into a general-purpose tool or maintaining it for others, so
there's no issue tracker and no contribution process. The intended workflow is:
**fork it, and drive your own version.**

It's designed to make that easy:

- **Machine-specific values live in config, not code** — see
  [Configuration](#configuration). Your fork shouldn't need source edits just to
  change a port, browser, or camera profile.
- **The architecture, hardware quirks, and gotchas are documented in
  [`CLAUDE.md`](CLAUDE.md)** — point an AI coding assistant at it and adapting
  the udev rules, dispatchers, and camera IDs to your own gear goes quickly.
- **Keep your setup out of the source.** If you republish your fork, keep
  usernames, hostnames, SSIDs, and passwords in `~/.config` and out of tracked
  files (the existing `__PLACEHOLDER__` + `install.sh` pattern shows how).

There's a small test suite to lean on while you change things (standard library
only — nothing to install):

```bash
./run-tests.sh
```

It runs the Python unit tests, byte-compiles the tools, and lints the shell
scripts with `shellcheck` when it's present.

## License

[MIT](LICENSE) © Ademar Reis

This is a personal project shared in the hope it's useful. It's built around
specific hardware (a Samsung tablet, a Sony A6300, an MT7601U WiFi adapter), but
the design keeps environment-specific values in configuration rather than code,
so adapting it to your own setup is mostly a matter of editing one config file.
