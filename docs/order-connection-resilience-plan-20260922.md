# 주문 오류·연결 단절·실시간 판정 중단 예방 수정 계획

작성일: 2026-09-22  
검토 기준: `a199b6c`의 실행 코드와 현재 작업 폴더  
최초 검토 범위: 코드 검토, 주문 없는 로컬 검증, 수정 계획 수립. 아래 최초 검토 결과는 구현 전 기록이다.

2026-09-27 구현·검증 결과는 [실행 보고서](order-connection-resilience-implementation-20260927.md)에 기록한다. 코딩 컨벤션 준수와 기존 UI 화면 유지 조건은 구현에도 동일하게 적용한다.

## 1. 반드시 지킬 조건

1. **모든 신규·수정 코드는 `CODING_CONVENTIONS.md`를 준수한다.** 의미가 명확한 이름, 클래스 `PascalCase`, 함수·변수 `snake_case`, 기존 파일과 일치하는 4칸 들여쓰기, 연산자 공백, 작업 단위 빈 줄을 유지한다. 함수 설명에는 이름·기능·인자·반환값·작성 날짜를, 클래스 설명에는 이름·기능·작성 날짜를 기록한다. TypeScript·Rust는 기존 JSDoc·문서 주석 형식과 외부 API의 필수 이름을 보존한다.
2. **기존 UI 화면은 변경하지 않는다.** 레이아웃, 컴포넌트의 시각 구조, CSS, 디자인 토큰, 색상, 폰트, 아이콘, 화면 문구, 팝업 및 사용자 조작 흐름을 유지한다. 프론트 수정은 통신 어댑터와 내부 복구 상태 처리에 한정한다. 오류 중에는 기존 상태 표시를 사용하고, 정상 복구 후 같은 데이터는 같은 화면으로 표시해야 한다.
3. 주문 상태가 불명확하면 같은 주문 ID를 조회한다. 연결 복구를 이유로 새 주문을 제출하거나 기존 주문을 자동 재전송하지 않는다. pending journal, 실제 체결, 포지션 및 거래 이력의 중복 방지·내구성 규칙을 보존한다.
4. STM의 판단과 Controller의 외부 작업 조정을 분리한 기존 구조를 유지한다. 전략 조건, 수수료 정책, 주문 한도, 타이머 판정 기준은 변경하지 않는다.
5. 화면 연결, 거래소 연결, 판정 엔진 상태를 구분한다. 화면 단절만으로 매매 중지를 호출하거나, heartbeat 수신만으로 판정 엔진과 계좌가 정상이라고 간주하지 않는다.
6. 기존 실행 프로세스·계좌·키 설정을 변경하지 않는다. 이번 검토 및 아래 초기 검증은 fake 계좌·가짜 시계·로컬 통신을 사용한다.

## 2. 검토 결론과 증거의 범위

현 코드에서 판정 복구가 계속 대기하거나 장애가 다른 경로로 전파되는 지점 다섯 가지를 확인했다. 아래 항목은 **현재 코드의 발생 가능한 결함과 검증 공백**이다. 운영 로그의 동일 사건을 대조하지 않았으므로 이번에 겪은 실제 장애의 원인을 확정한 보고서는 아니다.

| 번호 | 우선순위 | 확인한 지점 | 발생 가능한 영향 |
| --- | --- | --- | --- |
| R1 | 높음 | 주문·복구의 외부 작업 동안 공통 application lock 점유 | 판정 진행, 새 시세 반영, 상태 조회가 함께 대기 |
| R2 | 높음 | 동기 진단 파일 쓰기를 공통 진단 잠금 안에서 수행 | 디스크 지연이 거래 worker와 UI 통신으로 전파 |
| R3 | 높음 | 공개 시세 수신 및 일부 오류 통지를 socket thread에서 직접 처리 | 거래 잠금 대기가 다음 수신·연결 종료·복구 통지를 지연 |
| R4 | 중간 | 반복 close·resync 경로가 프론트 재시도 지연을 우회 | snapshot·socket 재생성 반복이 복구 중 부하를 증폭 |
| R5 | 높음·우선 수정 | POST 전 준비 만료도 제출 UNKNOWN으로 분류 | 존재하지 않는 주문을 계속 조회하며 판정 복구 대기 |

