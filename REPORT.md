# 컨베이어 벨트 안전 노드 드라이버 보고서

## 1. 시스템 개요

Linux 커널 모듈 기반 컨베이어 벨트 안전 감시 시스템.  
3개의 센서(기울기, 초음파, 광센서)를 커널 공간에서 융합해 4가지 안전 상태를 판정하고, MQTT를 통해 원격 모니터링한다.

### 전체 아키텍처

```
┌─────────────────────────────────────────────────────────┐
│            드라이버 Pi (10.10.10.12)                     │
│                                                         │
│  [tilt GPIO 27] ──IRQ──┐                                │
│  [echo GPIO 24] ──IRQ──┤──► fusion_work ──► /dev/conveyor_node0 │
│  [MCP3208 SPI]  ──ADC──┘         ▲                      │
│  [hrtimer 200ms]────────────────┘                       │
│                                                         │
│  conveyor_event_daemon.py                               │
│  /dev/conveyor_node0 → MQTT publish                     │
└───────────────────┬─────────────────────────────────────┘
                    │ MQTT (TCP 1883)
                    ▼
┌─────────────────────────────────────────────────────────┐
│          MacBook 브로커 (10.10.10.11 / 10.10.11.11)      │
│          mosquitto (0.0.0.0:1883)                        │
└───────────────────┬─────────────────────────────────────┘
                    │ MQTT (TCP 1883)
                    ▼
┌─────────────────────────────────────────────────────────┐
│          섭스크라이버 Pi (10.10.11.12)                    │
│  conveyor_subscriber.py  → 터미널 출력                   │
│  conveyor_web.py         → HTTP 웹 대시보드 :8080         │
└─────────────────────────────────────────────────────────┘
```

---

## 2. 하드웨어 핀 배선

| 센서 | 신호 | BCM GPIO / 인터페이스 |
|------|------|----------------------|
| 기울기 스위치 | OUT | GPIO 27 |
| 초음파 HC-SR04 | TRIG | GPIO 23 |
| 초음파 HC-SR04 | ECHO | GPIO 24 (3.3V 분압 필요) |
| MCP3208 ADC | SPI | SPI0 (CE0) |
| 경보 LED | - | GPIO 5 |

> **주의**: HC-SR04 ECHO 핀은 5V 출력이므로 반드시 전압 분압기(3.3V로 강하)를 통해 Pi GPIO에 연결해야 한다.

---

## 3. 개발 환경 구성

### 3-1. 크로스컴파일 환경 (맥/리눅스 호스트)

```bash
# ARM 크로스컴파일러 설치 (Ubuntu/Debian)
sudo apt install gcc-arm-linux-gnueabi

# Raspberry Pi 커널 소스 준비
git clone --depth=1 https://github.com/raspberrypi/linux ~/linux-rpi
cd ~/linux-rpi
ARCH=arm CROSS_COMPILE=arm-linux-gnueabi- make bcmrpi_defconfig
ARCH=arm CROSS_COMPILE=arm-linux-gnueabi- make -j$(nproc) modules_prepare
```

### 3-2. Pi 에서 직접 빌드 (권장)

```bash
# 빌드 의존성 설치
sudo apt install -y build-essential raspberrypi-kernel-headers

# 소스 디렉터리에서 빌드
cd ~/conveyor_node_driver
make
```

---

## 4. SPI 디바이스 바인딩

라즈베리 파이는 기본적으로 SPI0.0을 `spidev` 드라이버가 점유한다.  
커널 모듈이 SPI 디바이스를 직접 제어하려면 `spidev`를 언바인딩하고 `conveyor_node`를 바인딩해야 한다.

```bash
# 1. spidev가 spi0.0을 점유 중이므로 해제
sudo sh -c 'echo spi0.0 > /sys/bus/spi/drivers/spidev/unbind'

# 2. spi0.0 디바이스가 conveyor_node 드라이버를 사용하도록 지정
sudo sh -c 'echo conveyor_node > /sys/bus/spi/devices/spi0.0/driver_override'

# 3. conveyor_node 드라이버에 spi0.0 바인딩
sudo sh -c 'echo spi0.0 > /sys/bus/spi/drivers/conveyor_node/bind'
```

**각 명령의 의미**

| 명령 | 설명 |
|------|------|
| `spidev/unbind` | 커널이 자동으로 할당한 범용 spidev 드라이버 해제 |
| `driver_override` | 해당 SPI 디바이스가 특정 드라이버만 쓰도록 고정 |
| `conveyor_node/bind` | 모듈의 `probe()` 함수를 호출해 드라이버 초기화 |

