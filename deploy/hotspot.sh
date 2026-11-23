#!/usr/bin/env bash
# Wi-Fi hotspot on the Pi, for riding where there is no Wi-Fi you know.
#
#     deploy/hotspot.sh on       # set it up (asks for a name and a password)
#     deploy/hotspot.sh off      # remove it
#     deploy/hotspot.sh status
#
# How it works: NetworkManager keeps using your normal Wi-Fi whenever it can
# see it. When it can't (out on the road), the Pi starts its own network
# instead. Connect your phone to it and open http://10.42.0.1:8080.
#
# The Pi decides at boot (or when the Wi-Fi drops). Once the hotspot is on, it
# stays on until you restart the Pi, even when your home Wi-Fi comes back.
#
# Setting it up never switches the Wi-Fi over by itself while the Pi is
# connected to a network, so it's safe to run over SSH.
set -euo pipefail

CON=bikecomputer-hotspot
IFACE="${IFACE:-wlan0}"

need_nm() {
    if ! command -v nmcli >/dev/null || ! nmcli -t -f RUNNING general 2>/dev/null | grep -q running; then
        echo "This needs NetworkManager (Raspberry Pi OS Bookworm or newer)." >&2
        exit 1
    fi
}

case "${1:-}" in
on)
    need_nm
    read -r -p "Hotspot name [BikeComputer]: " ssid
    ssid="${ssid:-BikeComputer}"
    while true; do
        read -r -s -p "Hotspot password (at least 8 characters): " pass; echo
        [[ ${#pass} -ge 8 && ${#pass} -le 63 ]] && break
        echo "The password must be 8 to 63 characters."
    done
    sudo nmcli connection delete "$CON" >/dev/null 2>&1 || true
    sudo nmcli connection add type wifi ifname "$IFACE" con-name "$CON" ssid "$ssid" \
        802-11-wireless.mode ap 802-11-wireless.band bg \
        ipv4.method shared ipv6.method disabled \
        802-11-wireless-security.key-mgmt wpa-psk 802-11-wireless-security.proto rsn \
        802-11-wireless-security.pairwise ccmp 802-11-wireless-security.group ccmp \
        802-11-wireless-security.psk "$pass" \
        connection.autoconnect yes connection.autoconnect-priority -10 >/dev/null
    echo
    echo "Hotspot '$ssid' is set up. When the Pi can't find a Wi-Fi it knows, it starts"
    echo "this network. Connect your phone to '$ssid' and open http://10.42.0.1:8080"
    ;;
off)
    need_nm
    if sudo nmcli connection delete "$CON" >/dev/null 2>&1; then
        echo "Hotspot removed."
    else
        echo "There was no hotspot set up."
    fi
    ;;
status)
    need_nm
    if nmcli -t -f NAME connection show | grep -qx "$CON"; then
        ssid="$(nmcli -g 802-11-wireless.ssid connection show "$CON")"
        if nmcli -t -f NAME connection show --active | grep -qx "$CON"; then
            echo "Hotspot '$ssid' is ON now. Phone: join '$ssid', open http://10.42.0.1:8080"
        else
            echo "Hotspot '$ssid' is set up, and starts when no known Wi-Fi is around."
        fi
    else
        echo "No hotspot set up. Run: deploy/hotspot.sh on"
    fi
    ;;
*)
    sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
