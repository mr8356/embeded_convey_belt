# Conveyor Node Driver

컨베이어 투입구의 막힘과 기울어짐을 감지하기 위한 통합 커널 모듈입니다.

이 폴더는 기존 팀원별 센서 코드를 하나의 커널 상태 머신으로 합친 구현물입니다. 목표는 센서의 작은 변화나 노이즈를 전부 사용자 프로그램으로 올리는 것이 아니라, 커널 안에서 먼저 판단한 뒤 의미 있는 상태 변화만 `/dev/conveyor_node0`으로 전달하는 것입니다.

```text
raw sensor input
-> IRQ top-half에서 짧게 캡처
-> workqueue bottom-half에서 보정/판정
-> 통합 상태 머신
-> 상태가 바뀐 경우에만 user space wakeup
-> 선택적으로 MQTT publish
```

## 통합한 기존 코드

### 조영탁: `tilt_motion_driver`

기울기 센서 담당 코드의 구조를 통합 드라이버의 중심 구조로 사용했습니다.

- GPIO interrupt로 기울기 변화 감지
- IRQ handler에서는 GPIO 값, timestamp, edge count만 빠르게 기록
- delayed workqueue에서 debounce 처리
- event queue로 확정 이벤트 전달
- blocking `read()`와 `poll()` 지원
- `/proc` 통계 파일 제공
- user daemon에서 MQTT로 전달하는 구조

통합 후에는 기울기 센서가 단독 알림을 만드는 것이 아니라, 최종 컨베이어 상태를 결정하는 입력 중 하나가 됩니다.

### 초음파: `simple_ultra.c`

초음파 센서 코드는 물체가 통로 안에 머무는지 판단하는 보조 입력으로 반영했습니다.

- trigger GPIO로 초음파 발사
- echo GPIO의 rising/falling interrupt로 pulse width 측정
- pulse width를 거리 `distance_cm`로 변환
- 같은 거리 구간이 반복될 때 막힘 판단 보조

기존 방식처럼 user가 `read()`할 때마다 직접 trigger하고 기다리는 구조가 아니라, 커널 내부의 주기적 fusion work가 측정을 유도합니다. 사용자는 측정 과정이 아니라 확정된 상태 이벤트만 읽습니다.

### 조동현: photocell + MCP3208/MCP3008

조도 센서와 ADC 코드는 물체가 센서 경로를 가렸는지 판단하는 입력으로 반영했습니다.

- MCP3208/MCP3008 3-byte SPI command
- `spi_sync_transfer()`로 ADC 값 읽기
- `light_threshold` 기준으로 막힘 여부 판단
- workqueue에서 주기적으로 sampling
- 위험 상태에서 LED alarm GPIO 제어

기존의 `dmesg` 출력 중심 흐름은 통합 드라이버에서는 상태 머신 입력으로 바뀌었습니다.

## 최종 상태 정의

| 상태 | 조건 | 의미 | 대응 |
|---|---|---|---|
| `RUNNING_OK` | 기울기 정상 + 막힘 없음 | 정상 운전 | 정기 점검 외 특별 조치 없음 |
| `BLOCKAGE_ALERT` | 기울기 정상 + 막힘 있음 | 일반 막힘 비상 | 컨베이어 일시 정지, vibrator/air blaster, 필요 시 수동 제거 |
| `STRUCTURAL_FAULT` | 기울어짐 + 막힘 없음 | 구조 이상, 잠재 대형 사고 | 물건이 내려가도 즉시 정지, 슈트 수직 보정/용접/보강 |
| `CRITICAL_FAULT` | 기울어짐 + 막힘 있음 | 최악 비상 | 연쇄 emergency stop, 하중 지지, 철거/재설치 수준 정비 |

상태 판단은 다음처럼 단순한 2축 모델로 볼 수 있습니다.

```text
                         막힘 없음(CLEAR)          막힘 있음(BLOCKED)
기울기 정상(LEVEL)       RUNNING_OK                BLOCKAGE_ALERT
기울어짐(TILTED)         STRUCTURAL_FAULT          CRITICAL_FAULT
```