바인딩이 성공하면 `/dev/conveyor_node0`이 생성된다.

```bash
ls /dev/conveyor_node0   # 확인
dmesg | grep conveyor_node   # probe 로그 확인
```

---

## 5. 모듈 로드 및 운용 명령

### 드라이버 Pi (10.10.10.12)

```bash
# 모듈 로드 (기본 파라미터)
sudo insmod ~/conveyor_node_driver/conveyor_node.ko

# 파라미터 커스텀 예시
sudo insmod conveyor_node.ko \
    tilt_gpio=27 \
    ultra_trig_gpio=23 \
    ultra_echo_gpio=24 \
    blockage_confirm_count=5 \
    sample_period_ms=200

# SPI 바인딩 (모듈 로드 후)
sudo sh -c 'echo spi0.0 > /sys/bus/spi/drivers/spidev/unbind'
sudo sh -c 'echo conveyor_node > /sys/bus/spi/devices/spi0.0/driver_override'
sudo sh -c 'echo spi0.0 > /sys/bus/spi/drivers/conveyor_node/bind'

# 상태 모니터링
cat /proc/conveyor_node_stats

# 이벤트 스트림 확인 (텍스트)
cat /dev/conveyor_node0

# 모듈 언로드
sudo rmmod conveyor_node

# 서비스 관리
sudo systemctl status conveyor-publisher
sudo systemctl restart conveyor-publisher
journalctl -fu conveyor-publisher
```

### 섭스크라이버 Pi (10.10.11.12)

```bash
# 서비스 상태 확인
sudo systemctl status conveyor-subscriber
sudo systemctl status conveyor-web

# 실시간 로그
journalctl -fu conveyor-subscriber
journalctl -fu conveyor-web

# 웹 대시보드
# 브라우저에서: http://10.10.11.12:8080
```

### MacBook 브로커

```bash
# mosquitto 상태 확인
brew services list | grep mosquitto

# 실시간 로그
tail -f /opt/homebrew/var/log/mosquitto/mosquitto.log

# 재시작
brew services restart mosquitto
```

---

## 6. 전체 배포 (deploy.sh)

```bash
# MacBook에서 한 번에 배포
bash deploy.sh
```

`deploy.sh`는 다음을 순서대로 실행한다:
1. MacBook에 mosquitto 설치 및 재시작
2. 드라이버 Pi에 `conveyor_event_daemon.py` 전송 + systemd 서비스 등록
3. 섭스크라이버 Pi에 `conveyor_subscriber.py`, `conveyor_web.py` 전송 + systemd 서비스 등록

---

## 7. 커널 드라이버 구조 (conveyor_node.c)

### 7-1. 주요 데이터 구조

```c
struct conveyor_node_dev {
    struct spi_device *spi;         // MCP3208 ADC SPI 핸들

    // GPIO / IRQ
    int tilt_gpio, tilt_irq;
    int ultra_trig_gpio, ultra_echo_gpio, ultra_irq;
    int led_gpio;

    // 센서 상태 (state_lock 보호)
    enum tilt_state     tilt;       // LEVEL / TILTED
    enum blockage_state blockage;   // CLEAR / BLOCKED
    enum conveyor_state state;      // 4가지 안전 상태
    int distance_cm;
    bool distance_fresh;            // 새 echo 수신 여부
    int light_value;
    int light_ema;                  // EMA 필터 누적값 (×8)

    // 블락 판정 streak 카운터
    unsigned int block_streak;
    unsigned int clear_streak;

    // 비동기 처리
    struct delayed_work tilt_eval_work;  // 디바운스 후 tilt 평가
    struct work_struct  fusion_work;     // 센서 융합 로직
    struct hrtimer      sample_timer;    // 200ms 주기 타이머

    // 이벤트 큐 (크기 32, 원형 버퍼)
    struct conveyor_event q[32];
    unsigned int q_head, q_tail;

    // miscdevice (/dev/conveyor_node0)
    struct miscdevice miscdev;
    struct proc_dir_entry *proc_entry;  // /proc/conveyor_node_stats
};
```

### 7-2. 센서별 처리 흐름

#### 기울기 센서 (tilt)
```
GPIO BOTH_EDGE IRQ
    └─► tilt_irq_handler()
            └─► mod_delayed_work(50ms debounce)
                    └─► tilt_eval_work_fn()
                            ├─ gpio_get_value() 읽기
                            ├─ TILTED / LEVEL 판정
                            ├─ state 업데이트
                            └─ 변화 시 push_event(REASON_TILT_CHANGED)
```

