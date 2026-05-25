#!/bin/bash
# Install udev rule and NM dispatcher for automatic KVM switch recovery.
# Run with sudo.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET_USER="${SUDO_USER:?Run with sudo, not as root directly}"

echo "Installing for user: $TARGET_USER"

echo "Installing udev rule..."
cp "$SCRIPT_DIR/99-teleprompter-tablet.rules" /etc/udev/rules.d/
udevadm control --reload-rules

echo "Installing systemd service..."
sed "s/__USER__/$TARGET_USER/g" "$SCRIPT_DIR/teleprompter-tether-prompt.service" \
    > /etc/systemd/system/teleprompter-tether-prompt.service
systemctl daemon-reload

echo "Installing NetworkManager dispatcher..."
sed "s/__USER__/$TARGET_USER/g" "$SCRIPT_DIR/99-teleprompter" \
    > /etc/NetworkManager/dispatcher.d/99-teleprompter
chmod +x /etc/NetworkManager/dispatcher.d/99-teleprompter

echo "Done. KVM switch recovery is now automatic."
echo "  udev:  /etc/udev/rules.d/99-teleprompter-tablet.rules"
echo "  systemd: /etc/systemd/system/teleprompter-tether-prompt.service"
echo "  NM:   /etc/NetworkManager/dispatcher.d/99-teleprompter"
