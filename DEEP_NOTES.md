# 개발 전체 기록 — 설계 결정, 버그, 트러블슈팅

REPORT.md와 CODE_EXPLANATION.md에 담지 못한 내용을 모두 기록한다.

---

## 1. 네트워크 구성 상세

### 서브넷 구조

```
서브넷 A: 10.10.10.0/24
  MacBook  en10: 10.10.10.11
  드라이버 Pi  eth0: 10.10.10.12

서브넷 B: 10.10.11.0/24
  MacBook  en7:  10.10.11.11
  섭스크라이버 Pi eth0: 10.10.11.12
```

MacBook이 두 NIC으로 양쪽 서브넷에 동시에 연결되어 있어 자연스럽게 브로커 역할을 한다.  
mosquitto는 `listener 1883 0.0.0.0`으로 모든 인터페이스에 바인딩되기 때문에 두 Pi 모두에서 접근 가능하다.

### 왜 MacBook을 브로커로 선택했나

처음 설계는 섭스크라이버 Pi(10.10.11.12)를 브로커로 했다.  
그런데 Raspberry Pi OS Buster(Debian 10) 저장소에서 mosquitto의 의존성인 `libuv1` 패키지를 설치할 수 없었다.  
아카이브 미러에도 해당 버전이 없어 설치가 완전히 불가능한 상황이 발생했다.

이 문제 때문에 아키텍처를 전면 변경:
- **브로커**: MacBook (Homebrew mosquitto, 설치 가능)
- **Pi 두 대**: 외부 패키지 없이 Python stdlib만 사용하는 순수 Python MQTT 클라이언트

결과적으로 Pi에 아무 패키지도 설치할 필요가 없어 오프라인 환경에서도 완전히 동작한다.

### Pi → Mac 연결 가능 여부

Pi는 직접 MacBook IP(192.168.xxx)로 라우팅이 안 된다.  
하지만 MacBook이 해당 서브넷 NIC을 가지고 있으므로 서브넷 내부 IP로는 직접 통신된다:
- 드라이버 Pi → `10.10.10.11:1883` (Mac의 en10)
- 섭스크라이버 Pi → `10.10.11.11:1883` (Mac의 en7)

---

## 2. mosquitto 설정 상세

### /opt/homebrew/etc/mosquitto/mosquitto.conf

```
listener 1883 0.0.0.0    # 모든 인터페이스 (두 서브넷 모두)
allow_anonymous true      # 인증 없음
persistence false         # 재시작 시 메시지 보존 안 함
log_type error
log_type warning
log_type notice
log_dest file /opt/homebrew/var/log/mosquitto/mosquitto.log
```

`persistence false`는 MQTT 세션 상태를 메모리에만 유지한다는 의미다. 브로커 재시작 시 구독 정보와 큐잉된 메시지가 사라지지만, QoS 0만 사용하므로 문제없다.

### mosquitto 로그 해석

```
New client connected from 10.10.10.12:54970 as conveyor-pub (p4, c1, k0).
```

- `p4`: MQTT 프로토콜 버전 4 (v3.1.1)
- `c1`: clean session = 1 (이전 세션 상태 무시)
- `k0`: keepalive = 0 (타임아웃 비활성화) ← 우리가 수정한 부분

수정 전에는 `k60`으로 나왔고, 90초(keepalive×1.5) 후 브로커가 클라이언트를 강제 종료했다.

---

## 3. systemd 서비스 구성

### conveyor-publisher.service (드라이버 Pi)

