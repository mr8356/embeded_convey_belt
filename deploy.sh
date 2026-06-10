#!/usr/bin/env bash
# deploy.sh — conveyor MQTT infrastructure setup
#
# Architecture:
#   Mac (192.168.106.203)  : mosquitto broker  ← brew install, runs locally
#   DRIVER_PI (10.10.10.12): publisher          ← conveyor_event_daemon.py (pure Python)
#   SUBSCRIBER_PI (10.10.11.12): subscriber     ← conveyor_subscriber.py + conveyor_web.py (pure Python)
#
# Pi에 별도 패키지 설치 불필요 — Python 3 stdlib만 사용.

set -euo pipefail

BROKER_IP="${BROKER_IP:-192.168.106.203}"
DRIVER_PI="${DRIVER_PI:-pi@10.10.10.12}"
SUBSCRIBER_PI="${SUBSCRIBER_PI:-pi@10.10.11.12}"
DRIVER_DIR="/home/pi/conveyor_node_driver"
SUBSCRIBER_DIR="/home/pi/conveyor_broker"
MQTT_TOPIC="iot/conveyor/state"
MQTT_PORT=1883

_SSH=(-o BatchMode=yes -o ConnectTimeout=5 -o LogLevel=ERROR)
ssh_q()  { ssh  "${_SSH[@]}" "$@"; }
scp_q()  { scp -o BatchMode=yes -o ConnectTimeout=5 -o LogLevel=ERROR "$@"; }
remote() { local h=$1; shift; ssh_q "$h" "$@"; }

# ── 1. Mac 브로커 ───────────────────────────────────────────────────────────────

setup_mac_broker() {
    echo ""
    echo "==> Mac 브로커 설정 (mosquitto)"

    if ! command -v mosquitto &>/dev/null; then
        echo "  brew install mosquitto ..."
        brew install mosquitto
    else
        echo "  mosquitto 이미 설치됨: $(mosquitto -v 2>&1 | head -1)"
    fi

    # Homebrew prefix (Apple Silicon: /opt/homebrew, Intel: /usr/local)
    local brew_prefix
    brew_prefix=$(brew --prefix)
    local conf_dir="${brew_prefix}/etc/mosquitto"
    local conf_file="${conf_dir}/mosquitto.conf"
    local log_dir="${brew_prefix}/var/log/mosquitto"
    mkdir -p "$log_dir"

    echo "  mosquitto.conf 작성 → $conf_file"
    cat > "$conf_file" << EOF
# Conveyor broker — listens on all interfaces
listener ${MQTT_PORT} 0.0.0.0
allow_anonymous true
persistence false
log_type error
log_type warning
log_type notice
log_dest file ${log_dir}/mosquitto.log
EOF

    echo "  mosquitto 서비스 재시작 ..."
    brew services restart mosquitto
    sleep 2

    if brew services list | grep mosquitto | grep -q started; then
        echo "  mosquitto running OK  (0.0.0.0:${MQTT_PORT})"
    else
        echo "ERROR: mosquitto 시작 실패 — 'brew services list' 확인 필요"
        exit 1
    fi

    echo "  방화벽: macOS 방화벽이 켜져 있으면 포트 ${MQTT_PORT} 허용 필요"
    echo "    시스템 설정 → 네트워크 → 방화벽 → 수신 연결 허용 → mosquitto 추가"
}

# ── 2. 드라이버 Pi (publisher) ─────────────────────────────────────────────────