주문 지연으로 R1이 발생하면 R3의 수신 지연과 snapshot 대기가 뒤따를 수 있다. R2는 주문과 무관하게 통신을 함께 지연시킬 수 있고, R4는 연결 실패가 반복될 때 부하를 늘릴 수 있다. 지연으로 준비 정보가 만료되면 R5처럼 주문 미전송 상태에서도 판정 복구가 지속 대기할 수 있다. 다섯 현상이 항상 동시에 발생한다는 뜻은 아니다.

## 3. 결함별 수정안

### R1. 주문·복구 I/O와 공통 잠금의 결합

**코드 근거**

- [bootstrap/application.py:914](/Users/oscar/Desktop/Binance_Auto/backend/src/binance_auto_trader/bootstrap/application.py:914): application lock을 보유한 채 `asyncio.run(self._runtime_cycle())`를 실행한다.
- [application/trading_controller.py:7335](/Users/oscar/Desktop/Binance_Auto/backend/src/binance_auto_trader/application/trading_controller.py:7335), [같은 파일:7540](/Users/oscar/Desktop/Binance_Auto/backend/src/binance_auto_trader/application/trading_controller.py:7540): 해당 cycle에서 주문 준비와 제출로 이어진다.
- [spot_rest_client.py:1159](/Users/oscar/Desktop/Binance_Auto/backend/src/binance_auto_trader/adapters/binance/spot_rest_client.py:1159): 주문 준비에 여러 REST 조회가 순차 실행된다. live 권한 계층도 [live_permission.py:144](/Users/oscar/Desktop/Binance_Auto/backend/src/binance_auto_trader/bootstrap/live_permission.py:144), [197](/Users/oscar/Desktop/Binance_Auto/backend/src/binance_auto_trader/bootstrap/live_permission.py:197)에서 수수료 정책을 조회한다.
- [spot_rest_client.py:248](/Users/oscar/Desktop/Binance_Auto/backend/src/binance_auto_trader/adapters/binance/spot_rest_client.py:248): 요청별 timeout은 있지만 주문 준비·조회 전체의 절대 마감 시간은 없다. 기본 HTTP timeout 12초를 작업 전체의 12초 상한으로 해석할 수 없다.
- [routes/snapshot.py:25](/Users/oscar/Desktop/Binance_Auto/backend/src/binance_auto_trader/transport/routes/snapshot.py:25): UI snapshot도 같은 잠금을 제한 없이 기다린다. UI의 기본 HTTP 제한은 [BackendUiAdapter.ts:52](/Users/oscar/Desktop/Binance_Auto/UI/src/shared/api/BackendUiAdapter.ts:52)의 5초다.

**확인한 현상**

기존 fake fixture와 실제 trading runtime worker에서 가짜 `prepare_order`를 Event barrier로 지연시켰다. 지연 중 다른 thread의 `snapshot_session()`과 `note_market_input()`은 200ms 관측 동안 완료되지 않았고, barrier 해제 직후 모두 완료됐다. worker 자체는 실패하지 않았고 fake 제출은 한 번이었다. 외부 거래소 호출은 없었다.

이는 교착이 반드시 발생한다는 증거가 아니라, 정상적인 외부 작업 지연도 판정·관측 경로로 전파되는 증거다. snapshot 요청을 UI에서 취소해도 서버의 잠금 대기 thread는 자동 취소되지 않으므로 반복 재조회 시 대기 요청이 누적될 여지도 있다.

**수정 방향**

