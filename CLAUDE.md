# CLAUDE.md

## Project overview

Teleprompter Mirror: mirror a video call window from a laptop/desktop to a tablet
placed near the camera for eye contact during meetings.

Uses WebRTC (`mirror-server.py`, `cast.html`, `view.html`) to capture a
specific window and stream it to a tablet. The tablet displays the stream
mirrored, acting as a teleprompter near the camera lens.

## Architecture

The repo is grouped by purpose. Component names below are basenames; this map
gives their home directory:

```
app/        mirror-server.py, cast.html, view.html, latency-test.html, icon.svg, manifest.json
camera/     camera-control.py
lib/        config.sh, teleprompter_config.py        (shared config)
            usb.sh                                 (tablet interface discovery)
            window_focus.py                        (raise the shared window)
bin/        start-mirror.sh, open-cast.sh            (user launchers)
            focus-shared.sh
system/     install.sh, uninstall.sh, *.desktop, wifi-rebind.sh
            udev/ *.rules · systemd/ *.service
            networkmanager/ 99-teleprompter[-camera]
            gnome-extension/ teleprompter-focus@…    (window activation)
docs/       CAMERA.md
tests/      loader.py, test_*.py
.githooks/  pre-commit                               (secret-leak guard)
```

Cross-directory wiring to keep in mind when moving things:
- `app/mirror-server.py` and `camera/camera-control.py` add `../lib` to
  `sys.path` before `import teleprompter_config`; `mirror-server.py` imports
  `window_focus` from there too.
- `bin/*.sh` source `../lib/config.sh`; `system/install.sh` substitutes
  `__PROJECT_DIR__` with the **repo root** (its own parent), so the baked
  paths are `app/mirror-server.py`, `bin/open-cast.sh`, `camera/camera-control.py`,
  `bin/focus-shared.sh`.
- `bin/focus-shared.sh` runs `lib/window_focus.py` directly — no server
  involved, so the desktop action works whether or not a cast is running.
- The GNOME extension UUID (`teleprompter-focus@teleprompter-mirror.local`)
  appears in four places that must stay in sync: the extension directory name,
  `EXTENSION_UUID` in `lib/window_focus.py`, `EXT_UUID` in `system/install.sh`
  and `system/uninstall.sh`, and the path checked by `run-tests.sh`.

### Configuration

Environment-specific values are kept out of the source via a single
`KEY=value` config file at `~/.config/teleprompter-mirror/config.env`
(`config.example.env` is the documented template). Defaults live in two
mirrored places — `lib/config.sh` (bash) and `lib/teleprompter_config.py`
(Python) — and both resolve values as **environment > config file >
built-in default**.

- `lib/config.sh` — sourced by `bin/start-mirror.sh` and `bin/open-cast.sh`.
  Exports `TELEPROMPTER_*` so child processes (the server) inherit them.
- `lib/teleprompter_config.py` — imported by `app/mirror-server.py` and
  `camera/camera-control.py`. Parses the same file (stdlib only).
- `teleprompter-mirror.service` reads the file via `EnvironmentFile=-` so the
  systemd-managed server honors the same config.
- `install.sh` bakes `TELEPROMPTER_CAMERA_CONNECTION` into the NM camera
  dispatcher (`__CAMERA_CONNECTION__` placeholder) because dispatchers run as
  root and can't read the user's config at runtime. It also applies
  `TELEPROMPTER_CAMERA_BSSID` to the NM connection profile (BSSID lock) if
  set — this tells NM the camera is a single AP, disabling background
  scanning that takes the adapter off-channel and causes inactivity kicks.
  Re-run `install.sh` after changing either value.

When adding a new tunable: add it to `DEFAULTS` in `lib/teleprompter_config.py`,
the defaults block + export list in `lib/config.sh`, and `config.example.env`.
Keep the three in sync.

Numeric values go through `teleprompter_config.get_int()`, which raises on a
malformed value rather than falling back to the default. Silently substituting
would park a camera configured for 54 at 50 forever with nothing saying why.
Callers decide *when* to read it: `camera-control.py` validates the zoom target
in `main()` for lens-moving commands, but the keepalive only disables its
escalation and keeps polling, because losing the poll causes AP inactivity
kicks — a worse failure than not being able to recover.

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
  reconnecting. A "Focus Shared" button and a "choose shared window…" picker
  raise the window being mirrored (see *Focusing the shared window*). Crop adds a canvas step to the pipeline; when disabled, the
  stream goes direct (no extra latency). Page title reflects connection state.
  A connection attempt owns its peer and AbortController; stale asynchronous
  results cannot modify a replacement peer. A retry task belongs to the capture
  that started it and is invalidated by Stop. HTTP failures trigger backoff.
- `view.html` — Tablet-side. Receives WebRTC stream and displays it fullscreen with
  horizontal flip (`scaleX(-1)` for teleprompter mirror effect). Sets
  `jitterBufferTarget=0` to minimize receive-side buffering on USB. Requests
  Screen Wake Lock to keep the tablet on. Auto-reconnects on disconnect.
  A single retry loop owns negotiation and disconnection cleanup, including
  removing resize listeners. Failed SDP or HTTP operations re-enter that loop.
- `latency-test.html` — Visual latency measurement. Displays a millisecond clock
  that can be shared to the tablet; photograph both screens to measure delay.