```ini
[Unit]
Description=Conveyor MQTT publisher
After=network.target

[Service]
ExecStart=/usr/bin/python3 /home/pi/conveyor_node_driver/conveyor_event_daemon.py \
    --broker 10.10.10.11 --port 1883 --topic iot/conveyor/state
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

### conveyor-subscriber.service / conveyor-web.service (섭스크라이버 Pi)

동일 구조, `--broker 10.10.11.11`로 자신의 서브넷 쪽 Mac IP 사용.  
`conveyor-web.service`는 `--port 8080` 추가.

### 주의사항: systemd에서 `~` 경로 불가

`ExecStart`에 `~/conveyor_node_driver/...` 처럼 tilde를 쓰면 실행이 안 된다.  
systemd는 쉘이 아니므로 `~` 확장을 하지 않는다. 반드시 절대 경로 `/home/pi/...` 를 써야 한다.

---

## 4. SSH 설정

### Pi 암호 없이 SSH 접속 (공개키 등록)

```bash
ssh-copy-id pi@10.10.10.12
ssh-copy-id pi@10.10.11.12
```

`deploy.sh`는 `BatchMode=yes`로 SSH를 실행한다. 공개키 등록 없이 실행하면 비밀번호 입력 프롬프트가 나오는데 BatchMode에서는 이를 자동 실패로 처리해 스크립트가 멈춘다.

### SSH 후양자 암호화 경고 억제

최신 SSH에서 post-quantum 알고리즘 관련 경고가 stderr에 출력되어 스크립트 출력이 지저분해진다.

```bash
_SSH=(-o BatchMode=yes -o ConnectTimeout=5 -o LogLevel=ERROR)
```

`LogLevel=ERROR`로 INFO/WARNING 레벨 메시지를 모두 숨긴다.

---

## 5. 개발 중 발생한 버그 전체 목록

### 버그 1: echo 위상 비보호 → distance=158860cm

**현상**: `dmesg`에 `distance=158860` 출력. 1.58km는 물리적으로 불가능.

**원인 분석**:
```c
// 수정 전 (버그 있음)
if (value) {                     // ← phase 확인 없음!
    nd->echo_start = now;
    nd->echo_phase = 2;
}
```
노이즈 RISING 엣지가 들어오면 `echo_phase=2`로 고착된다. 이후 `trigger_ultrasonic()`은 `phase != 0 && phase != 3` 조건으로 진입을 막으므로 새 트리거가 영원히 안 된다. 수 초 뒤에 노이즈 FALLING이 오면 `(now - echo_start) = 수초 → distance = 수만 cm`가 계산된다.

**수정**:
```c
if (value && nd->echo_phase == 1) {      // phase==1 (트리거 완료) 일 때만
    nd->echo_start = now;
    nd->echo_phase = 2;
} else if (!value && nd->echo_phase == 2) {
    s64 us = ktime_to_us(...);
    if (us > 0 && us < 40000)            // 40ms 초과는 무효
        new_distance = (int)us / 58;
    nd->echo_phase = 3;
}
```

---

### 버그 2: echo-triggered fusion 루프 → 블락/오케이 200ms 진동

**현상**: 초음파 센서 근처에서 손을 움직이면 BLOCKED ↔ RUNNING_OK가 0.5초 주기로 반복.

**원인 분석**:
```c
// 수정 전: echo FALLING 때마다 fusion_work 예약
if (!value)
    schedule_work(&nd->fusion_work);   // ← 이 줄이 문제
```

5cm 거리에서 echo 왕복 시간: 5×58 = 290µs.  
echo_irq → fusion_work → trigger_ultrasonic → echo_irq → ... 루프가 ~10ms 주기로 돈다.  
`blockage_confirm_count=5`이면 5 × 10ms = **50ms**만에 BLOCKED 확정.  
`blockage_clear_count=2`이면 2 × 10ms = **20ms**만에 CLEAR.  
결과: 50ms BLOCKED + 20ms CLEAR 반복 = 14Hz 진동.

**수정**: echo IRQ에서 `schedule_work` 제거. 타이머(200ms)만이 fusion을 구동한다.  
이제 `blockage_confirm_count=5` × 200ms = 실제 1초가 된다.

---

### 버그 3: stale distance로 인한 false distance_stable

**현상**: 200ms 타이머가 echo 수신 전에 발동되면 `distance_cm`이 직전 값 그대로라 `diff=0` → `distance_stable=true` 오탐.

**타임라인**:
```
t=0ms:   hrtimer 발동 → fusion_work
             distance_cm=10 (직전값)
             prev_distance_cm=10 (직전값)
             diff = 10-10 = 0 → distance_stable=true!  ← 오탐
         → trigger_ultrasonic

t=5ms:   echo 수신 → distance_cm=12 갱신