#### 초음파 센서 (HC-SR04)
```
hrtimer 200ms
    └─► sample_timer_fn()
            └─► schedule_work(fusion_work)

fusion_work_fn()
    └─► trigger_ultrasonic()
            ├─ GPIO 23 HIGH (10µs)
            └─ GPIO 23 LOW

echo GPIO BOTH_EDGE IRQ
    └─► ultra_irq_handler()
            ├─ RISING & phase==1: echo_start = now, phase=2
            └─ FALLING & phase==2:
                    ├─ us = now - echo_start
                    ├─ if 0 < us < 40000: distance_cm = us / 58
                    └─ distance_fresh = true
```

#### 광센서 ADC (MCP3208)
```
fusion_work_fn()
    └─► adc_read_value(spi, ch=0)
            └─► SPI 3바이트 전송/수신 (1MHz)
                    └─► EMA 필터 적용
                            new_ema = old_ema - (old_ema >> 3) + sample
                            light_value = new_ema >> 3   (alpha = 1/8)
```

### 7-3. 센서 융합 로직 (fusion_work_fn)

```c
// 초음파: 새 echo 수신 + 탐지 범위 내 + 직전과 차이 2cm 이하
distance_stable = distance_fresh
               && distance > 0
               && distance < ultra_max_dist_cm   // 50cm
               && diff <= ultra_tolerance_cm;    // 2cm

// 광센서: EMA 필터 적용값 기준
light_blocked = (light_value < light_threshold); // 1800

// 둘 중 하나라도 차단이면 raw_blocked
raw_blocked = distance_stable || light_blocked;

// streak 카운터로 확정 (오탐 방지)
if (raw_blocked)  block_streak++;  else clear_streak++;
if (block_streak >= 5)  → BLOCK_BLOCKED   // 1초
if (clear_streak >= 2)  → BLOCK_CLEAR     // 400ms
```

---

## 8. 상태 머신 (FSM)

### 상태 정의

| 상태 | 의미 |
|------|------|
| `RUNNING_OK` | 정상 운행 (tilt=LEVEL, blockage=CLEAR) |
| `BLOCKAGE_ALERT` | 이물질 감지 (tilt=LEVEL, blockage=BLOCKED) |
| `STRUCTURAL_FAULT` | 구조적 이상 (tilt=TILTED, blockage=CLEAR) |
| `CRITICAL_FAULT` | 복합 위험 (tilt=TILTED, blockage=BLOCKED) |

### FSM 다이어그램

```
                    ┌─────────────────────────────────────┐
                    │                                     │
          tilt=LEVEL│                                     │tilt=LEVEL
          blk=CLEAR │                                     │blk=BLOCKED
                    ▼                                     ▼
          ┌─────────────────┐   blockage=BLOCKED  ┌─────────────────────┐
          │   RUNNING_OK    │ ─────────────────►  │  BLOCKAGE_ALERT     │
          │  (정상 운행)     │ ◄─────────────────  │  (이물질 감지)       │
          └─────────────────┘   blockage=CLEAR    └─────────────────────┘
                  │                                          │
      tilt=TILTED │                                          │ tilt=TILTED
                  │                                          │
                  ▼                                          ▼
          ┌─────────────────┐   blockage=BLOCKED  ┌─────────────────────┐
          │ STRUCTURAL_FAULT│ ─────────────────►  │   CRITICAL_FAULT    │
          │  (구조적 이상)   │ ◄─────────────────  │   (복합 위험)        │
          └─────────────────┘   blockage=CLEAR    └─────────────────────┘
                    │                                     │
          tilt=LEVEL│                                     │tilt=LEVEL
                    └─────────────────────────────────────┘
```

**전이 조건 요약**

- `blockage=BLOCKED` : 초음파 거리 1초 안정 OR 광센서 차단
- `blockage=CLEAR`   : 400ms 연속으로 차단 없음
- `tilt=TILTED`      : GPIO 레벨 변화 + 50ms 디바운스
- `tilt=LEVEL`       : GPIO 레벨 변화 + 50ms 디바운스
- `CRITICAL_FAULT`   : LED(GPIO 5) 점등

---

## 9. 이벤트 포맷

`/dev/conveyor_node0`에서 읽히는 텍스트 라인:

```
ts=6828591970518 state=BLOCKAGE_ALERT tilt=LEVEL blockage=BLOCKED distance_cm=7 light=2599 reason=0xa
```