- `open-cast.sh` — Opens `/cast` in Chrome's `--app` mode (standalone window,
  no browser chrome, keeps Chrome Tab capture in getDisplayMedia). Does NOT use
  `--class` — on Wayland, that flag sets the app_id process-wide, so if the cast
  window starts Chrome first (e.g. after reboot), all Chrome windows inherit the
  teleprompter icon. Without `--class`, the cast window groups with Chrome in the
  taskbar, which is the correct behavior for tab capture to work. Shows a zenity
  error dialog if the server isn't running.
- `start-mirror.sh` — One-command USB tethering + server startup. Supports
  `usb` (full setup), `reconnect` (re-enable USB without restarting server),
  and no-argument (server only) modes. `lib/usb.sh` selects the interface by
  driver (`rndis_host` or `cdc_ether`) and USB identity (`04e8:6864`), rejects
  ambiguous matches, and tolerates no match while USB modes change.
  Before routing/firewall changes, the launcher
  checks the active profile name and modifies it by UUID. The NM dispatcher
  uses its event's connection ID/UUID, avoiding substring and duplicate-name
  matches against unrelated active profiles.
- `teleprompter-mirror.service` — systemd user service template. Installed to
  `~/.config/systemd/user/` by `install.sh`. Auto-starts the mirror server at
  login (`WantedBy=graphical-session.target`), restarts on failure, stops on
  logout (`PartOf`). Uses `__PROJECT_DIR__` placeholder like the desktop entry.

### Focusing the shared window

With the cast app in front of you it is easy to lose the window it is
mirroring. Raising that window again is a three-part problem, and the awkward
part is not the raising:

- **Nothing in the browser can do it.** Chrome's Conditional Focus API
  (`CaptureController.setFocusBehavior()`) is only valid in the instant the
  capture starts and throws afterwards, and `getDisplayMedia()` deliberately
  tells the page nothing identifying about the window the portal handed over.
- **Nothing outside gnome-shell can do it either.**
  `org.gnome.Shell.Introspect.GetWindows` is allowlisted to the desktop
  portals and answers `AccessDenied` to everyone else; `org.gnome.Shell.Eval`
  needs unsafe-mode; `wmctrl`/`xdotool` are X11-only; and Chrome owns no
  session bus name, so `org.freedesktop.Application.Activate` is not an
  option. Hence the extension.
- **Which window is shared is not reported anywhere**, so it is inferred.
  Chrome focuses the captured surface when capture starts (the Conditional
  Focus default, which `cast.html` asks for explicitly via `CaptureController`),
  so the window that takes focus away from the cast window right after
  `getDisplayMedia()` resolves is the captured one. `adoptSharedWindow()`
  polls `/windows` for up to 2s looking for that change and remembers the
  result; the picker is the manual override when it does not happen.

Components:

- `system/gnome-extension/teleprompter-focus@teleprompter-mirror.local/` —
  ~80-line GNOME Shell extension exporting
  `org.gnome.Shell.Extensions.TeleprompterFocus` with `List` (JSON, so the
  Python side stays stdlib-only) and `Activate(id)`. Exported on gnome-shell's
  own connection, so the D-Bus destination is `org.gnome.Shell`. `List`
  returns `global.display.get_tab_list()` order — most recently used first —
  with each window's id, title, `wm_class`, size, and focus state. `Activate`
  calls `Main.activateWindow()`, which also switches workspace and is immune
  to focus-stealing prevention because the compositor itself is raising it.
- `lib/window_focus.py` — selection logic plus the D-Bus calls, which shell out
  to `busctl --json=short` (the only stdlib-friendly way to speak D-Bus).
  `select_window()` is pure and carries the matching rules; everything that
  touches the bus is in `_call()`.
- `app/mirror-server.py` — `GET /windows`, `GET|POST /focus/target`, `POST /focus`.
  Handled *before* the signaling lock is taken: they wait on the shell and
  must not stall an SDP exchange. Failures answer 409 with a JSON `error`,
  because the usual cause ("no window matches") is desktop state, not a server
  fault.
- `bin/focus-shared.sh` — same thing without the server, for the desktop
  entry's `Focus Shared Window` action and for a custom keyboard shortcut.
- `system/extension-state.py` — enables/disables the extension by editing
  gsettings, used by `install.sh` and `uninstall.sh`. Its `plan()` is pure and
  tested.

Lifetimes to preserve when touching this code:

- **The focus bridge must never block casting.** `startCast()` kicks off the
  baseline window lookup but does not await it: `getDisplayMedia()` needs the
  click's transient activation, which expires after five seconds, so an
  awaited call to a wedged shell would cost the user the ability to share at
  all. Every focus request is bounded (`FOCUS_TIMEOUT_MS` in the page,
  `CALL_TIMEOUT` in `window_focus.py`).
- **Adoption owns its capture and the target generation it started with.**
  `adoptSharedWindow()` re-checks `mine()` after every await, so Stop, a
  replacement capture, and a manual pick all invalidate work in flight. This
  is the same discipline the connection code follows, and `browser.test.js`
  has a regression test per case.
- **The target file has several writers** (the threaded server, the CLI), so
  `save_target()` writes a sibling temporary file and `os.replace()`s it in.

What the three capture types mean for the target:

| `displaySurface` | Behavior |
|---|---|
| `browser` (a tab) | Chrome focuses the tab, so the **window hosting it** is adopted. Raising a specific tab is not possible from outside the browser — if you switch tabs in that window afterwards, Focus Shared brings up the window, not the tab. |
| `window` | Adopted when the compositor actually raises it. On Wayland Chrome cannot raise another application's window, so this often falls through to the picker. |
| `monitor` | Nothing specific is shared, so the existing target is left alone — "the window I keep losing" is still a meaningful thing to raise while sharing a whole screen. No auto-adoption, no prompt. |

Matching rules (`select_window`), strongest first: a remembered **window id**
(plus agreeing class, since ids are per-session) pins one exact window — this
is what makes a choice stick when several browser windows share a class; then
class plus exact title; then class alone. A remembered target always wins over
the `TELEPROMPTER_FOCUS_MATCH` pattern. Titles are only ever a bonus: call
windows rename themselves constantly, and a browser window is named after
whichever tab is active. Among equals, a window that is **not** currently
focused wins — the cast page runs in a Chrome window, so it shares `wm_class`
with a shared Chrome window and would otherwise re-focus itself.

The remembered target lives in `~/.config/teleprompter-mirror/focus-target.json`
rather than the page's `localStorage` so the cast button, the desktop action,
and a keyboard shortcut all raise the same window.

### KVM switch automation

When the tablet and camera WiFi adapter are on a USB KVM switch,
disconnects/reconnects reset everything. System hooks automate recovery:

- `99-teleprompter-tablet.rules` — udev rule. Detects Samsung tablet connecting
  in MTP mode (`04e8:6860`) and triggers a systemd service that opens tethering
  settings on the tablet via ADB. The user just taps the toggle.
- `99-teleprompter` — NetworkManager dispatcher. Fires when `usb0` comes up
  after tethering is enabled. Fixes routing (never-default), firewall (trusted
  zone), and ADB reverse port forwarding. No user action needed. Uses a
  filter: connection name must match `"Wired connection"*` AND
  the network driver must be `rndis_host` or `cdc_ether` (Android USB
  tethering), with USB identity `04e8:6864` matching the tethering udev rule.
  The identity check also excludes ordinary `cdc_ether` dongles. This prevents
  poisoning Thunderbolt dock ethernet, which also auto-creates as
  `"Wired connection N"` before being renamed.
- `99-teleprompter-camera` — NetworkManager dispatcher. Acts on `up` (runs
  `camera-control.py reconnect`, which waits for the camera to answer then
  applies the same NotReady gate as `start`), `down`
  (logs disconnection), and `dhcp4-change` (logs WiFi station metrics from
  `iw` for retrospective analysis). On `dhcp4-change` it then runs
  `camera-control.py start`. Both `reconnect` and `start` are self-gating: they
  check camera status via
  `getEvent` and only call `startRecMode` (+ zoom restore) if the camera is in
  NotReady (brief WiFi blips may not reset Smart Remote). `reconnect` used to
  restore zoom unconditionally, which corrupted a good zoom on every brief RF
  flap — worse, a reconnect rides in on a still-weak link, and the timed zoom
  restore over that link stranded the lens at 100/100 (see the timed-zoom gotcha
  below). Gating both paths on actual camera state fixes this. This replaced an
  earlier heuristic that grepped the kernel log for a `disassociated` line —
  that missed power-loss drops, which log `authentication timed out` instead of
  a disassociation and so left the camera stuck in NotReady. Verifying actual
  camera state catches every drop type; the cost is one cheap read-only
  `getEvent` per DHCP renewal (~every 27 min). Also manages the keepalive service lifecycle:
  starts `teleprompter-camera-keepalive.service` on `up`, stops it on `down`.
  Uses `CONNECTION_ID` env var to identify the connection.
- `teleprompter-camera-keepalive.service` — systemd user service. Runs
  `camera-control.py keepalive`, which polls `getEvent` every 10s to prevent
  the camera AP from kicking the client for WiFi inactivity. Not auto-started
  at login; the NM dispatcher starts/stops it when camera WiFi connects/
  disconnects. The poll itself is read-only, which is what makes a 10s
  interval safe. It is also the **recovery backstop**: a dispatcher recovery
  can fail and exit, and nothing else then looks at the camera until the next
  DHCP renewal ~27 min later, so after `KEEPALIVE_NOTREADY_THRESHOLD`
  consecutive `NotReady` polls (~30s — long enough for a dispatcher recovery
  to claim the lock) it runs `recover()` itself. It takes `lens_lock()` for
  that escalation only, never for the service's lifetime, and declining
  (`LensBusy`) is a normal outcome meaning someone else is already on it.
  `KEEPALIVE_RECOVERY_COOLDOWN` keeps it from hammering a camera whose AP has
  wedged and needs a physical power cycle.
- `99-teleprompter-tether.rules` — udev rule. Detects the Samsung tablet connecting
  in tethering+ADB mode (`04e8:6864`) and triggers `teleprompter-adb-reverse.service`.
  Covers the case where the laptop disconnects and reconnects (suspend/resume, KVM
  switch, dock) while the tablet stays in tethering mode — `usb0` may not fully
  cycle down/up so the NM dispatcher doesn't fire, but ADB reverse state is lost.
  Complements the NM dispatcher's `adb reverse` (which handles fresh tethering).