t=200ms: hrtimer 발동 → fusion_work
             distance_cm=12 (새 값)
             prev_distance_cm=10 (직전값)
             diff = 2 → distance_stable=true (이번엔 맞음)
```

**수정**: `distance_fresh` bool 플래그 추가.
```c
// ultra_irq_handler에서
nd->distance_cm    = new_distance;
nd->distance_fresh = true;           // 새 측정값 표시

// fusion_work_fn에서
distance_fresh     = nd->distance_fresh;
nd->distance_fresh = false;          // 소비

distance_stable = distance_fresh && ...;   // fresh 아니면 무조건 false
```

---

### 버그 4: MQTT keepalive=60 → 90초 후 연결 끊김

**현상**: 이벤트가 없는 상태로 90초 경과 시 브로커 로그에 `disconnected: exceeded timeout`.  
재연결 시 `disconnected: protocol error`까지 발생.

**원인**: MQTT keepalive=60초. 브로커는 keepalive × 1.5 = 90초 이내에 패킷이 없으면 클라이언트를 강제 종료한다. 컨베이어 이벤트는 가끔씩만 오므로 유휴 구간이 90초를 초과한다.

**수정**: `bytes([4, 2, 0, 60])` → `bytes([4, 2, 0, 0])` (keepalive=0 비활성화)

세 파일 모두 수정: `conveyor_event_daemon.py`, `conveyor_subscriber.py`, `conveyor_web.py`.

---

### 버그 5: 광센서 0↔2600 깜빡임

**현상**: ADC 값이 0과 2600을 번갈아가며 출력. `distance=158860`과 함께 `light=0`으로 BLOCKAGE_ALERT 오탐.

**원인 1 (회로)**: 센서 배선 불량. 고쳐서 해결.

**원인 2 (소프트웨어)**: 50/60Hz 조명 아래서 200ms 샘플링은 `200ms = 10 × 20ms(50Hz 주기)`로 완벽히 동기화되어 항상 같은 위상을 샘플링한다. 그 위상이 꺼진 순간이면 항상 0이 찍힌다.

**수정**: EMA 필터 (alpha=1/8, 시정수 1.6초).
```c
nd->light_ema = nd->light_ema - (nd->light_ema >> 3) + light_value;
nd->light_value = nd->light_ema >> 3;
```

---

### 버그 6: Python 3.7 타입 힌트 문법 오류

**현상**: `list[queue.Queue]`, `socket.socket | None` 같은 타입 힌트가 Python 3.7에서 `TypeError`.

**원인**: `list[...]` 제네릭 구문은 Python 3.9+, `A | B` Union 구문은 Python 3.10+. Raspberry Pi OS Buster는 Python 3.7.

**수정**: 모든 Python 파일에 `from __future__ import annotations` 추가.  
이 임포트는 타입 힌트 평가를 런타임이 아닌 문자열로 지연시켜 3.7에서도 동작하게 한다.

---

### 버그 7: eth0 USB 단선 (전원 부족)

**현상**: Pi dmesg에 주기적으로 `smsc95xx 1-1.1:1.0 eth0: unregister` + `Under-voltage detected! (0x00050005)`.

**원인**: Raspberry Pi 3B의 이더넷은 USB 허브 칩(SMSC95xx) 위에 구현된다. 전원 전압이 부족하면 USB 버스 전체가 리셋되어 eth0이 순간 끊긴다.

`0x00050005` 스로틀링 플래그 의미:
- Bit 0 (0x1): 현재 Under-voltage
- Bit 2 (0x4): 현재 CPU 스로틀링
- Bit 16 (0x10000): 부팅 이후 Under-voltage 이력
- Bit 18 (0x40000): 부팅 이후 스로틀링 이력

**해결**: 5.1V 3A 이상의 공식 라즈베리 파이 전원 어댑터로 교체. 일반 스마트폰 충전기(5V/2A)는 부하 시 4.7V까지 떨어져 이 현상을 유발한다.

---

### 버그 8: deploy.sh에서 `systemctl is-active` exit code 3

**현상**: `set -euo pipefail` 환경에서 `systemctl is-active conveyor-publisher`가 "activating" 상태를 반환할 때 exit code 3으로 스크립트 전체가 종료됨.

**원인**: `systemctl is-active`는 active=0, activating=3, failed=3 등 상태마다 다른 exit code를 반환한다. `set -e`에서 0이 아닌 exit code는 즉시 종료.

**수정**:
```bash
st=$(sudo systemctl is-active conveyor-publisher 2>/dev/null || true)
echo "publisher status: $st"
```
`|| true`로 exit code를 무시하고 상태 문자열만 출력한다.

---

## 6. 하드웨어 특성 메모

### HC-SR04 초음파 센서

| 항목 | 값 |
|------|-----|
| 동작 전압 | 5V |
| 측정 범위 | 2cm ~ 400cm |
| ECHO 출력 | 5V (Pi GPIO는 3.3V 내성 → **반드시 분압기 필요**) |
| 최대 echo 시간 | ~38ms (400cm × 58µs/cm) |
| 최소 측정 간격 | 60ms |
| 거리 계산식 | `distance_cm = echo_us / 58` |

**5V → 3.3V 분압기**: 1kΩ + 2kΩ 저항 분배. ECHO → 1kΩ → GPIO PIN → 2kΩ → GND.

### MCP3208 ADC

| 항목 | 값 |
|------|-----|
| 해상도 | 12비트 (0~4095) |
| 채널 수 | 8개 |
| 인터페이스 | SPI MODE 0 또는 MODE 3 |
| 최대 SPI 클럭 | 2MHz (VDD=5V), 1MHz (VDD=2.7V) |
| 코드에서 사용 | 1MHz, MODE 0 |

SPI 명령 프레임 (채널 0 기준):
```
TX: 0x06 0x00 0x00
RX: ---- 0x0X 0xXX
결과: (rx[1] & 0x0F) << 8 | rx[2]
```

### 기울기 스위치

두 가지 타입이 있다:
- **NO (Normally Open)**: 기울어지면 GPIO HIGH → `tilt_active_level=1` (기본값)
- **NC (Normally Closed)**: 기울어지면 GPIO LOW → `tilt_active_level=0`으로 insmod 필요

잘못 설정하면 항상 LEVEL로만 나온다. `/proc/conveyor_node_stats`의 `tilt_gpio` 현재 값과 `last_tilt_gpio`를 비교해 판단할 수 있다.

---

## 7. 웹 대시보드 상세

### 상태별 색상 코딩

| 상태 | 배경색 | 글자색 | 특이사항 |
|------|--------|--------|---------|
| RUNNING_OK | 진한 초록 (#14532d) | 연초록 (#86efac) | - |
| BLOCKAGE_ALERT | 진한 주황 (#78350f) | 노랑 (#fde68a) | - |
| STRUCTURAL_FAULT | 진한 적갈색 (#7c2d12) | 살구 (#fed7aa) | - |
| CRITICAL_FAULT | 진한 빨강 (#7f1d1d) | 분홍 (#fca5a5) | 0.6초 깜빡임 |

### SSE 연결 유지 메커니즘

브라우저는 SSE 연결이 일정 시간 무응답이면 자동 재연결을 시도한다.  
이를 방지하기 위해 25초마다 `: heartbeat` 주석 라인을 전송한다.

```python
HEARTBEAT_SEC = 25