1. 잠금 안에서 주문 의도, client order ID, 세션·복구 세대, 필요한 버전과 제출 권한을 예약한다.
2. 외부 조회·준비·제출은 단일 실행 주체가 순서대로 수행하되 application lock을 장시간 점유하지 않도록 분리한다. 단순히 기존 `with`만 제거하지 않는다.
3. 결과 반영 시 잠금을 다시 획득해 예약 identity와 최신 정지·긴급 정지·계좌 readiness를 검사한다. STM 전이와 event sequence publication은 짧은 임계 구역에서 원자적으로 수행한다.
4. PREPARED 저장 완료 전 POST 금지, POST 직전 권한 재검사, 한 의도에 대한 제출 직렬화는 유지한다. 오래된 읽기 결과는 폐기할 수 있지만 **이미 제출한 주문의 늦은 응답과 실제 체결은 폐기하지 않고 같은 ID로 대조·반영**한다.
5. 전체 작업에 단조 시계 기반 마감 시간을 추가한다. 단순 future timeout 후 작업을 방치하거나 새 제출을 시작하지 않는다. 제출 여부가 모호하면 기존 UNKNOWN·reconciliation 경로로 보낸다.
6. snapshot 잠금 대기에 제한을 두고 UI의 5초 제한 전에 재시도 가능한 typed 응답을 반환하도록 한다. 대기 요청 수도 제한한다. 추후 불변 읽기 모델을 도입한다면 snapshot과 포함된 sequence를 함께 게시하고, 오래된 값을 최신 정상 상태로 가장하지 않는다.
7. 같은 잠금을 쓰는 계좌·시장 복구 I/O에도 동일 원칙을 적용한다. 정지 요청과 복구 완료가 경합해도 임의 재개하지 않는다.

**완료 검증**

- 주문 준비·조회·저장 지연 중 UI heartbeat와 진단 조회가 지속되고 snapshot이 제한 시간 안에 응답한다.
- 서버가 수락한 뒤 응답을 잃은 POST에서 실제 제출은 한 번이며 동일 ID 조회로 수렴한다.
- 지연 중 stop·manual kill·계좌 단절·세대 교체가 발생해도 추가 제출과 늦은 전략 재개가 없다.
- 체결과 저장의 중간 실패, 재시작, pending 제거 실패에서도 Position·Trade가 한 번만 반영된다.
- 반복 snapshot timeout에도 서버의 대기 thread·진행 중 요청 수가 설정 상한을 넘지 않는다.

### R2. 진단 로그 저장 지연의 전파

**코드 근거**

- [runtime_diagnostics.py:106](/Users/oscar/Desktop/Binance_Auto/backend/src/binance_auto_trader/application/runtime_diagnostics.py:106): 공통 진단 잠금 안에서 sink를 동기 호출한다.
- [diagnostic_log_writer.py:150](/Users/oscar/Desktop/Binance_Auto/backend/src/binance_auto_trader/adapters/filesystem/diagnostic_log_writer.py:150): 파일 열기·쓰기·flush를 호출 thread에서 수행한다.
- [transport/app.py:1765](/Users/oscar/Desktop/Binance_Auto/backend/src/binance_auto_trader/transport/app.py:1765): UI heartbeat 전송 후 같은 진단 경로를 호출한다. 여기서 대기하면 이후 frame과 heartbeat 처리가 진행되지 않는다.

**확인한 현상**

fake sink를 Event barrier로 정지시키자 다른 thread의 `ui_stream_heartbeat` 기록도 반환하지 않았다. 현재 예외 처리는 저장 실패를 격리하지만, 반환하지 않는 저장 작업의 지연은 격리하지 못한다. native liveness의 별도 writer가 존재해도 이 backend 진단 경로의 동기 쓰기는 남아 있다.

**수정 방향**

- 크기 제한이 있는 queue와 단일 writer로 진단 파일 작업을 옮긴다. 호출 측은 외부 I/O 없이 짧게 enqueue하고, 입력 시점의 시각·순서·세션과 불변 payload를 보존한다.
- queue 초과·writer 실패에는 누락 계수와 사건 정보를 남기되 거래·통신 thread에서 기다리거나 동기 파일 기록으로 우회하지 않는다.
- 종료 시 queue 회수에 제한 시간을 적용하고 미기록 건수를 남긴다. writer 정지가 주문 복구나 프로세스 안전 종료의 추가 무한 대기가 되지 않게 한다.
- **변경 대상은 진단 로그뿐이다.** 주문 저널·거래 이력의 fsync와 제출 전 내구성 보장은 비동기 진단 queue로 옮기지 않는다.

**완료 검증**

sink 정지, 파일 오류, queue 초과 상태에서도 판정 worker·UI heartbeat·진단 조회가 계속 진행해야 한다. 기록 순서, 누락 계수, 민감정보 제거, 종료 제한과 회수도 함께 검사한다.

### R3. 시세 수신과 오류 통지의 동기 callback

**코드 근거**