- `teleprompter-adb-reverse.service` — systemd one-shot service triggered by the
  tethering udev rule. Waits for ADB readiness (`adb wait-for-device`) then
  re-establishes `adb reverse tcp:8047 tcp:8047`.
- `99-teleprompter-wifi.rules` — udev rule. Detects the Atheros AR9271 USB WiFi
  adapter (`0cf3:9271`) and triggers `teleprompter-wifi-rebind.service`. After
  KVM switches or port changes, the `ath9k_htc` driver can fail to claim the
  USB interface despite successful enumeration (no `wlan0` created). The service
  waits for normal probe, then forces a USB re-probe if needed. (Replaced the
  original MT7601U `148f:7601`, whose receiver failed 2026-09.)
- `teleprompter-tether-prompt.service` — systemd one-shot service triggered by
  the tablet udev rule. Runs `adb shell am start` as the user.
- `teleprompter-wifi-rebind.service` — systemd one-shot service triggered by
  the WiFi adapter udev rule. Runs `wifi-rebind.sh` as root (needs sysfs
  write access to toggle USB device authorization).
- `wifi-rebind.sh` — Recovery script for the AR9271. Waits 5 seconds for
  the driver to probe normally, then checks for `wlan0`. If missing, finds the
  device in sysfs and toggles its `authorized` attribute to force re-enumeration
  and driver re-probe. Retries up to 3 times. Logs to `teleprompter-wifi`
  syslog tag. If the device dropped from sysfs entirely (EPROTO), logs a
  warning — physical replug is needed.
- `install.sh` — Installs system hooks, desktop entry, user service, and the
  GNOME Shell extension (run with sudo). Substitutes `__USER__` and
  `__PROJECT_DIR__` placeholders with runtime values so the source files
  contain no hardcoded paths or usernames. The extension is copied into the
  user's `~/.local/share/gnome-shell/extensions/` and enabled via
  `gnome-extensions enable` plus `system/extension-state.py` (see the gotcha
  below).
- `uninstall.sh` — Removes all files installed by `install.sh` (including the
  user service — disables and stops it first).
- `teleprompter-mirror.desktop` — Desktop entry template. Installed to
  `~/.local/share/applications/` by `install.sh`.

### Camera control

- `camera-control.py` — Controls the Sony A6300 camera via Sony's Camera Remote
  API (JSON-RPC over WiFi). Requires the camera to be in Movie mode with Smart
  Remote Embedded running. A dedicated USB WiFi adapter (Atheros AR9271, `wlan0`)
  connects to the camera's WiFi AP (`DIRECT-xxxx:ILCE-6300`) via the `Camera-A6300` NM
  profile, leaving the main WiFi free for internet. HDMI capture continues working
  simultaneously. Supports zoom in/out (power zoom lens only), refocus nudge
  (zoom in+out to trigger AF-C), and status queries. No external deps (stdlib only).
  The camera's WiFi AP uses `192.168.122.0/24` — libvirt's default network was
  moved to `192.168.124.0/24` to avoid a subnet collision.
  Commands that can actuate the lens or restart Smart Remote (`zoom` with a
  direction, `refocus`, `reconnect`, `start`) take an exclusive `flock` on
  `~/.config/teleprompter-mirror/camera.lock` for their whole run; read-only
  commands (`status`, bare `zoom`, `discover`, `apis`) never take it and stay
  answerable during a recovery. `actuates_lens()` is pure and decides which is
  which; `lens_lock()` does the locking. `keepalive` is deliberately on the
  read-only side despite being able to recover — it runs for hours, so it
  takes the lock around its escalation only. See the concurrent-recovery
  gotcha below for why any of this exists.

  Recovery (`recover()`, shared by `start` and `reconnect`) runs in verified
  passes: restart rec mode, park the lens, then **re-read the camera before
  releasing the lock**. A pass that ends `NotReady` again means the camera
  reset mid-recovery, so the work repeats rather than being reported as done.
  Zoom restore drives to `TELEPROMPTER_CAMERA_ZOOM_TARGET` in a closed loop
  (`zoom_to()`), measuring the motor's speed from each move rather than
  trusting a constant.

### Tests

- `tests/` holds stdlib `unittest` tests (no pytest dependency). `run-tests.sh`
  runs them plus `py_compile`, `bash -n`, and required `shellcheck`. Each failed
  check propagates a nonzero exit status; temporary fixtures use `~/tmp`.
  There's no CI — run it locally.
- `tests/browser.test.js` runs the inline page scripts in Node.js VM contexts
  with controlled peers, fetches, and timers. It covers reconnects, cancellation,
  and stale asynchronous work, without npm packages or live media hardware.
- `app/mirror-server.py` and `camera/camera-control.py` have hyphens, so they
  can't be imported normally. `tests/loader.py` loads them by path via
  `importlib` and puts `lib/` on `sys.path` so `import teleprompter_config`
  resolves.
- Network/hardware code is kept at arm's length from logic so the pure parts are
  testable: `_fix_mdns` (SDP rewriting), `parse_device_descriptor` (camera
  SSDP XML), and `select_window` (window matching) take values and return
  values, with no I/O.
