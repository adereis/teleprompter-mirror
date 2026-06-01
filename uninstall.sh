#!/bin/bash
# Remove system hooks and desktop entry installed by install.sh.
# Run with sudo.
set -euo pipefail

TARGET_USER="${SUDO_USER:?Run with sudo, not as root directly}"
TARGET_HOME="$(eval echo ~"$TARGET_USER")"

echo "Removing system hooks and desktop entry..."

rm -f /etc/udev/rules.d/99-teleprompter-tablet.rules
rm -f /etc/udev/rules.d/99-teleprompter-wifi.rules
udevadm control --reload-rules

rm -f /etc/systemd/system/teleprompter-tether-prompt.service
rm -f /etc/systemd/system/teleprompter-wifi-rebind.service
systemctl daemon-reload

rm -f /etc/NetworkManager/dispatcher.d/99-teleprompter
rm -f /etc/NetworkManager/dispatcher.d/99-teleprompter-camera

rm -f "$TARGET_HOME/.local/share/applications/teleprompter-mirror.desktop"
runuser -u "$TARGET_USER" -- update-desktop-database "$TARGET_HOME/.local/share/applications" 2>/dev/null || true

echo "Done. All Teleprompter Mirror system files removed."