- [spot_websocket_client.py:891](/Users/oscar/Desktop/Binance_Auto/backend/src/binance_auto_trader/adapters/binance/spot_websocket_client.py:891): 공개 시세 socket callback에서 `on_message(payload)`를 직접 실행한다. Gateway·MarketDataController를 거쳐 application 처리와 잠금 대기로 이어진다.
- [spot_websocket_client.py:650](/Users/oscar/Desktop/Binance_Auto/backend/src/binance_auto_trader/adapters/binance/spot_websocket_client.py:650): 계좌 dispatcher의 실패 처리도 동기 disconnect 통지가 끝난 뒤 socket을 닫는다. production 통지는 [application.py:1571](/Users/oscar/Desktop/Binance_Auto/backend/src/binance_auto_trader/bootstrap/application.py:1571)에서 application lock을 기다린다.

**확인한 현상**

fake 공개 시세 consumer를 Event barrier로 정지시키자 socket `on_message`도 반환하지 않았다. 계좌 stream의 정상 FIFO 처리는 이미 분리돼 있으므로 이를 새 결함으로 중복 보고하지 않는다. 남은 오류 통지 대기는 수신·close·복구 알림 지연에 관한 문제이며, 계좌 연결 flag가 정상인 채 신규 주문이 허용된다고 단정하지 않는다.

**수정 방향**

- 공개 시세도 순서와 용량을 제한한 단일 dispatcher로 전달한다. receive thread에는 짧은 검증·enqueue·연결 상태 전환만 남긴다.
- 봉 종료와 시간 경계 이벤트는 임의 합치기·폐기 대상에서 제외한다. queue 초과 시 현재 세대를 폐기하고 기존 full resync를 요청하며, 누락된 시장 정보로 판단하지 않는다.
- 실제 disconnected/readiness 상태 전환은 즉시 유지하면서, application lock이 필요한 복구 통지는 중복 병합되는 별도 실행 경로에 전달한다. 통지 대기가 socket close를 막지 않게 한다.
- 재연결·종료 시 오래된 dispatcher와 callback이 새 세션에 이벤트를 넣지 못하도록 세대와 구독 identity를 검사한다.

**완료 검증**

application lock을 의도적으로 점유한 상태에서 수신 callback과 close가 반환해야 한다. overflow는 복구를 정확히 한 번 요청해야 하고, 순서·봉 경계·이전 세대 폐기·계좌 readiness 차단을 각각 검사한다.

### R4. 반복 연결 실패에서 재시도 지연 우회

**코드 근거**

- [BackendUiAdapter.ts:2086](/Users/oscar/Desktop/Binance_Auto/UI/src/shared/api/BackendUiAdapter.ts:2086): 재시도 가능한 socket close에서 즉시 `full_resynchronize()`를 호출한다.
- [BackendUiAdapter.ts:2135](/Users/oscar/Desktop/Binance_Auto/UI/src/shared/api/BackendUiAdapter.ts:2135): resync 지시와 sequence gap도 같은 즉시 경로를 사용한다.
- [BackendUiAdapter.ts:2325](/Users/oscar/Desktop/Binance_Auto/UI/src/shared/api/BackendUiAdapter.ts:2325): snapshot이 성공하면 바로 socket을 연다. 별도 실패 처리의 [2382행](/Users/oscar/Desktop/Binance_Auto/UI/src/shared/api/BackendUiAdapter.ts:2382)에 있는 1·2·5·10·30초 지연을 거치지 않는다.
- [BackendUiAdapter.ts:2443](/Users/oscar/Desktop/Binance_Auto/UI/src/shared/api/BackendUiAdapter.ts:2443): 정상 frame 하나로 retry 횟수가 초기화된다.
- 서버도 [transport/app.py:1777](/Users/oscar/Desktop/Binance_Auto/backend/src/binance_auto_trader/transport/app.py:1777), [1790](/Users/oscar/Desktop/Binance_Auto/backend/src/binance_auto_trader/transport/app.py:1790)에서 인증 후 timeout·내부 오류에 close `1011`을 보낼 수 있다.

**확인한 현상**