- `run-tests.sh` also parses the GNOME extension with `node --check` (Node 22
  detects the ES module syntax). Without it a typo there only surfaces as
  "extension failed to load" at the next login. The check is guarded by a
  `-f` test because `test_run_tests.py` runs the script against a fixture tree
  containing only `run-tests.sh`.
- The extension's own behavior cannot be tested offline — it only runs inside
  gnome-shell. The Python side of the bridge can be: point
  `window_focus.BUS_NAME` at a stub service that exports the same interface.

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
- A newly installed GNOME Shell extension is invisible to the running shell:
  `gnome-extensions enable` answers "Extension does not exist", and
  `org.gnome.Shell.Extensions.ReloadExtension` now returns
  `NotSupported: ReloadExtension is deprecated and does not work`. On Wayland
  the shell cannot be restarted in place either, so **a logout/login is
  required** after installing or changing `teleprompter-focus@…`. That is why
  `install.sh` also runs `system/extension-state.py`, which writes the
  gsettings keys directly. Note it must clear `disabled-extensions` as well:
  GNOME's schema says that key "takes precedence over the enabled-extensions
  setting", so for an extension the user once switched off, adding it to the
  enabled list alone reports success and changes nothing. While the extension is
  absent, `busctl` reports `Object does not exist at path …`; `window_focus.py`
  translates that (and the related unknown-interface/method errors) into an
  actionable "install it and log out" message rather than a raw D-Bus error.
- GStreamer's `pipewiresrc` cannot consume GNOME Shell's screencast portal
  streams on GNOME 49 / PipeWire 1.4.x. GNOME creates the screencast node
  with `object.register=false`, making it invisible to pipewiresrc's
  registry-based discovery. This blocks any native PipeWire capture approach;
  a prototype attempting it was removed (see git history). The working solution
  is `app/cast.html`'s crop mode, which uses Chrome's `getDisplayMedia()` +
  canvas crop instead.
- The tablet is a Samsung Galaxy Tab A7 (SM-T500), Wi-Fi only, Android 12.
  USB tethering works despite being Wi-Fi only.
- The Sony A6300's WiFi AP hardcodes `192.168.122.0/24`, which collides with
  libvirt's default `virbr0` bridge. The libvirt default network was moved to
  `192.168.124.0/24` to fix this.
- The A6300's Camera Remote API only exposes focus control methods when the
  full Smart Remote Control app is installed (not Smart Remote Embedded). Sony
  discontinued PlayMemories Camera Apps, so the upgrade is no longer available.
  Workaround: use AF-C mode and trigger refocus via a small zoom nudge.
- The A6300's WiFi AP was initially observed crashing under frequent HTTP
  requests (keepalive at 60s caused 0.55 disassociations/hr). However, later
  testing showed that **read-only `getEvent` polling is safe** — 6 req/min
  for 17+ hours (6,000+ requests) caused zero AP instability. The crashes
  were likely caused by write operations (`startRecMode`, `actZoom`) combined
  with other stressors (USB autosuspend, NM background scanning) that have
  since been fixed. A 10-second `getEvent` keepalive now runs permanently
  via `teleprompter-camera-keepalive.service` to prevent inactivity kicks.
- The A6300's WiFi AP also disassociates idle clients (802.11 Reason 4:
  DISASSOC_DUE_TO_INACTIVITY). This is more frequent after laptop reboots —
  observed as clusters of exactly 10-minute-interval kicks that taper off over
  1–2 hours, then the connection stabilizes for days. Hypothesis: NM does
  aggressive background scanning on a fresh boot, taking wlan0 off-channel
  and making the adapter miss the AP's keepalive probes. As NM reduces scan
  frequency, the disconnects stop. BSSID locking (`TELEPROMPTER_CAMERA_BSSID`)
  disables NM background scanning on the camera adapter, which significantly
  reduces Reason 4 events. Not yet fully confirmed — needs testing with
  `iw event` capture across a reboot cycle. The `dhcp4-change` dispatcher
  handler catches these silent re-associations and runs `camera-control.py
  start`, which checks camera status via `getEvent` and only calls
  `startRecMode` if the camera actually reset to NotReady.
- Not every camera drop is an RF/inactivity event. A **power interruption to
  the camera** (the A6300 runs on a DC "fake battery"; a loose barrel/dummy
  connector or a brownout when a shared-circuit device like a sit/stand desk
  motor kicks in) resets Smart Remote and can make the WiFi AP go fully silent.
  Diagnostic tells: (1) `nmcli device wifi list` sees neighbor APs but **not**
  `DIRECT-*:ILCE-6300` — the AP is off-air, so trust the scan, not the camera's
  LCD, which can still show the "connect to SSID" / "Connecting…" screen while
  the radio is dead; (2) the kernel logs `wlan0: authentication ... timed out`
  (and *no* `disassociated (Reason:N)` line). A hard AP wedge needs a **camera
  power-cycle** (exiting/re-entering Smart Remote is not enough); a brief drop
  self-recovers but leaves Smart Remote in NotReady until the dispatcher's
  `start` re-runs `startRecMode`. The USB WiFi dongle is never the victim in
  these — it keeps scanning fine — which is how you know it's the camera, not
  the adapter.
