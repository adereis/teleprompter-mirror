# CLAUDE.md

## Project overview

Teleprompter Mirror: mirror a video call window from a laptop/desktop to a tablet
placed near the camera for eye contact during meetings.

Uses WebRTC (`mirror-server.py`, `cast.html`, `view.html`) to capture a
specific window and stream it to a tablet. The tablet displays the stream
mirrored, acting as a teleprompter near the camera lens.

## Architecture

### WebRTC mirror

- `mirror-server.py` — Python HTTP server (stdlib only, no deps). Binds to
  `127.0.0.1:8047` by default (localhost only — tablet connects via ADB reverse).
  Serves HTML pages, static assets (icon, manifest), and acts as the WebRTC
  signaling relay (SDP offer/answer exchange via POST/GET). Rewrites Chrome mDNS
  ICE candidates to real LAN IPs in `_fix_mdns()`. Supports `--bind` to override
  the bind address and `--ip` to force a specific IP for mDNS rewrite.
- `cast.html` — Laptop-side. Two-column layout: video preview fills the left
  area, controls/status/stats in a right sidebar. Uses `getDisplayMedia()` +
  `RTCPeerConnection` to capture and send a window. Shows WebRTC stats (encode
  time, FPS, bitrate, RTT, jitter) in the sidebar after connection. Downscales
  to viewer resolution before encoding to reduce VP8 CPU load. Optional crop
  mode: click "Crop" to draw a rectangle and stream only that region via canvas.
  Uses `replaceTrack()` to switch between direct and cropped streams without
  reconnecting. Crop adds a canvas step to the pipeline; when disabled, the
  stream goes direct (no extra latency). Page title reflects connection state.
- `view.html` — Tablet-side. Receives WebRTC stream and displays it fullscreen with
  horizontal flip (`scaleX(-1)` for teleprompter mirror effect). Sets
  `jitterBufferTarget=0` to minimize receive-side buffering on USB. Requests
  Screen Wake Lock to keep the tablet on. Auto-reconnects on disconnect.
- `latency-test.html` — Visual latency measurement. Displays a millisecond clock
  that can be shared to the tablet; photograph both screens to measure delay.
- `open-cast.sh` — Opens `/cast` in Chrome's `--app` mode (standalone window,
  separate taskbar entry, keeps Chrome Tab capture in getDisplayMedia). Shows a
  zenity error dialog if the server isn't running.
- `start-mirror.sh` — One-command USB tethering + server startup. Supports
  `usb` (full setup), `reconnect` (re-enable USB without restarting server),
  and no-argument (server only) modes.
- `teleprompter-mirror.service` — systemd user service template. Installed to
  `~/.config/systemd/user/` by `install.sh`. Auto-starts the mirror server at
  login (`WantedBy=graphical-session.target`), restarts on failure, stops on
  logout (`PartOf`). Uses `__PROJECT_DIR__` placeholder like the desktop entry.

### KVM switch automation

When the tablet and camera WiFi adapter are on a USB KVM switch,
disconnects/reconnects reset everything. System hooks automate recovery:

- `99-teleprompter-tablet.rules` — udev rule. Detects Samsung tablet connecting
  in MTP mode (`04e8:6860`) and triggers a systemd service that opens tethering
  settings on the tablet via ADB. The user just taps the toggle.
- `99-teleprompter` — NetworkManager dispatcher. Fires when `usb0` comes up
  after tethering is enabled. Fixes routing (never-default), firewall (trusted
  zone), and ADB reverse port forwarding. No user action needed. Uses a
  dual-check filter: connection name must match `"Wired connection"*` AND
  the network driver must be `rndis_host` or `cdc_ether` (Android USB
  tethering). The driver check prevents poisoning Thunderbolt dock ethernet,
  which also auto-creates as `"Wired connection N"` before being renamed.
- `99-teleprompter-camera` — NetworkManager dispatcher. Fires on `up` (initial
  connection) and `dhcp4-change` (lease renewal). On `up`, logs the connection.
  On `dhcp4-change`, checks if the camera reset to NotReady and runs reconnect
  if so. Only `dhcp4-change` runs reconnect — it always follows `up`, and
  checking status first avoids duplicate reconnects. No keepalive — periodic
  HTTP pings were found to cause WiFi AP resets on the A6300.
- `99-teleprompter-wifi.rules` — udev rule. Detects the MT7601U USB WiFi adapter
  (`148f:7601`) and triggers `teleprompter-wifi-rebind.service`. After KVM
  switches or port changes, the `mt7601u` driver sometimes fails to claim the
  USB interface despite successful enumeration (no `wlan0` created). The service
  waits for normal probe, then forces a USB re-probe if needed.