기존 [BackendUiAdapter.recovery.test.ts:349](/Users/oscar/Desktop/Binance_Auto/UI/src/shared/api/BackendUiAdapter.recovery.test.ts:349)는 100회 단절·복구를 매회 가짜 시간 0ms 진행만으로 처리한다. 이 테스트는 타이머 누적과 명령 재전송을 막는 증거이지만, 요청 빈도 제한의 증거는 아니다. HTTP snapshot만 성공하고 socket이 반복 종료되는 상황에서는 같은 즉시 재연결 경로가 반복된다.

**수정 방향**

- 최초 단절의 신속한 복구는 유지하되, 안정적인 정상 수신 전에 재발한 close·gap·resync를 공통 재시도 스케줄러로 보낸다.
- retry 횟수는 snapshot 성공이나 frame 한 번으로 초기화하지 않고, 일정한 정상 수신 유지 기간 이후 초기화한다. 기존 지연 단계와 최대 30초 상한을 재사용한다.
- 한 번에 하나의 snapshot 요청·socket·retry timer만 유지하고 기존 세대 검사, 명령 식별자, shutdown 차단 조건을 보존한다.
- 정상 snapshot과 같은 세션·cursor의 heartbeat/event를 확인한 뒤에만 기존 복구 상태를 해제한다. 화면 디자인·문구 변경은 없다.

**완료 검증**

close `1011` 반복, 반복 resync 지시, 정상 frame 한 번 후 재단절을 가짜 시간으로 재현한다. 재시도 횟수와 간격, 단일 timer, 정상 회복 후 예산 초기화, stop 후 잔여 요청 없음, 주문 재전송 0회를 검사한다.

### R5. 전송 전 만료를 제출 결과 불명으로 처리

**코드 근거**

- [spot_rest_client.py:1271](/Users/oscar/Desktop/Binance_Auto/backend/src/binance_auto_trader/adapters/binance/spot_rest_client.py:1271), [1652](/Users/oscar/Desktop/Binance_Auto/backend/src/binance_auto_trader/adapters/binance/spot_rest_client.py:1652), [1719](/Users/oscar/Desktop/Binance_Auto/backend/src/binance_auto_trader/adapters/binance/spot_rest_client.py:1719): 준비 증거가 30초를 초과하면 실제 POST 전에 `OrderPreparationRequiredError`를 발생시킨다.
- [trading_controller.py:7523](/Users/oscar/Desktop/Binance_Auto/backend/src/binance_auto_trader/application/trading_controller.py:7523): Controller는 이 호출 이전에 SUBMITTED를 영속화한다. crash 경계에서 주문을 놓치지 않기 위한 보수적인 기록 자체는 필요한 방어다.
- [trading_controller.py:7541](/Users/oscar/Desktop/Binance_Auto/backend/src/binance_auto_trader/application/trading_controller.py:7541): 이후 모든 예외를 전송 여부 구분 없이 UNKNOWN으로 변환한다.
- [trading_controller.py:6134](/Users/oscar/Desktop/Binance_Auto/backend/src/binance_auto_trader/application/trading_controller.py:6134): 같은 주문 조회 결과가 UNKNOWN이면 세션 복구를 재시도한다. 미전송 확정 경로가 없어 반복 부재 응답만으로 복구를 끝낼 수 없다.

**확인한 현상**

실제 REST adapter와 가짜 HTTP transport를 기존 Controller 복구 fixture에 연결하고, 준비 후 시계를 31초 진행시켰다. **POST 0회, 전송 시도 증거 없음**인데 Order와 journal은 UNKNOWN, pending은 1개였다. 이어 같은 ID 조회에 `ORDER_NOT_VISIBLE`을 세 번 반환하자 매번 `SessionRecoveryRetry`가 발생하고 복구 대기가 유지됐다. 실제 네트워크는 사용하지 않았다.

30초 freshness 제한과 UNKNOWN에서 새 주문을 막는 정책은 유지해야 한다. 결함은 제한 자체가 아니라, 미전송이 확정된 실패와 실제 제출의 불확실성을 구분해 안전하게 종료하는 경로가 없다는 점이다.

**수정 방향**