- The camera dongle's `iw station dump` `beacon_loss` is a cumulative counter
  that persists across re-associations (resets on driver load). Its *rate of change*
  — not its absolute value — is a useful diagnostic: during stable operation it
  barely moves (single digits over many hours), but it spikes during 2.4 GHz
  channel congestion **while signal strength stays strong**, which makes it a
  leading indicator of an impending disassociation. Example (2026-09-01, ch 6):
  flat at 4→5 over 8h, then 5→50 during a ~1h evening interference episode that
  ended in a Reason 2/3 drop, then flat again 50→51 over the following 10h. (An
  earlier note that this field is stuck at a fixed 88 no longer holds — likely a
  prior driver version; verify on the running kernel before trusting old values.)
- **There is no noise floor reading on this adapter.** `iw dev wlan0 survey
  dump` exits 0 and prints nothing — `ath9k_htc` doesn't implement survey. That
  removes the one measurement that cleanly separates "interference raised the
  noise" from "attenuation lowered the signal", so use two fields together
  instead. `beacon signal avg` is the attenuation probe: beacons leave the AP at
  a fixed rate and power every 100 ms, so a drop there means the path got
  lossier (geometry, polarization, an obstruction). `beacon loss` climbing
  **while `beacon signal avg` holds steady** means beacons are being sent at
  normal strength and still not arriving — collisions or noise. Also note that
  ath9k reports signal relative to its own noise-floor calibration, so the dBm
  figure is not an absolute power measurement and shouldn't be read as one.
  A sampler for live experiments (`station dump` is read-only, adds no traffic):

  ```bash
  while :; do iw dev wlan0 station dump 2>/dev/null | awk -v t="$(date +%H:%M:%S)" '
    $1=="signal:" {s=$2} $1=="signal"&&$2=="avg:" {a=$3}
    $1=="beacon"&&$2=="signal" {b=$4} $1=="beacon"&&$2=="loss:" {l=$3}
    $1=="rx"&&$2=="bitrate:" {rx=$3} $1=="tx"&&$2=="retries:" {q=$3}
    END {if(s=="") printf "%s -- DOWN --\n",t;
         else printf "%s sig=%-5s avg=%-5s beacon=%-5s loss=%-6s rx_mcs=%-6s retry=%s\n",t,s,a,b,l,rx,q}'
    sleep 2; done
  ```

  Pair it with `iw event -t` in another terminal for reason codes with
  timestamps.
- **An RF fade and a wedged camera are two failures, and they are routinely
  mistaken for one.** They arrive together and both leave a dead feed, so the
  natural reading is "the camera broke and only a power cycle fixed it". The
  2026-10-02 episode separates them, because the fade *outlived the reset*:

  | | RF fade | Camera wedged |
  |---|---|---|
  | Symptom | signal collapses, beacons lost, link flaps | `NotReady`, HDMI "Connecting", no API answer |
  | Power cycle | **does nothing** | the only fix (modes 3/4 above) |
  | Recovery | on its own, link stays associated | needs `startRecMode` or a reset |

  Timeline: beacon loss began storming at 15:44:02, 93 seconds before any
  disconnect; signal went -57 → -82 → -86 dBm; five associate/drop cycles
  followed with `getEvent` returning `No route to host` (associated at L2, ARP
  failing). A camera power cycle at ~15:47 restored Smart Remote and
  reachability but **left the signal at -80 dBm**, where it stayed for at
  least eight more minutes. It returned to -57 dBm by 16:18 with no
  disconnect, no reassociation and no second power cycle in any log, then held
  -52 to -56 dBm for the following 2h+ on one unbroken association
  (`connected time` confirms it). So the reset fixed the camera, not the link.
  When diagnosing, establish which of the two you are looking at before
  reaching for a remedy — and check `connected time` and the dispatcher's
  `signal=` samples, which together show whether anything actually dropped.

  The fade's tell is `locally_generated=1` on the supplicant's disconnect:
  *our* side stopped hearing the AP rather than the AP kicking us. The reason
  code is **also 4**, identical to the inactivity kick the keepalive already
  fixed, so the code alone cannot distinguish them — read the flag. Two log
  lines mislead here: `authentication timed out` appears but is interleaved
  with *successful* authentications, so unlike the power-loss case it does not
  mean the AP went off-air, and `4-Way Handshake failed - pre-shared key may
  be incorrect` is a degraded link losing handshake frames, never a real PSK
  problem. Treat the fade's *magnitude* with suspicion too — ath9k derives
  reported dBm from its own periodic noise-floor calibration, which an
  interference episode can skew, so part of a large apparent swing may be the
  measurement rather than the signal.

  **Cause not established.** Desk-height multipath, a dongle or cable rotating,
  USB 3 emissions near the dongle, and unrelated 2.4 GHz traffic all remain
  open. Deliberate desk raise/lower and motor-burst tests were inconclusive on
  2026-10-02, as an earlier round was in 2026-09 — but note the 15:56–16:18
  recovery window was never correlated against what was physically happening,
  so "inconclusive" here means untested rather than refuted. Don't record this
  as solved.