try:
    msg = q.get(timeout=HEARTBEAT_SEC)
    self.wfile.write(msg)
except queue.Empty:
    self.wfile.write(b": heartbeat\n\n")   # SSE 주석 = 브라우저에 표시 안 됨
```

### 브라우저 접속 시 즉시 현재 상태 표시

새 탭으로 접속하면 다음 이벤트가 올 때까지 화면이 비어있는 문제를 방지한다.

```python
def _serve_sse(self):
    # 연결 직후 현재 스냅샷 즉시 전송
    with _state_lock:
        if _current:
            snap = "data: " + json.dumps(_current) + "\n\n"
            self.wfile.write(snap.encode())
```

### reason 비트마스크 → 배지 렌더링

JavaScript에서 reason 값을 파싱해 의미 있는 배지로 표시한다.

```javascript
const REASON_MAP = {
    0x1: 'TILT',
    0x2: 'BLOK',
    0x4: 'LIGHT',
    0x8: 'DIST'
};
// reason=0x6 → ['BLOK', 'LIGHT'] 배지
Object.entries(REASON_MAP)
    .filter(([bit]) => reason & Number(bit))
    .map(([, label]) => `<span class="badge">${label}</span>`)
    .join('')
```

---

## 8. conveyor_monitor.c — 유저스페이스 테스트 도구

커널 모듈을 직접 테스트하기 위한 C 프로그램.

```c
// poll() + read() 패턴
struct pollfd pfd = { .fd = fd, .events = POLLIN };
poll(&pfd, 1, -1);      // 이벤트 대기 (무한정)
read(fd, buf, sizeof(buf)-1);
printf("%s", buf);
```

`poll()` 사용으로 CPU를 소모하지 않고 대기한다.  
커널의 `node_poll()`이 `wait_queue`에 등록하고, `push_event()`의 `wake_up_interruptible()`이 이를 깨운다.

빌드 및 실행:
```bash
# Pi에서
gcc -Wall -O2 -o conveyor_monitor conveyor_monitor.c
./conveyor_monitor
```

---

## 9. 모듈 파라미터 런타임 변경

모듈 로드 후 일부 파라미터는 `/sys/module`을 통해 실시간으로 변경 가능하다.  
(`module_param` 선언 시 퍼미션이 `0644`인 경우.)

```bash
# blockage 확정 카운트 실시간 변경 (재로드 불필요)
echo 10 | sudo tee /sys/module/conveyor_node/parameters/blockage_confirm_count