1. adapter에서 재준비 가능한 만료와 실제 I/O 시작 여부를 구분한 typed 결과를 제공한다. 첫 POST 후 재시도 준비에서도 같은 예외가 발생할 수 있으므로 **예외 클래스만 보고 미전송으로 판단하지 않는다.**
2. 현재 실행의 정확한 client ID·intent·전송 이력을 대조해 미전송이 확실한 경우에만 그 사실을 durable journal에 기록한다. 메모리의 전송 증거 부재를 재시작 후 미전송 증거로 사용하지 않는다.
3. 미전송 확정 기록과 정리가 끝난 뒤 최신 세션·신호·정지 상태를 재검증해 재준비하거나 해당 의도를 만료 처리한다. 기존 제출·재시도 예산을 임의 초기화하지 않는다.
4. fingerprint 불일치·장부 손상 등 내부 불변식 오류는 재준비 가능한 만료와 분리해 계속 차단한다. 실제 전송 이력이 있거나 영속화 결과가 불명확하면 기존 same-ID reconciliation을 유지한다.
5. durable 상태를 확장한다면 구버전 journal 읽기와 crash cut-point 검증을 함께 보완한다. UNKNOWN 기록을 일괄 삭제하거나 부재 응답 횟수만으로 재제출하지 않는다.

**완료 검증**

정확히 30초와 31초 경계, 준비 후 journal 지연, POST 직전 실패, 첫 POST 후 후속 준비 실패를 각각 검사한다. 미전송 확정은 불필요한 UNKNOWN 대기를 벗어나고, 제출 가능성이 남은 경우는 신규 주문을 막아야 한다. 미전송 확정 저장·정리 중 crash와 정지 경합도 재시작 fixture로 검증한다.

## 4. 기존 수정으로 보호되는 경계

다음 항목은 이번에 새로 발견한 결함으로 취급하거나 방어를 완화하지 않는다.

- idle UI stream의 실제 heartbeat와 frontend 수신 제한, 전체 snapshot 기반 재연결.
- 이전 연결 세대의 callback 차단, close 없는 onerror 처리, 화면 반영 예외의 제한된 복구.
- 같은 주문 ID 조회, 중복·늦은 partial fill 처리, dirty Trade 저장 및 pending REMOVE 재시도.
- rate-limit `Retry-After`와 주문별 wait-not-before 보존. 초기 주문 응답에서 대기가 소실된다는 가설은 추가 확인 결과 기존 코드·테스트가 방어하고 있어 제외했다.
- 정지·종료·복구 세대 경합과 명령 식별자 보존.
- 내부 실행기 불변식 실패, 장부 손상, 프로세스 소유권 불명확 상태의 fail-close. worker나 sidecar를 무조건 다시 시작하는 방식으로 우회하지 않는다.

## 5. 실행 순서와 검증 관문

1. **재현 고정:** 이번 barrier 기반 지연과 가짜 시간 반복 단절 시나리오를 정식 회귀 테스트로 추가한다. 현 코드에서 결함을 잡는지 먼저 확인하고, 기준 UI의 소스와 화면 상태를 기록한다.
2. **미전송 실패 분류:** R5를 먼저 보완해 준비 만료만으로 세션이 복구 대기에 남지 않게 한다. durable 상태와 실제 전송 이력의 검증을 이 단계의 필수 관문으로 둔다.
3. **독립적인 장애 격리:** R2의 진단 writer 분리와 R4의 재시도 지연을 각각 구현·검증한다. 이 작업들은 R5와 독립적으로 진행할 수 있다.
4. **수신 경계 분리:** R3의 공개 시세 dispatcher와 오류 통지를 보완한다. FIFO·세대·봉 경계 계약을 우선 검증한다.
5. **주문·복구 임계 구역 개선:** R1을 예약 → 외부 작업 → 검증된 결과 반영 단계로 나눈다. 범위가 가장 크므로 별도 변경 단위로 다루며 durable 저널과 실제 주문 상태에 관한 기존 검증을 모두 유지한다. snapshot 대기 제한도 함께 적용한다.
6. **통합 장애 검증:** 주문 응답 지연 + 시세 유입 + 계좌 callback + UI 재연결 + 로그 지연을 조합한다. 주문 모호성, 평가 진행, 데이터 최신성, 화면 복구를 별도 결과로 판정한다.
7. **회귀·화면 보존 확인:** 전체 주문 없는 backend/UI 테스트, TypeScript 검사, 관련 architecture 검사와 변경된 native 코드가 있다면 해당 Rust 테스트를 실행한다. 화면 비교는 같은 fixture·시간·viewport에서 수정 직전과 직후를 비교한다.

