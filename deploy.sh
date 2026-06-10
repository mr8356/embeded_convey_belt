#!/usr/bin/env bash
# deploy.sh — conveyor MQTT infrastructure setup
#
# Topology:
#   DRIVER_PI  (10.10.10.12) : kernel driver + conveyor_event_daemon.py (publisher)
#   BROKER_PI  (10.10.11.12) : mosquitto broker + conveyor_subscriber.py
#
# Both Pis have no external network.
# This script runs on the Mac (which HAS internet) and pushes everything.

set -euo pipefail

DRIVER_PI="${DRIVER_PI:-pi@10.10.10.12}"
BROKER_PI="${BROKER_PI:-pi@10.10.11.12}"
DRIVER_DIR="~/conveyor_node_driver"
BROKER_DIR="~/conveyor_broker"
MQTT_TOPIC="iot/conveyor/state"
PKGS_DIR="$(dirname "$0")/.deb_cache"

ssh_q()  { ssh -o BatchMode=yes -o ConnectTimeout=5 "$@"; }
scp_q()  { scp -o BatchMode=yes -o ConnectTimeout=5 "$@"; }
remote() { local host=$1; shift; ssh_q "$host" "$@"; }

# ── helpers ────────────────────────────────────────────────────────────────────

check_connectivity() {
    echo "==> checking SSH connectivity"
    ssh_q "$DRIVER_PI" "echo driver-pi ok" || { echo "ERROR: cannot reach $DRIVER_PI"; exit 1; }
    ssh_q "$BROKER_PI" "echo broker-pi ok" || { echo "ERROR: cannot reach $BROKER_PI"; exit 1; }
}

detect_arch() {
    local host=$1
    remote "$host" "dpkg --print-architecture 2>/dev/null || uname -m"
}