| 필드 | 설명 |
|------|------|
| `ts` | ktime_get_ns() 타임스탬프 (ns) |
| `state` | 현재 컨베이어 상태 |
| `tilt` | LEVEL / TILTED |
| `blockage` | CLEAR / BLOCKED |
| `distance_cm` | 초음파 측정 거리 (cm) |
| `light` | ADC EMA 필터 값 (0–4095) |
| `reason` | 전이 원인 비트마스크 |

**reason 비트마스크**

| 비트 | 값 | 의미 |
|------|-----|------|
| 0 | 0x1 | TILT_CHANGED |
| 1 | 0x2 | BLOCKAGE_CHANGED |
| 2 | 0x4 | LIGHT_BLOCKED |
| 3 | 0x8 | DISTANCE_STABLE |

---

## 10. /proc 통계

```bash
cat /proc/conveyor_node_stats
```

```
state: RUNNING_OK
tilt: LEVEL
blockage: CLEAR
distance_cm: 45
light_value: 2601
tilt_gpio: 27
ultra_trig_gpio: 23
ultra_echo_gpio: 24
led_gpio: 5
last_tilt_gpio: 0
last_echo_gpio: 0
irq_total: 1284
tilt_edges: 12
ultra_edges: 1272
fusion_runs: 3847
suppressed_events: 3840
user_events: 7
```

---

## 11. 모듈 파라미터

| 파라미터 | 기본값 | 설명 |
|----------|--------|------|
| `tilt_gpio` | 27 | 기울기 센서 BCM GPIO |
| `tilt_active_level` | 1 | 기울어진 상태의 GPIO 레벨 (NC 스위치면 0) |
| `tilt_debounce_ms` | 50 | 기울기 디바운스 지연 (ms) |
| `ultra_trig_gpio` | 23 | 초음파 TRIG BCM GPIO |
| `ultra_echo_gpio` | 24 | 초음파 ECHO BCM GPIO |
| `ultra_max_dist_cm` | 50 | 차단 감지 최대 거리 (cm) |
| `ultra_tolerance_cm` | 2 | 안정 판정 허용 오차 (cm) |
| `blockage_confirm_count` | 5 | BLOCKED 확정에 필요한 연속 샘플 수 (×200ms) |
| `blockage_clear_count` | 2 | CLEAR 확정에 필요한 연속 샘플 수 (×200ms) |
| `adc_channel` | 0 | MCP3208 채널 번호 |
| `adc_bits` | 12 | ADC 해상도 (MCP3208=12, MCP3008=10) |
| `light_threshold` | 1800 | 광센서 차단 임계값 (0–4095) |
| `light_blocked_when_below` | 1 | 1: 임계값 미만이면 차단 |
| `led_gpio` | 5 | 경보 LED BCM GPIO |
| `sample_period_ms` | 200 | 융합 샘플링 주기 (ms) |
| `spi_speed_hz` | 1000000 | SPI 클럭 속도 |

---

## 12. MQTT 토픽 및 페이로드

- **토픽**: `iot/conveyor/state`
- **브로커**: MacBook `10.10.10.11:1883` (드라이버 Pi 서브넷) / `10.10.11.11:1883` (섭스크라이버 Pi 서브넷)
- **QoS**: 0

페이로드 예시:
```json
{
  "ts": 6828591970518,
  "state": "BLOCKAGE_ALERT",
  "tilt": "LEVEL",
  "blockage": "BLOCKED",
  "distance_cm": 7,
  "light": 2599,
  "reason": 10,
  "node_id": "conveyor-1",
  "source": "conveyor_node",
  "created_at": 1749862414.2
}
```

---

## 13. 알려진 이슈 및 해결 이력

| 이슈 | 원인 | 해결 |
|------|------|------|
| `distance=158860cm` 출력 | echo rising edge를 phase 무관하게 수신, 타임아웃 없음 | phase==1일 때만 rising 수신, 40ms 초과 측정 폐기 |
| 블락/오케이 빠른 진동 | echo-triggered fusion_work가 근거리에서 초당 100회 루프 | echo IRQ에서 schedule_work 제거, 타이머만 사용 |
| 광센서 0↔2600 깜빡임 | 50/60Hz 조명 간섭 또는 회로 불량 | EMA 필터 적용 (alpha=1/8, 시정수 1.6초) |
| MQTT 연결 90초 후 끊김 | keepalive=60s인데 이벤트 없으면 브로커가 타임아웃 | keepalive=0 (비활성화) 으로 변경 |
| 전원 부족으로 eth0 리셋 | Under-voltage (0x00050005) | 5.1V 3A 공식 어댑터로 교체 필요 |