## 상태 머신 의도

이번 프로젝트의 핵심은 센서값을 단순히 출력하는 것이 아니라, 여러 센서의 의미를 합쳐서 현장의 대응 수준을 결정하는 것입니다.

- 기울기 센서는 구조물이 틀어졌는지를 봅니다.
- 초음파 센서는 내부 거리 변화로 물체가 멈춰 있는지를 봅니다.
- 조도 센서는 경로가 가려졌는지를 봅니다.
- 초음파와 조도 센서를 함께 사용해 막힘 판단의 신뢰도를 높입니다.

예를 들어 조도 센서가 순간적으로 가려졌다고 바로 사고로 보지 않습니다. 초음파 거리와 반복 횟수까지 함께 보고, 확정된 상태 전이가 생겼을 때만 이벤트를 발생시킵니다.

## 동작 구조

### Top-half

IRQ top-half는 interrupt가 들어온 즉시 실행되는 부분입니다. 여기서는 오래 걸리는 일을 하면 안 되므로 최소한의 기록만 합니다.

```text
Tilt GPIO edge IRQ
-> irq_total 증가
-> tilt_edges 증가
-> 현재 GPIO 값 저장
-> debounce용 delayed work 예약

Ultrasonic echo IRQ
-> irq_total 증가
-> ultra_edges 증가
-> rising timestamp 저장
-> falling timestamp에서 pulse width 계산
-> distance_cm snapshot 갱신
-> fusion work 예약
```

### Bottom-half / Workqueue

bottom-half는 interrupt 직후에 바로 끝내기 어려운 작업을 나중에 처리하는 부분입니다. 이 구현에서는 workqueue를 사용합니다.

```text
tilt_eval_work
-> debounce 시간이 지난 뒤 GPIO 재확인
-> LEVEL/TILTED 판정
-> fusion work 예약

fusion_work
-> ultrasonic trigger
-> SPI ADC sampling
-> light threshold 확인
-> 거리 안정성 확인
-> CLEAR/BLOCKED 판정
-> 최종 conveyor_state 계산
```

### Event queue

상태가 바뀌지 않은 센서 변화는 사용자 프로그램을 깨우지 않습니다.

```text
state unchanged
-> suppressed_events 증가
-> user space wakeup 없음

state changed
-> event queue에 한 줄 추가
-> /dev/conveyor_node0 read 대기 중인 프로그램 wakeup
-> CRITICAL_FAULT이면 LED alarm on
```

이 구조 덕분에 노이즈성 이벤트는 커널 안에서 흡수되고, MQTT나 상위 시스템에는 의미 있는 상태 변화만 올라갑니다.

## 파일 구성

| 파일 | 역할 |
|---|---|
| `conveyor_node.c` | 통합 커널 모듈. SPI ADC, GPIO IRQ, workqueue, 상태 머신, `/dev`, `/proc` 담당 |
| `conveyor_monitor.c` | `/dev/conveyor_node0`을 `poll()`로 기다리며 이벤트를 출력하는 C 테스트 프로그램 |
| `conveyor_event_daemon.py` | 이벤트 한 줄을 JSON으로 변환하고, 필요하면 MQTT로 publish하는 user daemon |
| `mknod.sh` | 장치 노드가 자동 생성되지 않을 때 수동 생성 보조 |
| `Makefile` | 커널 모듈과 테스트 프로그램 빌드, 라즈베리파이 복사 |
| `README.md` | 구현 설명, 실행 방법, 팀 전달 문서 |

## 빌드

강의자료 Lab6 흐름처럼 가상머신에서 라즈베리파이용 커널 모듈을 크로스 컴파일합니다.

```sh
cd conveyor_node_driver
make ARCH=arm CROSS_COMPILE=arm-linux-gnueabi-
chmod +x conveyor_event_daemon.py mknod.sh
```

기본 커널 소스 위치는 `~/linux-rpi`입니다. 위치가 다르면 `KDIR`을 직접 지정합니다.

```sh
make ARCH=arm CROSS_COMPILE=arm-linux-gnueabi- KDIR=/path/to/linux-rpi
```

