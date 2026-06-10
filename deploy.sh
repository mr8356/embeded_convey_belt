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
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

_SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=5 -o LogLevel=ERROR)
ssh_q()  { ssh  "${_SSH_OPTS[@]}" "$@"; }
scp_q()  { scp -o BatchMode=yes -o ConnectTimeout=5 -o LogLevel=ERROR "$@"; }
remote() { local host=$1; shift; ssh_q "$host" "$@"; }

# ── helpers ────────────────────────────────────────────────────────────────────

check_connectivity() {
    echo "==> checking SSH connectivity"
    ssh_q "$DRIVER_PI" "echo driver-pi ok" || { echo "ERROR: cannot reach $DRIVER_PI"; exit 1; }
    ssh_q "$BROKER_PI" "echo broker-pi ok" || { echo "ERROR: cannot reach $BROKER_PI"; exit 1; }
}

is_installed() {
    local host=$1 pkg=$2
    remote "$host" "dpkg -l '$pkg' 2>/dev/null | grep -q '^ii'" 2>/dev/null
}

detect_arch() {
    remote "$1" "dpkg --print-architecture 2>/dev/null || echo arm64"
}

detect_codename() {
    remote "$1" "lsb_release -cs 2>/dev/null || \
        grep -oP '(?<=VERSION_CODENAME=).+' /etc/os-release 2>/dev/null || \
        echo bookworm"
}

# Pi apt 캐시에서 URI를 뽑고, Mac에서 내려받아 SCP → dpkg 설치
# fallback 미러: archive.debian.org, archive.raspbian.org
download_and_push_debs() {
    local host=$1
    shift
    local pkgs=("$@")

    local outdir="$PKGS_DIR"
    mkdir -p "$outdir"

    echo "  Pi apt 캐시에서 패키지 URI 수집 중 ..."
    # apt-get --print-uris는 설치 필요한 패키지의 다운로드 URL을 출력
    local raw
    raw=$(remote "$host" \
        "apt-get --print-uris -qq install --no-install-recommends ${pkgs[*]} 2>/dev/null" \
        | awk '{gsub(/'"'"'/,""); print $1}')

    if [[ -z "$raw" ]]; then
        echo "ERROR: Pi apt 인덱스 없음 — Pi에서 'sudo apt-get update' 필요"
        exit 1
    fi

    local deb_files=()
    while IFS= read -r uri; do
        [[ -z "$uri" ]] && continue
        local fname
        fname=$(basename "$uri")
        local dest="$outdir/$fname"

        if [[ -f "$dest" ]]; then
            echo "  cached  $fname"
        else
            echo "  fetch   $fname"
            # 1차: Pi가 알려준 원본 URI
            if ! curl -sSfL --retry 2 --connect-timeout 10 "$uri" -o "$dest" 2>/dev/null; then
                # 2차: archive.debian.org (Debian 표준 패키지)
                local pkg_prefix="${fname%%_*}"
                local alt1="https://archive.debian.org/debian/pool/main/${pkg_prefix:0:1}/${pkg_prefix}/${fname}"
                # 3차: archive.raspbian.org
                local alt2="http://archive.raspbian.org/raspbian/pool/main/${pkg_prefix:0:1}/${pkg_prefix}/${fname}"
                echo "  retry   $alt1"
                if ! curl -sSfL --retry 2 --connect-timeout 10 "$alt1" -o "$dest" 2>/dev/null; then
                    echo "  retry   $alt2"
                    curl -sSfL --retry 2 --connect-timeout 10 "$alt2" -o "$dest" || {
                        echo "ERROR: $fname 다운로드 실패 (모든 미러 시도)"
                        rm -f "$dest"
                        exit 1
                    }
                fi
            fi
        fi
        deb_files+=("$dest")
    done <<< "$raw"

    echo "  Pi로 전송 중 ..."
    remote "$host" "mkdir -p /tmp/conveyor_debs && rm -f /tmp/conveyor_debs/*.deb"
    scp_q "${deb_files[@]}" "${host}:/tmp/conveyor_debs/"
    remote "$host" "sudo dpkg -i /tmp/conveyor_debs/*.deb"
    echo "  오프라인 설치 완료."
}

install_if_needed() {
    local host=$1
    shift
    local pkgs=("$@")
    local primary="${pkgs[0]}"

    echo "==> $host 에서 ${pkgs[*]} 확인 중"

    if is_installed "$host" "$primary"; then
        echo "  이미 설치됨."
        return 0
    fi

    # 1차 시도: apt (로컬 캐시에 있으면 인터넷 없이도 됨)
    echo "  apt 시도 중 ..."
    remote "$host" "sudo apt-get install -y --no-install-recommends ${pkgs[*]}" 2>/dev/null || true

    if is_installed "$host" "$primary"; then
        echo "  apt 설치 성공."
        return 0
    fi

    # 2차 시도: Mac에서 .deb 직접 다운로드 → SCP → dpkg
    echo "  apt 실패 — Mac에서 오프라인 설치 진행 ..."
    download_and_push_debs "$host" "${pkgs[@]}"

    if ! is_installed "$host" "$primary"; then
        echo "ERROR: $primary 설치 실패"
        exit 1
    fi
    echo "  설치 완료."
}

# ── broker Pi (10.10.11.12) setup ──────────────────────────────────────────────

setup_broker() {
    echo ""
    echo "==> setting up MQTT broker on $BROKER_PI"

    install_if_needed "$BROKER_PI" mosquitto mosquitto-clients

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

    install_if_needed "$DRIVER_PI" mosquitto-clients

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