코딩 컨벤션 자동 검사는 현재 `domain/trading`에 한정된 부분이 있으므로, 이것만 통과했다고 모든 변경 파일이 준수한다고 판단하지 않는다. 변경한 Python·TypeScript·Rust의 이름, 함수 문서, 주석, 공백은 파일별로 추가 점검한다.

화면 동일성은 CSS·자산·화면 문구·JSX 시각 구조의 diff 부재와 기존 화면 테스트, 변경 직전 대비 fresh capture로 확인한다. 저장소의 과거 Figma SSIM 결과를 이번 변경의 화면 동일성 증거로 대체하거나, 기존 visual baseline·기준값을 수정해서 통과시키지 않는다.

### 통합 완료 기준

| 장애 주입 | 반드시 확인할 결과 |
| --- | --- |
| 주문 준비·응답 지연 | 통신 경로는 응답하고 판정 상태를 관측할 수 있음. 미확정 주문 중 추가 제출 없음 |
| POST 수락 후 응답 유실 | 같은 ID 조회로 실제 체결과 일치, 중복 주문 0건 |
| POST 전 준비 만료 | 미전송을 영속적으로 확정한 뒤 재준비 또는 의도 만료, 불필요한 UNKNOWN 대기 없음 |
| 진단 파일 정지·queue 초과 | 거래·heartbeat 유지, 누락 기록, writer 수·queue 크기 상한 유지 |
| 시세 consumer 지연·overflow | socket 처리 유지, 신뢰할 수 없는 세대 차단, 전체 재동기화 후 최신 평가 |
| socket 반복 close·gap | 지정된 backoff와 단일 복구 작업, 무제한 즉시 반복 없음 |
| 복구 중 stop·shutdown·manual kill | 뒤늦은 전략 재개·추가 제출 없음, 기존 종료 안전 조건 유지 |
| durable 저장의 중간 실패 | pending·Position·Trade 대조가 끝나기 전 주문 gate 차단 |
| 정상 복구 | backend 평가 시각/순번이 다시 전진하고 UI도 최신 snapshot/event를 표시 |
| 정상 화면 비교 | 동일 상태의 레이아웃·스타일·문구·구조 동일 |

진행 신호는 기존 진단에 잠금 대기 시간, 외부 작업 소요 시간, queue 깊이·초과 횟수, 마지막 실제 평가 시각, retry 횟수를 추가하는 방식으로 수집한다. 새 UI 요소를 만들지 않고 API 키·token·원문 주문 요청은 기록하지 않는다.

## 6. 이번 검토에서 수행한 검증

| 범위 | 결과 |
| --- | --- |
| 세션 복구·Case C 복구·실제 runtime worker·계좌 복구·로컬 WebSocket·코딩 컨벤션 | backend 52개 통과 |
| BackendUiAdapter·복구·payload mapper·live bootstrap·실시간 전략 process fixture | UI 5개 파일, 202개 통과 |
| 별도 경계 점검: WebSocket client·진단 writer·runtime worker | 33개 통과. 앞의 52개와 worker 7개가 중복되므로 단순 합산하지 않음 |
| 지연·만료 주입 | 주문 준비 barrier, 진단 sink barrier, 공개 시세 callback barrier에서 해당 대기 결합 확인. 31초 준비 만료에서 POST 0회인 UNKNOWN 복구 대기 재현 |
| 외부 거래소·실제 주문·credential 접근 | 수행하지 않음 |
| 실행 코드·기존 UI 수정 | 없음 |

backend 최초 실행에서는 sandbox의 로컬 포트 bind 제한으로 WebSocket 7개가 실행되지 않았다. 로컬 테스트 포트를 허용한 재실행에서 같은 52개가 모두 통과했다. 제품 코드 실패와 실행 환경 제한을 구분한다.

이번 결과는 기존 회귀가 통과한다는 증거다. **위 수정안의 구현 완료나 운영 장애의 완전 해결을 뜻하지 않는다.** 전체 suite, 장시간 복합 장애 시험, 실제 데스크톱 전후 화면 비교는 구현 후의 완료 조건으로 남긴다.
