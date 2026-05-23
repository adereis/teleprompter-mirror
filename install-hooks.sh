#!/bin/bash
# Install udev rule and NM dispatcher for automatic KVM switch recovery.
# Run with sudo.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Installing udev rule..."
cp "$SCRIPT_DIR/99-teleprompter-tablet.rules" /etc/udev/rules.d/
udevadm control --reload-rules

echo "Installing systemd service..."
cp "$SCRIPT_DIR/teleprompter-tether-prompt.service" /etc/systemd/system/
systemctl daemon-reload

echo "Installing NetworkManager dispatcher..."
cp "$SCRIPT_DIR/99-teleprompter" /etc/NetworkManager/dispatcher.d/
chmod +x /etc/NetworkManager/dispatcher.d/99-teleprompter

echo "Done. KVM switch recovery is now automatic."
echo "  udev:  /etc/udev/rules.d/99-teleprompter-tablet.rules"
echo "  systemd: /etc/systemd/system/teleprompter-tether-prompt.service"
echo "  NM:   /etc/NetworkManager/dispatcher.d/99-teleprompter"