- **Timed zoom control is only valid over a fast link.** `actZoom` is a
  fire-and-forget start/stop pair; `zoom_timed` runs the motor for the
  wall-clock gap between them, so it assumes each call round-trips in ~100ms.
  Over a degraded camera link those calls block for seconds (2-7s seen during a
  weak-signal reconnect at -84 dBm), the motor runs uncontrolled, and the lens
  overshoots — once stranding it at 100/100 while the code logged "Zoom
  restored". Guards: `zoom_timed` raises `ZoomTimingError` when an `actZoom`
  exceeds `ZOOM_LATENCY_LIMIT` (2s), and `restore_zoom` rejects a final position
  above `ZOOM_RESTORE_CEILING` (75) and retries with backoff — letting the link
  settle rather than mis-actuating. If `zoom set` reports "link too slow", check
  `iw dev wlan0 link` / signal and retry once the link recovers.
  Every timed CLI move uses this helper, validates duration before moving, and
  attempts `stop` in `finally`, including a failed start or Ctrl-C during the
  hold. A disconnected link can still prevent the stop from reaching the lens.
  Missing or invalid zoom telemetry is an error, never an assumed position of 0.

  **The restore is closed-loop, because open-loop timed moves don't repeat.**
  Running the motor a fixed 1.2s from zero and accepting wherever it stopped
  parked the same lens at 54, 48 and 45 on three consecutive recoveries — a
  one-way ratchet that re-framed the shot a little each time. `zoom_to()`
  reads the real position back after every move and corrects, so the error
  shrinks instead of accumulating; four real runs afterward landed 45, 51, 45,
  45 against a target of 50. It also **measures the motor's speed as it goes**
  rather than trusting `ZOOM_RATE_GUESS`: a fixed figure that underestimates
  the lens by 2x makes a proportional controller overshoot, reverse, overshoot
  again and never settle.

  Two measured facts constrain that loop (see *Zoom positions* in
  `docs/CAMERA.md`). The motor has a minimum **travel**, not a minimum pulse
  width — commands of 0.05s, 0.08s, 0.10s and 0.15s each moved the lens about
  10 of 100 positions — so the lens cannot be parked more precisely than about
  ±5 and `ZOOM_TOLERANCE` is derived from that, not chosen. And a short move
  says nothing about speed: calibrating from one would read 10 ÷ 0.05s = 200
  positions/second, so only moves of at least `ZOOM_LINEAR_MIN_STEP` update
  the estimate.

  Recovery also exits nonzero for missing camera status, failed `startRecMode`,
  or exhausted zoom retries, so a failed recovery cannot look successful.
- **A link flap can start several recoveries that then share one motor.** The
  camera dispatcher spawns a recovery on every `up` and every `dhcp4-change`,
  backgrounded, and a slow link makes each one outlive the next event. On
  2026-10-02 a five-flap minute left three `camera-control.py` processes
  restoring zoom simultaneously; each read a position the other two were
  already correcting, and the lens landed at 58 instead of 54. The timed-zoom
  guards above cannot see this — `ZOOM_LATENCY_LIMIT` measures one caller's own
  round trips and knows nothing of a second actuator interleaving its
  start/stop pair. Fix is `lens_lock()`: an exclusive, **non-blocking** `flock`
  held for the whole command. Non-blocking matters — a queued recovery would
  act on camera state it read minutes earlier, so declining and leaving the
  outcome to the running holder is the correct answer. A declined run logs
  `Lens busy: pid N …` and exits 1, so it can't be mistaken for work done.
  The lock file is under `~/.config` rather than `XDG_RUNTIME_DIR`, where a
  lock would normally belong: the dispatcher invokes the script through
  `runuser`, which sets `HOME` but leaves `XDG_RUNTIME_DIR` unset (hence the
  explicit `XDG_DIR` the dispatcher passes to its `systemctl --user` calls), so
  a runtime-dir path would give dispatcher recoveries a different lock file
  from the CLI's and no mutual exclusion at all. `flock` state lives in the
  kernel, so the file outliving a reboot is harmless and a killed holder leaves
  nothing stale behind.
- **A non-blocking decline is only safe if the holder finishes the job, and on
  2026-10-06 it didn't.** A six-reassociation storm (12:48:19–12:49:07) hit
  while a recovery held the lens lock: it ran `startRecMode`, lost the link
  mid-restore, and spent 50s in zoom backoff. Four later recoveries were
  declined during that window — including the one triggered by the final,
  *good* re-association at -57 dBm. The holder then exhausted its retries and
  exited, leaving the camera `NotReady` behind a healthy link with nothing
  scheduled to look at it for ~27 minutes. The decline had silently consumed
  the one trigger that would have worked. Note this is not an argument for a
  blocking lock — the reasoning above still holds. Three changes close it, and
  all three are load-bearing:
  - `recover()` re-reads camera state **before releasing the lock** and
    repeats the pass if the camera reset again, so "done" is an honest claim.
  - `restore_zoom` answers a `CameraReset` by calling `startRecMode` rather
    than backing off (see the timed-zoom gotcha).
  - the keepalive escalates when the camera stays `NotReady`, so a recovery
    that failed and exited is not the last word.

  Diagnosing a repeat: `journalctl -t teleprompter-camera` and look for
  `Lens busy` lines clustered around a flap, followed by a `Zoom restore
  failed` with no later recovery.