# Download mosquitto + mosquitto-clients .deb files on the Mac for the given
# Debian/Raspbian architecture, then SCP them to the target Pi for dpkg install.
download_and_push_debs() {
    local host=$1
    local arch=$2
    mkdir -p "$PKGS_DIR/$arch"

    echo "  downloading mosquitto packages for $arch (Debian Bookworm) ..."

    # Resolve current package URLs from Debian package index
    local base="http://deb.debian.org/debian/dists/bookworm/main"
    local pkgs_file="$PKGS_DIR/$arch/Packages.gz"

    if [[ ! -f "$pkgs_file" ]]; then
        curl -sSL "${base}/binary-${arch}/Packages.gz" -o "$pkgs_file"
    fi

    # Extract .deb pool paths for mosquitto and mosquitto-clients
    for pkg in mosquitto mosquitto-clients; do
        local deb_path
        deb_path=$(zcat "$pkgs_file" | awk -v p="^Package: ${pkg}$" '
            $0 ~ p { found=1 }
            found && /^Filename:/ { print $2; found=0 }
        ' | head -1)

        if [[ -z "$deb_path" ]]; then
            echo "  WARNING: could not find $pkg in Packages index"
            continue
        fi

        local filename
        filename=$(basename "$deb_path")
        local local_deb="$PKGS_DIR/$arch/$filename"

        if [[ ! -f "$local_deb" ]]; then
            echo "  fetching $filename ..."
            curl -sSL "http://deb.debian.org/debian/${deb_path}" -o "$local_deb"
        else
            echo "  cached: $filename"
        fi
    done

    # Also grab libmosquitto1 (runtime dependency)
    for pkg in libmosquitto1; do
        local deb_path
        deb_path=$(zcat "$pkgs_file" | awk -v p="^Package: ${pkg}$" '
            $0 ~ p { found=1 }
            found && /^Filename:/ { print $2; found=0 }
        ' | head -1)
        if [[ -n "$deb_path" ]]; then
            local filename; filename=$(basename "$deb_path")
            local local_deb="$PKGS_DIR/$arch/$filename"
            [[ -f "$local_deb" ]] || curl -sSL "http://deb.debian.org/debian/${deb_path}" -o "$local_deb"
        fi
    done

    echo "  transferring .deb files to $host ..."
    remote "$host" "mkdir -p /tmp/mosquitto_debs"
    scp_q "$PKGS_DIR/$arch/"*.deb "${host}:/tmp/mosquitto_debs/"
    remote "$host" "sudo dpkg -i /tmp/mosquitto_debs/*.deb || sudo apt-get install -f -y"
    echo "  mosquitto installed offline."
}

install_mosquitto_if_needed() {
    local host=$1
    local pkgs=$2   # "mosquitto mosquitto-clients" or "mosquitto-clients"
    echo "==> checking mosquitto on $host"

    local need_install=0
    for p in $pkgs; do
        if ! remote "$host" "dpkg -s $p &>/dev/null"; then
            need_install=1
            break
        fi
    done

    if [[ $need_install -eq 0 ]]; then
        echo "  already installed."
        return 0
    fi

    # Try apt from local cache first (no internet needed if cache exists)
    echo "  trying apt from local cache ..."
    if remote "$host" "sudo apt-get install -y --no-install-recommends $pkgs 2>&1" | grep -q "Unable to fetch"; then
        echo "  apt cache miss — falling back to offline .deb transfer ..."
        local arch
        arch=$(detect_arch "$host")
        download_and_push_debs "$host" "$arch"
    fi
}

# ── broker Pi (10.10.11.12) setup ──────────────────────────────────────────────

setup_broker() {
    echo ""
    echo "==> setting up MQTT broker on $BROKER_PI"

    install_mosquitto_if_needed "$BROKER_PI" "mosquitto mosquitto-clients"

    remote "$BROKER_PI" "mkdir -p $BROKER_DIR"
    scp_q broker/mosquitto.conf          "${BROKER_PI}:${BROKER_DIR}/"
    scp_q broker/conveyor_subscriber.py  "${BROKER_PI}:${BROKER_DIR}/"
    scp_q broker/conveyor_web.py         "${BROKER_PI}:${BROKER_DIR}/"

    # Install custom conf and restart mosquitto
    remote "$BROKER_PI" "
        sudo cp ${BROKER_DIR}/mosquitto.conf /etc/mosquitto/conf.d/conveyor.conf
        sudo systemctl enable mosquitto
        sudo systemctl restart mosquitto
        sleep 1
        sudo systemctl is-active mosquitto && echo 'mosquitto running OK'
    "

    # Install systemd service for subscriber
    remote "$BROKER_PI" "sudo tee /etc/systemd/system/conveyor-subscriber.service > /dev/null" << 'EOF'
[Unit]
Description=Conveyor MQTT subscriber
After=network.target mosquitto.service
Requires=mosquitto.service

[Service]
ExecStart=/usr/bin/python3 /home/pi/conveyor_broker/conveyor_subscriber.py --broker localhost
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

    remote "$BROKER_PI" "
        sudo systemctl daemon-reload
        sudo systemctl enable conveyor-subscriber
        sudo systemctl restart conveyor-subscriber
        sleep 1
        sudo systemctl is-active conveyor-subscriber && echo 'subscriber running OK'
    "

    # Install systemd service for web UI
    remote "$BROKER_PI" "sudo tee /etc/systemd/system/conveyor-web.service > /dev/null" << 'EOF'
[Unit]
Description=Conveyor node web dashboard
After=network.target mosquitto.service

[Service]
ExecStart=/usr/bin/python3 /home/pi/conveyor_broker/conveyor_web.py --broker localhost --port 8080
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

    remote "$BROKER_PI" "
        sudo systemctl daemon-reload
        sudo systemctl enable conveyor-web
        sudo systemctl restart conveyor-web
        sleep 1
        sudo systemctl is-active conveyor-web && echo 'web UI running OK  →  http://10.10.11.12:8080'
    "

    echo "==> broker Pi setup complete."
}

# ── driver Pi (10.10.10.12) setup ──────────────────────────────────────────────

setup_driver() {
    echo ""
    echo "==> setting up event publisher on $DRIVER_PI"

    install_mosquitto_if_needed "$DRIVER_PI" "mosquitto-clients"

    remote "$DRIVER_PI" "mkdir -p $DRIVER_DIR"
    scp_q conveyor_event_daemon.py "${DRIVER_PI}:${DRIVER_DIR}/"

    # Install systemd service for publisher daemon
    remote "$DRIVER_PI" "sudo tee /etc/systemd/system/conveyor-publisher.service > /dev/null" << EOF
[Unit]
Description=Conveyor event MQTT publisher
After=network.target

[Service]
ExecStart=/usr/bin/python3 ${DRIVER_DIR}/conveyor_event_daemon.py --broker 10.10.11.12 --topic ${MQTT_TOPIC}
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

    remote "$DRIVER_PI" "
        sudo systemctl daemon-reload
        sudo systemctl enable conveyor-publisher
        sudo systemctl restart conveyor-publisher
        sleep 1
        sudo systemctl is-active conveyor-publisher && echo 'publisher running OK'
    "

    echo "==> driver Pi setup complete."
}

# ── main ───────────────────────────────────────────────────────────────────────

check_connectivity
setup_broker
setup_driver

echo ""
echo "==> all done."
echo "    broker    : $BROKER_PI  (mosquitto :1883)"
echo "    web UI    : http://10.10.11.12:8080"
echo "    publisher : $DRIVER_PI  -> $BROKER_PI  topic=${MQTT_TOPIC}"
echo ""
echo "    check logs:"
echo "      ssh $BROKER_PI  'journalctl -fu conveyor-web'"
echo "      ssh $BROKER_PI  'journalctl -fu conveyor-subscriber'"
echo "      ssh $DRIVER_PI  'journalctl -fu conveyor-publisher'"