# 현재 파라미터 값 확인
cat /sys/module/conveyor_node/parameters/blockage_confirm_count

# light threshold 변경
echo 2000 | sudo tee /sys/module/conveyor_node/parameters/light_threshold
```

단, `tilt_gpio`, `ultra_trig_gpio`, `ultra_echo_gpio`는 `0444` (읽기 전용)으로 선언되어 있어 로드 후 변경 불가.

---

## 10. 전체 타이밍 다이어그램

```
t=0ms     hrtimer 발동
           └─ schedule_work(fusion_work)

t=~0ms    fusion_work_fn 실행
           ├─ adc_read_value() — SPI 읽기 (~수십 µs)
           ├─ EMA 갱신
           ├─ distance_stable 판정
           ├─ streak 카운터 업데이트
           ├─ set_fused_state() — 상태 변화 시 push_event()
           └─ trigger_ultrasonic()
                └─ GPIO 23 HIGH (10µs) → LOW

t=~0.3ms  (5cm 거리) echo RISING → ultra_irq_handler
           └─ echo_start = now, phase=2

t=~0.6ms  echo FALLING → ultra_irq_handler
           └─ us = 290µs → distance_cm = 5cm
           └─ distance_fresh = true

t=200ms   hrtimer 발동 (다음 사이클)
           └─ fusion_work 실행
               └─ distance_fresh=true → 이번엔 새 거리 사용
```

이벤트 발생 없이 정상 운행 중:
- `fusion_runs` 카운터: 초당 5씩 증가
- `suppressed_events` 카운터: 상태 변화가 없으므로 초당 5씩 증가
- `user_events` 카운터: 상태 변화 시에만 증가

---

## 11. 디버깅 치트시트

```bash
# === 드라이버 Pi (10.10.10.12) ===

# 커널 로그 실시간
dmesg -w | grep conveyor

# 현재 상태 스냅샷
cat /proc/conveyor_node_stats

# 이벤트 스트림 (블로킹)
cat /dev/conveyor_node0

# 모듈 로드/언로드
sudo insmod ~/conveyor_node_driver/conveyor_node.ko
sudo rmmod conveyor_node
lsmod | grep conveyor

# GPIO 현재 값
cat /sys/class/gpio/gpio27/value   # tilt
cat /sys/class/gpio/gpio24/value   # echo

# 전원 상태
dmesg | grep -i voltage
vcgencmd get_throttled             # 0x0=정상, 0x50005=under-voltage

# 서비스
journalctl -fu conveyor-publisher
sudo systemctl status conveyor-publisher

# === 섭스크라이버 Pi (10.10.11.12) ===

journalctl -fu conveyor-subscriber
journalctl -fu conveyor-web
sudo systemctl restart conveyor-web