사용자 공간 테스트 프로그램만 로컬에서 문법 확인하고 싶다면 다음처럼 빌드할 수 있습니다.

```sh
gcc -Wall -Wextra -O2 -o conveyor_monitor conveyor_monitor.c
```

## 라즈베리파이로 복사

```sh
make scp RPI=pi@10.10.10.12 RPI_DIR=~/conveyor_node_driver
```

`RPI`와 `RPI_DIR`은 실제 라즈베리파이 주소와 복사할 경로에 맞게 바꿉니다.

## SPI probe 주의

이 모듈은 SPI ADC 장치를 기준으로 probe되는 `spi_driver`입니다. 정석은 device tree overlay에서 `compatible = "simple,conveyor-node"` 장치를 만들고 이 드라이버가 그 SPI 장치에 붙도록 하는 것입니다.

수업 데모에서 빠르게 확인해야 하는 경우에는 기존 `spidev`가 잡고 있는 SPI 장치를 unbind한 뒤 이 드라이버를 bind해야 할 수 있습니다. 실제 명령은 라즈베리파이의 `/sys/bus/spi/devices/` 상태에 따라 달라집니다.

확인할 예:

```sh
ls /sys/bus/spi/devices/
ls /sys/bus/spi/drivers/
dmesg | tail -50
```

## 모듈 파라미터

| 파라미터 | 기본값 | 의미 |
|---|---:|---|
| `tilt_gpio` | `27` | 기울기 센서 GPIO |
| `tilt_active_level` | `1` | 이 값일 때 TILTED로 판단 |
| `tilt_debounce_ms` | `50` | 기울기 debounce 시간 |
| `ultra_trig_gpio` | `23` | 초음파 trigger GPIO |
| `ultra_echo_gpio` | `24` | 초음파 echo GPIO |
| `ultra_max_dist_cm` | `50` | 막힘 판단에 사용할 최대 거리 |
| `ultra_tolerance_cm` | `2` | 거리 안정성 허용 오차 |
| `blockage_confirm_count` | `3` | BLOCKED 확정에 필요한 연속 횟수 |
| `blockage_clear_count` | `2` | CLEAR 확정에 필요한 연속 횟수 |
| `adc_channel` | `0` | MCP3208/MCP3008 ADC 채널 |
| `adc_bits` | `12` | ADC 해상도. MCP3208은 12, MCP3008은 10 |
| `light_threshold` | `1800` | 조도 기반 막힘 판단 기준 |
| `light_blocked_when_below` | `1` | 낮은 ADC 값을 막힘으로 볼지 여부 |
| `led_gpio` | `5` | 위험 알림 LED GPIO |
| `sample_period_ms` | `200` | fusion work 주기 |
| `spi_speed_hz` | `1000000` | SPI 속도 |

로드 예:

```sh
sudo insmod conveyor_node.ko \
  tilt_gpio=27 tilt_active_level=1 tilt_debounce_ms=50 \
  ultra_trig_gpio=23 ultra_echo_gpio=24 \
  adc_channel=0 adc_bits=12 light_threshold=1800 \
  led_gpio=5 sample_period_ms=200
```

언로드:

```sh
sudo rmmod conveyor_node
```

## 실행과 확인

장치 파일 확인:

```sh
ls -l /dev/conveyor_node0
```

상태 통계 확인:

```sh
cat /proc/conveyor_node_stats
```

C 모니터 실행:

```sh
./conveyor_monitor
```

Python daemon 실행:

```sh
python3 conveyor_event_daemon.py
```

출력 이벤트 예:

```text
seq=3 state=CRITICAL_FAULT tilt=TILTED blockage=BLOCKED distance_cm=12 light=530 reason=0x6
```

각 필드의 의미는 다음과 같습니다.

| 필드 | 의미 |
|---|---|
| `seq` | 커널이 발행한 이벤트 순서 |
| `state` | 최종 컨베이어 상태 |
| `tilt` | 기울기 판정 |
| `blockage` | 막힘 판정 |
| `distance_cm` | 초음파 거리 snapshot |
| `light` | ADC 조도 값 snapshot |
| `reason` | 어떤 입력 변화로 상태 전이가 발생했는지 나타내는 bit flag |

