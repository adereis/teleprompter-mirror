# Helpers for identifying tablet tethering without changing other interfaces.
# shellcheck shell=bash

find_tablet_interface() {
    local net_root="${1:-/sys/class/net}" path driver vendor product selected=""
    for path in "$net_root"/*; do
        # Loopback, virtual devices, and unplugged devices have no USB driver.
        [ -L "$path/device/driver" ] || continue
        driver=$(readlink "$path/device/driver") || return 1
        case "${driver##*/}" in rndis_host|cdc_ether) ;; *) continue ;; esac
        # cdc_ether is also used by Ethernet dongles. Match the tablet's USB
        # tethering identity, as the udev rule does, before trusting a link.
        vendor=$(cat "$path/device/../idVendor") || return 1
        product=$(cat "$path/device/../idProduct") || return 1
        [ "$vendor:$product" = "04e8:6864" ] || continue
        if [ -n "$selected" ]; then
            echo "Multiple tethering interfaces found; connect only the tablet." >&2
            return 1
        fi
        selected="${path##*/}"
    done
    # No match is expected while the tablet switches USB modes.
    printf '%s\n' "$selected"
}