# === MacBook 브로커 ===

tail -f /opt/homebrew/var/log/mosquitto/mosquitto.log
brew services list | grep mosquitto
brew services restart mosquitto

# 연결된 클라이언트 수 확인 (로그에서)
grep "New client connected" /opt/homebrew/var/log/mosquitto/mosquitto.log | tail -10
```

---

## 12. Python 순수 stdlib MQTT 구현 — 패킷 구조 상세

### CONNECT 패킷 (0x10)

```
Byte 0:    0x10 (CONNECT 명령 타입)
Byte 1..N: remaining length (가변 길이 인코딩)
Body:
  [0x00 0x04 'M' 'Q' 'T' 'T']  프로토콜 이름 (길이 프리픽스)
  [0x04]                         프로토콜 레벨 (4 = v3.1.1)
  [0x02]                         connect flags (clean session=1)
  [0x00 0x00]                    keepalive (0 = 비활성화)
  [길이 2바이트][client_id]       클라이언트 ID
```

### PUBLISH 패킷 (0x30)

```
Byte 0:    0x30 (PUBLISH, QoS 0, no retain)
Byte 1..N: remaining length
Body:
  [길이 2바이트][topic]   토픽 이름
  [payload bytes]         페이로드 (QoS 0이므로 packet ID 없음)
```

### SUBSCRIBE 패킷 (0x82)

```
Byte 0:    0x82 (SUBSCRIBE)
Byte 1..N: remaining length
Body:
  [0x00 0x01]             packet ID = 1
  [길이 2바이트][topic]   토픽 필터
  [0x00]                  요청 QoS = 0
```

### PINGREQ / PINGRESP

```
PINGREQ:  0xC0 0x00 (2바이트 고정)
PINGRESP: 0xD0 0x00 (2바이트 고정)
```

`conveyor_subscriber.py`와 `conveyor_web.py`는 60초 소켓 타임아웃 후 PINGREQ를 보낸다.  
브로커가 PINGRESP를 보내면 연결이 살아있음이 확인되고 타임아웃 카운터가 리셋된다.

---

## 13. Makefile 크로스컴파일 관련

```makefile
ARCH         ?= arm
CROSS_COMPILE ?= arm-linux-gnueabi-
KDIR         ?= $(HOME)/linux-rpi   # Pi 커널 소스
```

크로스컴파일 환경이 없으면 Pi에서 직접 빌드해야 한다:
```bash
# Pi에서
sudo apt install build-essential raspberrypi-kernel-headers
cd ~/conveyor_node_driver
make KDIR=/lib/modules/$(uname -r)/build
```

`Makefile`의 `KDIR`을 Pi에서는 `/lib/modules/$(uname -r)/build`로 오버라이드하면 `~/linux-rpi`가 없어도 빌드 가능하다.

---

## 14. 이벤트 큐 오버플로우 정책

큐 크기는 32개 고정. 커널 드라이버가 이벤트를 발생시키는 속도가 유저스페이스(`conveyor_event_daemon.py`)가 읽는 속도보다 빠를 때 오버플로우가 발생한다.

```c
if (queue_full(nd))
    nd->q_tail = (nd->q_tail + 1) % EVENT_Q_SIZE;  // 가장 오래된 이벤트 삭제
nd->q[nd->q_head] = ev;
nd->q_head = (nd->q_head + 1) % EVENT_Q_SIZE;
```

가장 오래된 이벤트를 버리고 최신 이벤트를 유지하는 정책이다. 안전 시스템에서는 최신 상태가 더 중요하므로 적절한 선택이다.

---

## 15. MQTT 토픽 설계

현재 토픽: `iot/conveyor/state`

섭스크라이버는 `iot/conveyor/#` 와일드카드로 구독해 향후 추가 토픽(예: `iot/conveyor/heartbeat`, `iot/conveyor/alarm`)도 수신 가능하다.

QoS 0 (At Most Once)을 사용한다. 네트워크 단절 시 메시지 손실이 발생할 수 있지만, 컨베이어 안전 이벤트는 상태 변화 시마다 발행되므로 하나가 손실돼도 다음 상태 변화 이벤트로 복구된다.