- `teleprompter-tether-prompt.service` — systemd one-shot service triggered by
  the tablet udev rule. Runs `adb shell am start` as the user.
- `teleprompter-wifi-rebind.service` — systemd one-shot service triggered by
  the WiFi adapter udev rule. Runs `wifi-rebind.sh` as root (needs sysfs
  write access to toggle USB device authorization).
- `wifi-rebind.sh` — Recovery script for the MT7601U. Waits 5 seconds for
  the driver to probe normally, then checks for `wlan0`. If missing, finds the
  device in sysfs and toggles its `authorized` attribute to force re-enumeration
  and driver re-probe. Retries up to 3 times. Logs to `teleprompter-wifi`
  syslog tag. If the device dropped from sysfs entirely (EPROTO), logs a
  warning — physical replug is needed.
- `install.sh` — Installs system hooks, desktop entry, and user service (run
  with sudo). Substitutes `__USER__` and `__PROJECT_DIR__` placeholders with
  runtime values so the source files contain no hardcoded paths or usernames.
- `uninstall.sh` — Removes all files installed by `install.sh` (including the
  user service — disables and stops it first).
- `teleprompter-mirror.desktop` — Desktop entry template. Installed to
  `~/.local/share/applications/` by `install.sh`.

### Camera control

- `camera-control.py` — Controls the Sony A6300 camera via Sony's Camera Remote
  API (JSON-RPC over WiFi). Requires the camera to be in Movie mode with Smart
  Remote Embedded running. A dedicated USB WiFi adapter (MT7601U, `wlan0`) connects
  to the camera's WiFi AP (`DIRECT-xxxx:ILCE-6300`) via the `Camera-A6300` NM
  profile, leaving the main WiFi free for internet. HDMI capture continues working
  simultaneously. Supports zoom in/out (power zoom lens only), refocus nudge
  (zoom in+out to trigger AF-C), and status queries. No external deps (stdlib only).
  The camera's WiFi AP uses `192.168.122.0/24` — libvirt's default network was
  moved to `192.168.124.0/24` to avoid a subnet collision.

### Non-functional prototypes

- `cast-region.py` — Native GStreamer/PipeWire screen capture prototype. Blocked
  by GNOME 49's `object.register=false` on screencast PipeWire nodes. Kept for
  reference; see docstring for details.

## Key gotchas

- Chrome obfuscates local IPs in WebRTC candidates with mDNS UUIDs. The server
  rewrites these to real IPs via regex in `_fix_mdns()`. Without this, the tablet
  can't connect on LAN. When multiple network interfaces exist (Wi-Fi + USB), each
  mDNS candidate is duplicated for every local IP so ICE finds any working path.
- Android Chrome blocks `video.play()` without prior user gesture. The `muted`
  attribute on the video element bypasses this for video-only streams.
- USB tethering creates a wired connection that NetworkManager may set as the
  default route, breaking internet. Fix with `ipv4.never-default yes` on the USB
  connection profile — this persists across reconnects.
- USB tethering interface naming varies by kernel/driver: `usb0` (legacy),
  `enp*` (predictable naming), or `enx*` (MAC-based). Scripts must match all three.
- `adb shell svc usb setFunctions rndis,adb` temporarily kills the ADB connection
  because the USB stack resets. The command exits 137 (SIGKILL) — this is expected.
  ADB reconnects within ~5 seconds.
- Samsung tablets may silently revert the USB function back to MTP. The
  `start-mirror.sh` script falls back to opening the tethering Settings UI via
  `adb shell am start -a android.settings.TETHER_SETTINGS` when this happens.
- Fedora's firewalld blocks WebRTC media (UDP) by default. Move the USB interface
  to the `trusted` zone (`firewall-cmd --zone=trusted --change-interface=usb0`).
  This is runtime-only. The signaling server no longer needs a TCP firewall rule
  since it binds to localhost (tablet reaches it via ADB reverse).
- The tablet's Wi-Fi can stay on when using USB tethering — the firewall naturally
  forces WebRTC media over USB. Wi-Fi UDP is blocked by the default zone, while
  USB is in the trusted zone. ICE tries both paths and selects USB.
- GStreamer's `pipewiresrc` cannot consume GNOME Shell's screencast portal
  streams on GNOME 49 / PipeWire 1.4.x. GNOME creates the screencast node
  with `object.register=false`, making it invisible to pipewiresrc's
  registry-based discovery. The `cast-region.py` prototype is blocked by
  this. The workaround is `cast.html`'s crop mode, which uses Chrome's
  `getDisplayMedia()` + canvas crop instead.
- The tablet is a Samsung Galaxy Tab A7 (SM-T500), Wi-Fi only, Android 12.
  USB tethering works despite being Wi-Fi only.