## MQTT 연동

커널 모듈이 직접 MQTT를 처리하지 않습니다. 커널은 `/dev/conveyor_node0`으로 이벤트를 내보내고, user daemon이 JSON으로 바꿔 publish합니다.

출력만 확인:

```sh
python3 conveyor_event_daemon.py
```

MQTT publish:

```sh
python3 conveyor_event_daemon.py --broker 127.0.0.1 --topic iot/conveyor/state
```

node id 지정:

```sh
python3 conveyor_event_daemon.py --broker 127.0.0.1 --topic iot/conveyor/state --node-id conveyor-1
```

## 강의자료 연결

| 강의자료 | 코드에 반영된 내용 |
|---|---|
| 15 Hardware Event Handling | IRQ, ISR, top-half, interrupt에서 짧게 처리해야 하는 이유 |
| 16 Lab8 Hardware Event Handling | GPIO IRQ, rising/falling edge trigger |
| 17 Synchronization | `atomic64_t`, `spinlock_t`, shared state 보호 |
| 19 Serial Busses | SPI bus, MCP 계열 ADC, publish-subscribe 개념 |
| 20 Lab10 Serial Busses Sensor Actuator | MCP3208/MCP3008 ADC, ultrasonic, sensor-actuator 흐름 |
| 21 Deferrable Functions | bottom-half, workqueue로 일을 미루는 구조 |
| 22 Lab11 Deferrable Functions | tasklet/workqueue 실습 흐름과 연결 |
| 23 Lab12 procfs | `/proc/conveyor_node_stats` 통계 파일 |

발표에서는 다음 한 문장으로 구조를 설명하면 됩니다.

```text
Interrupt에서는 센서 변화의 흔적만 빠르게 저장하고, workqueue에서 센서값을 융합해 최종 상태를 판단한 뒤, 상태가 바뀐 경우에만 user space와 MQTT로 올립니다.
```

## 팀원에게 전달할 구현 요약

통합본은 세 센서의 raw 값을 그대로 보내지 않습니다. 커널이 먼저 다음 네 상태 중 하나로 정리합니다.

```text
기울기 정상 + 막힘 없음 = RUNNING_OK
기울기 정상 + 막힘 있음 = BLOCKAGE_ALERT
기울어짐 + 막힘 없음 = STRUCTURAL_FAULT
기울어짐 + 막힘 있음 = CRITICAL_FAULT
```

역할 분담 관점에서는 다음처럼 설명할 수 있습니다.

- 조영탁 기울기 코드는 interrupt, debounce, event queue, blocking read 구조의 뼈대가 되었습니다.
- 초음파 코드는 거리 측정과 막힘 보조 판단에 들어갔습니다.
- 조도/ADC 코드는 SPI sampling과 threshold 기반 막힘 판단에 들어갔습니다.
- MQTT는 커널이 아니라 Python daemon이 담당합니다.

## 실제 장비에서 남은 확인 사항

- SPI 장치가 `conveyor_node` 드라이버에 정상 probe되는지 확인
- MCP3208/MCP3008 종류에 맞게 `adc_bits`가 맞는지 확인
- ADC 채널 번호와 `light_threshold` 보정
- 기울기 센서의 `tilt_active_level` 확인
- 초음파 trigger/echo GPIO 번호와 거리값 안정성 확인
- `BLOCKAGE_ALERT`, `STRUCTURAL_FAULT`, `CRITICAL_FAULT` 상태 전이가 실제 센서 조합으로 발생하는지 테스트
- LED alarm GPIO가 `CRITICAL_FAULT`에서 켜지고 해제 상태에서 꺼지는지 확인

## 현재 한계

이 폴더의 코드는 통합 설계와 구현을 정리한 제출/발표용 초안입니다. 실제 라즈베리파이에서 커널 모듈을 빌드하고 load하려면 해당 보드의 커널 소스, cross compiler, SPI device binding 설정이 필요합니다.