setup_driver_pi() {
    echo ""
    echo "==> 드라이버 Pi 설정 ($DRIVER_PI) — publisher"

    ssh_q "$DRIVER_PI" "echo driver-pi ok" || {
        echo "ERROR: $DRIVER_PI 에 SSH 불가"; exit 1
    }

    remote "$DRIVER_PI" "mkdir -p $DRIVER_DIR"
    scp_q conveyor_event_daemon.py "${DRIVER_PI}:${DRIVER_DIR}/"

    remote "$DRIVER_PI" "sudo tee /etc/systemd/system/conveyor-publisher.service > /dev/null" << EOF
[Unit]
Description=Conveyor MQTT publisher
After=network.target

[Service]
ExecStart=/usr/bin/python3 ${DRIVER_DIR}/conveyor_event_daemon.py \\
    --broker ${BROKER_IP} --port ${MQTT_PORT} --topic ${MQTT_TOPIC}
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
        sleep 2
        st=\$(sudo systemctl is-active conveyor-publisher 2>/dev/null || true)
        echo \"publisher status: \$st\"
    "
}

# ── 3. 섭스크라이버 Pi (subscriber + web UI) ───────────────────────────────────

setup_subscriber_pi() {
    echo ""
    echo "==> 섭스크라이버 Pi 설정 ($SUBSCRIBER_PI) — subscriber + web UI"

    ssh_q "$SUBSCRIBER_PI" "echo subscriber-pi ok" || {
        echo "ERROR: $SUBSCRIBER_PI 에 SSH 불가"; exit 1
    }

    remote "$SUBSCRIBER_PI" "mkdir -p $SUBSCRIBER_DIR"
    scp_q broker/conveyor_subscriber.py "${SUBSCRIBER_PI}:${SUBSCRIBER_DIR}/"
    scp_q broker/conveyor_web.py        "${SUBSCRIBER_PI}:${SUBSCRIBER_DIR}/"

    # systemd: subscriber (터미널 로그용)
    remote "$SUBSCRIBER_PI" "sudo tee /etc/systemd/system/conveyor-subscriber.service > /dev/null" << EOF
[Unit]
Description=Conveyor MQTT subscriber
After=network.target

[Service]
ExecStart=/usr/bin/python3 ${SUBSCRIBER_DIR}/conveyor_subscriber.py \\
    --broker ${BROKER_IP} --port ${MQTT_PORT} --topic ${MQTT_TOPIC}
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

    # systemd: web UI
    remote "$SUBSCRIBER_PI" "sudo tee /etc/systemd/system/conveyor-web.service > /dev/null" << EOF
[Unit]
Description=Conveyor web dashboard
After=network.target

[Service]
ExecStart=/usr/bin/python3 ${SUBSCRIBER_DIR}/conveyor_web.py \\
    --broker ${BROKER_IP} --port 8080
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

    remote "$SUBSCRIBER_PI" "
        sudo systemctl daemon-reload
        sudo systemctl enable conveyor-subscriber conveyor-web
        sudo systemctl restart conveyor-subscriber conveyor-web
        sleep 2
        st_sub=\$(sudo systemctl is-active conveyor-subscriber 2>/dev/null || true)
        st_web=\$(sudo systemctl is-active conveyor-web        2>/dev/null || true)
        echo \"subscriber status: \$st_sub\"
        echo \"web UI status:     \$st_web\"
    "
}

# ── main ───────────────────────────────────────────────────────────────────────

setup_mac_broker
setup_driver_pi
setup_subscriber_pi

echo ""
echo "=========================================="
echo " 배포 완료"
echo "=========================================="
echo "  브로커   : Mac ${BROKER_IP}:${MQTT_PORT}"
echo "  퍼블리셔 : ${DRIVER_PI}  →  브로커"
echo "  섭스크라이버: ${SUBSCRIBER_PI}  ←  브로커"
echo "  웹 UI    : http://10.10.11.12:8080"
echo ""
echo "  로그 확인:"
echo "    ssh ${DRIVER_PI}     'journalctl -fu conveyor-publisher'"
echo "    ssh ${SUBSCRIBER_PI} 'journalctl -fu conveyor-subscriber'"
echo "    ssh ${SUBSCRIBER_PI} 'journalctl -fu conveyor-web'"
echo "    tail -f $(brew --prefix)/var/log/mosquitto/mosquitto.log"