- The Sony A6300's WiFi AP hardcodes `192.168.122.0/24`, which collides with
  libvirt's default `virbr0` bridge. The libvirt default network was moved to
  `192.168.124.0/24` to fix this.
- The A6300's Camera Remote API only exposes focus control methods when the
  full Smart Remote Control app is installed (not Smart Remote Embedded). Sony
  discontinued PlayMemories Camera Apps, so the upgrade is no longer available.
  Workaround: use AF-C mode and trigger refocus via a small zoom nudge.
- The A6300's WiFi AP resets when it receives frequent HTTP requests. Keepalive
  pings at 60s caused 0.55 disassociations/hr; at 300s, 0.15/hr; with no pings,
  zero disassociations over 92 hours. Do not poll the Camera Remote API unless
  the user explicitly triggered a command. The NM dispatcher handles recovery
  from any WiFi drops without needing periodic polling.
- The camera must be in Movie mode for clean high-res HDMI output. Still/P mode
  outputs a low-resolution LCD mirror over HDMI.
- Camera WiFi uses `ipv4.never-default yes` to avoid stealing the default route.
  A dedicated USB WiFi adapter (MT7601U, `wlan0`) connects to the camera via the
  `Camera-A6300` NM profile, so the main WiFi (`wlp9s0`) stays on the home/office
  network. The profile auto-connects when the camera's AP is visible.
- The laptop's built-in webcam (Integrated RGB Camera) has higher PipeWire
  priority than the HDMI capture dongle by default, making it the default
  camera in Google Meet and other apps. A WirePlumber rule in
  `~/.config/wireplumber/wireplumber.conf.d/prefer-hdmi-capture.conf` boosts
  the HDMI capture's `priority.session` above the built-in camera so the
  external camera is preferred. Both cameras remain available in app dropdowns.
- The `99-teleprompter` NM dispatcher must only match tablet USB tethering
  connections, not all USB ethernet. It uses a dual-check filter: NM connection
  name (`Wired connection *`) AND network driver (`rndis_host` or `cdc_ether`).
  Connection name alone is insufficient — Thunderbolt dock ethernet also
  auto-creates as `"Wired connection N"` and would get `never-default yes` set
  on it, breaking internet connectivity. The driver check is authoritative:
  only Android USB tethering uses `rndis_host`/`cdc_ether`.

## Security and privacy rules

This repo is public. Every commit is auditable. Follow these rules strictly.

### No user-specific values in source files

- **No usernames, hostnames, or MAC addresses** in tracked files. Use
  placeholders (`__USER__`, `__PROJECT_DIR__`) and substitute at install time
  (see `install.sh`).
- **No passwords or credentials.** Generate at first run and store outside the
  repo.
- **No WiFi SSIDs or passwords.** Camera/tablet connection details live in
  NetworkManager profiles, not in code.
- **Check before committing**: `git diff --cached | grep -iE 'password|secret|ssid'`
  should return nothing. If a value is user-specific, it doesn't belong in the repo.

### Network isolation

- **Routing priority**: Ethernet (`Ethernet` profile) is the primary internet
  uplink; WiFi (`optimusnet`) is the automatic fallback. Both must keep
  `ipv4.never-default no` (the default). Tablet USB (`Wired connection N`) and
  camera WiFi (`Camera-A6300`) are local-only — both use `never-default yes`
  and must never carry a default route. NM auto-assigns metrics (~100 for
  ethernet, ~600 for WiFi) and adds +20,000 to WiFi when a wired default
  exists, so no explicit metrics are needed.
- **Servers bind to localhost only** (`127.0.0.1`). The tablet reaches them via
  ADB reverse port forwarding. Never bind to `0.0.0.0` or a LAN address.
- **USB tethering interfaces go in the `trusted` firewall zone** at runtime only
  (no `--permanent`). All other interfaces stay in their default restricted zones.
- **Camera WiFi uses a dedicated adapter** (`wlan0`) with `never-default yes` so
  it never becomes a route to the internet.
### Dispatcher and system hooks

- **Match tablet connections by NM connection name AND network driver.** Name
  alone (`Wired connection *`) is ambiguous — Thunderbolt dock ethernet also
  auto-creates with that name. The driver check (`rndis_host` or `cdc_ether`)
  is authoritative for Android USB tethering.
- **Never set `never-default` or change firewall zones** on named connection
  profiles (like `Ethernet`). Only auto-created `Wired connection N` profiles
  with a tethering driver should be modified by dispatchers.

## Environment

- Fedora Linux, GNOME 49, Wayland, PipeWire
- No external Python dependencies — stdlib only
- Tablet: Android with Chrome
- Camera: Sony A6300 (ILCE-6300) with E PZ 16-50mm power zoom kit lens, HDMI
  capture via USB adapter