- **"Not Available Now" (Sony error 1) means the camera left rec mode, not
  that the link is slow.** The two need opposite responses: a slow link is
  answered by waiting, a reset only by `startRecMode`. `restore_zoom`
  originally caught both as one generic failure, so during the 2026-10-06
  storm it spent its last 40 seconds pushing `actZoom` at a camera that
  rejected every one — backing off from a condition that waiting cannot fix.
  `api_call(..., detect_reset=True)` now raises `CameraReset` for that code,
  and only `_actzoom` asks for it, so read-only callers keep the old behavior.
- The camera must be in Movie mode for clean high-res HDMI output. Still/P mode
  outputs a low-resolution LCD mirror over HDMI.
- Camera WiFi uses `ipv4.never-default yes` to avoid stealing the default route.
  A dedicated USB WiFi adapter (Atheros AR9271, `wlan0`) connects to the camera via the
  `Camera-A6300` NM profile, so the main WiFi (`wlp9s0`) stays on the home/office
  network. The profile auto-connects when the camera's AP is visible.
- The laptop's built-in webcam (Integrated RGB Camera) has higher PipeWire
  priority than the HDMI capture dongle by default, making it the default
  camera in Google Meet and other apps. A WirePlumber rule in
  `~/.config/wireplumber/wireplumber.conf.d/prefer-hdmi-capture.conf` boosts
  the HDMI capture's `priority.session` above the built-in camera so the
  external camera is preferred. Both cameras remain available in app dropdowns.
- The `99-teleprompter` NM dispatcher must only match tablet USB tethering
  connections, not all USB ethernet. It checks the NM connection
  name (`Wired connection *`), network driver (`rndis_host` or `cdc_ether`),
  and Samsung tethering USB identity (`04e8:6864`).
  Connection name alone is insufficient — Thunderbolt dock ethernet also
  auto-creates as `"Wired connection N"` and would get `never-default yes` set
  on it, breaking internet connectivity. The USB identity is necessary because
  ordinary Ethernet devices can also use `cdc_ether`.

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
- **Automated guard**: `.githooks/pre-commit` scans staged additions for MAC
  addresses, real home paths / usernames, credential assignments, WiFi PSK/SSID
  values, and private keys, and blocks the commit on a match. It is **not**
  active until enabled per clone — run `git config core.hooksPath .githooks`
  once after cloning. Documented placeholders are allow-listed; extend the
  `USERNAMES`/`ALLOW` lists in the hook when adding a new machine or template
  value. Bypass a vetted false positive with `git commit --no-verify`.

### Network isolation

- **Routing priority**: the wired Ethernet profile is the primary internet
  uplink; the home/office Wi-Fi profile is the automatic fallback. Both must keep
  `ipv4.never-default no` (the default). Tablet USB (`Wired connection N`) and
  camera WiFi (the `TELEPROMPTER_CAMERA_CONNECTION` profile) are local-only —
  both use `never-default yes`
  and must never carry a default route. NM auto-assigns metrics (~100 for
  ethernet, ~600 for WiFi) and adds +20,000 to WiFi when a wired default
  exists, so no explicit metrics are needed.
- **Servers bind to localhost only** (`127.0.0.1`). The tablet reaches them via
  ADB reverse port forwarding. Never bind to `0.0.0.0` or a LAN address.
- **Localhost is not a trust boundary against other pages.** Binding to
  `127.0.0.1` keeps other machines out, but any site the user has open can
  POST to the server: a cross-origin `fetch(..., {mode: 'no-cors'})` cannot
  read the reply, yet the side effect still happens. `Handler._authorized()`
  therefore requires (a) a `Host` header naming the loopback interface, which
  blocks DNS rebinding — a name resolving to `127.0.0.1` would otherwise serve
  our own pages under the attacker's origin and make every route readable to
  them — and (b) on the acting and desktop-exposing routes, an `Origin` that
  is one of ours. A missing `Origin` is allowed: browsers always send one on
  POST, so those requests come from local tools, which already have the
  session bus. Keep new routes behind this guard.
- **`/windows` and `/focus` are reachable from the tablet** — ADB reverse
  connections arrive from `127.0.0.1`, so they are indistinguishable from
  local ones. Keep these endpoints to listing and raising windows that already
  exist. Never let them launch, close, or send input to anything.
- **USB tethering interfaces go in the `trusted` firewall zone** at runtime only
  (no `--permanent`). All other interfaces stay in their default restricted zones.
- **Camera WiFi uses a dedicated adapter** (`wlan0`) with `never-default yes` so
  it never becomes a route to the internet.
### Dispatcher and system hooks

- **Match tablet connections by NM connection name, driver, and USB identity.** Name
  alone (`Wired connection *`) is ambiguous — Thunderbolt dock ethernet also
  auto-creates with that name. The driver check (`rndis_host` or `cdc_ether`)
  plus the Samsung tethering USB identity (`04e8:6864`) selects this tablet.
  Keep that identity in `lib/usb.sh`, the dispatcher, and the tethering udev
  rule synchronized when adapting the project to different hardware.
- **Never set `never-default` or change firewall zones** on named connection
  profiles (like `Ethernet`). Only auto-created `Wired connection N` profiles
  with a tethering driver should be modified by dispatchers.

## Environment

- Fedora Linux, GNOME 49, Wayland, PipeWire
- No external Python dependencies — stdlib only
- Tablet: Android with Chrome
- Camera: Sony A6300 (ILCE-6300) with E PZ 16-50mm power zoom kit lens, HDMI
  capture via USB adapter
