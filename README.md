# Teleprompter Mirror

Mirror a laptop window to a tablet placed near the camera, designed for
eye contact during video calls.

## Quick start

1. Connect the tablet via USB (data cable, not charge-only) with ADB debugging enabled
2. Start the server with USB tethering:
   ```bash
   ./start-mirror.sh usb
   ```
3. Open the cast page — either from GNOME's app launcher ("Teleprompter Mirror") or:
   ```bash
   ./open-cast.sh
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

## USB tethering (recommended)

USB tethering provides lower latency (~90ms) and a secure direct link.
Use a **data-capable USB cable** — charge-only cables silently fail.

`start-mirror.sh usb` handles everything: tethering, routing, firewall, ADB reverse,
and server startup. If the ADB USB function switch doesn't work (common on Samsung),
the script opens the tethering settings on the tablet for manual toggle.

### KVM switch recovery

If the tablet is connected through a USB KVM switch, switching away and back resets
the USB tethering. Install the system hooks to automate recovery:

```bash
sudo ./install-hooks.sh
```

After a KVM switch: accept the USB debugging prompt on the tablet → tethering settings
open automatically → tap the USB tethering toggle → routing/firewall/ADB reverse are
configured automatically. Or manually: `./start-mirror.sh reconnect`.

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
6. Start the server: `./mirror-server.py`
7. On the tablet, open `http://localhost:8047/view`

</details>

## Camera control

The Sony A6300 camera can be controlled (zoom, refocus) from the command line
while HDMI capture stays active. A dedicated USB WiFi adapter connects to the
camera's WiFi AP, leaving the main WiFi free for internet.

```bash
./camera-control.py zoom in        # zoom in one step
./camera-control.py zoom out 2s    # smooth zoom out for 2 seconds
./camera-control.py zoom set       # go to default framing position
./camera-control.py refocus        # nudge zoom to trigger AF-C refocus
./camera-control.py zoom           # show current zoom position
```

See [CAMERA.md](CAMERA.md) for setup instructions, API details, and
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

<details>
<summary>RDP virtual display (alternative)</summary>

For simpler setups (laptop with 1080p screen), GNOME Remote Desktop can mirror the
primary display to the tablet via RDP — no window selection needed.

```bash
./virtual-display.sh setup    # configure headless RDP (generates random password)
./virtual-display.sh status   # check state
./virtual-display.sh mirror   # open local preview with wl-mirror
./virtual-display.sh stop     # disable
```

Connect from the tablet with an RDP client (e.g., Windows App) to `<laptop-ip>:3389`.
Credentials are generated on first setup and stored in `.rdp-credentials`.
RDP is restricted to the trusted firewall zone (USB tethering interface only).

</details>

## Requirements

- Linux with GNOME/Wayland
- Python 3 (standard library only) — for the mirror server and camera control
- Chrome or Chromium on both laptop and tablet
- `adb` — for USB tethering setup and ADB reverse port forwarding
- USB WiFi adapter (MT7601U or similar) — for camera control (optional)
