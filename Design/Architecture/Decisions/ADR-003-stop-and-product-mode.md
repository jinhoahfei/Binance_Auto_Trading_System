# ADR-003 — Stop 흐름, 거래 상품과 실행 모드

| 항목 | 값 |
|---|---|
| 상태 | Accepted |
| 결정일 | 2026-08-20 |
| 적용 결정 | D-05, D-06, D-09, D-15 |

## 1. 거래 상품 결정

첫 release의 canonical 상품은 Binance Spot `ETHUSDT`, long-only다.

- base asset은 `ETH`, quote asset은 `USDT`다.
- Margin, Futures, leverage, borrow/repay와 short position은 지원하지 않는다.
- `Position.quantity`의 유효 범위는 `0` 이상이다.
- BUY 수량은 사용 가능한 quote balance와 exchange filter 안에서 계산한다.
- SELL 수량은 authoritative `Position.quantity`와 실제 free ETH 중 작은 값을 넘을 수 없다.
- 수량이 0이면 주문을 만들지 않고 `ZERO_POSITION`으로 거부한다.
- `TYPE_4`의 문서에 있는 “반등 숏”은 Spot long-only 범위에 포함되지 않는다. TYPE_4가
  별도 Event-Action Table을 갖더라도 상품 ADR을 대체하기 전에는 naked sell을 만들 수
  없다.
- 현재 UI fixture의 `ETH/KRW` 표시는 demo data이며 production symbol 계약이 아니다.

Margin 또는 Futures를 지원하려면 이 ADR을 대체하고 Account, Position, Order,
liquidation, funding, leverage와 위험 한도를 별도로 명세해야 한다. 기존 Spot 클래스에
조건문만 추가해 혼합하지 않는다.

## 2. Stop 상태 분기

사용자 확인 뒤 `RUNNING` 세션에 처음 적용되는 Python Operation
`TradingController.stop_trading(*, command_id, expected_version)`
(Communication alias `stopTrading(commandId, expectedVersion)`)은 먼저
`STOP_CONFIRMED`를 TradingSTM에 전달한다. 그 다음 authoritative Position과 pending
주문 상태로 아래 절차를 실행하고 `TradingSessionResult`를 반환한다. 이미
`STOPPING`, `RECONCILIATION_REQUIRED` 또는 `LOGIC_TERMINATED`인 세션의 후속 stop은
새 STM Action을 만들지 않는 성공 no-op이며 이 분기 표를 다시 실행하지 않는다.

| 조건 | TradingSTM 경로 | Controller 동작 | 완료 조건 |
|---|---|---|---|
| `quantity == 0`이고 pending 없음 | `G-05` | 신규 event 수신·timer·구독 정리 | `LOGIC_TERMINATED`, sell 호출 0회 |
| `quantity > 0`이고 pending 없음 | `G-06` | 현재 수량만 force-sell, fill/Position/history 반영 | 수량 0 뒤 `FORCE_SELL_FINISHED` → `G-06F` |
| pending 주문 존재 | `G-06P` | ADR-002의 query/cancel/reconcile 후 잔여 수량만 force-sell | pending 없음 + 수량 0 뒤 `FORCE_SELL_FINISHED` |
| force-sell terminal zero-fill/실패 | `G-06R` | 3초 간격 최대 4회 retry, 이후 reconciliation lock | 성공 전에는 종료하지 않음 |
| 상태 불명 또는 저장 실패 | `STOPPING`/`RECONCILIATION_REQUIRED` | 같은 주문 사실 확인·저장 복구 | 운영상 안전 상태가 확인될 때까지 종료 금지 |

`Position.quantity`가 0인 branch에서는 `APIGateway.sellAllPosition(...)` 또는
`submitOrder(SELL, ...)`을 호출하지 않는다. UI가 표시한 보유 여부가 아닌 backend
Position snapshot을 최종 기준으로 삼는다.

### 2.1 재시작 복구 포지션의 명시 청산

startup reconciliation에서 설명 가능하게 복원한 열린 Position은 일반
`stop_trading()`의 active-session 계약으로 위장하거나 자동매매로 resume하지 않는다.
사용자가 별도 경고 UI에서 확인한 경우에만
`TradingController.liquidate_recovered_position(*, command_id, expected_version)`
(Communication alias `liquidateRecoveredPosition`)이 liquidation-only session을 만든다.

이 Operation은 startup reconciliation과 account stream이 완료되고, normal session이
`NOT_STARTED`이며, pending·dirty persistence·scheduled same-ID query가 없고, authoritative
Position의 양수 수량·owner·snapshot 및 durable open lot의 단일 지원 REGIME과 owner가
일치할 때만 허용한다. effective free ETH는 Position 전량 이상이어야 한다. 사용자가 승인한
max-notional의 승인 의미는 노출을 늘리는 BUY entry cap이다. 일반 SELL의 기존 local cap은
유지하고, 가격 상승 뒤 노출을 줄이는 STOP/recovery SELL의 quote 금액에는 적용하지 않는다.
UI의 현재 선택 REGIME은 복구
provenance로 사용하지 않는다.
검증된 REGIME으로 fresh STM과 Context를 초기화하되 `TradingSTM.run()`을 호출하지 않고
`STOP_CONFIRMED`를 직접 처리해 기존 `G-06`, `G-06F`, `G-06R`과 ADR-002 주문 경로만
재사용한다. 이 세션에는 시장 event와 신규 BUY를 전달하지 않는다.

동기 REST `FILLED`와 WebSocket terminal 결과도 먼저 기존 직렬 queue에 넣고, production
bootstrap의 단일 event runtime worker가 Controller의 bounded cycle을 깨워 처리한다.
따라서 HTTP route는 `drain_events()`를 직접 호출하지 않으며, Position·history 반영 뒤의
`FORCE_SELL_FINISHED`가 다음 microstep에서 처리돼 `G-06F`와 `TERMINATED`를 게시한다.
`UNKNOWN`과 active partial은 같은 ID의 due 조회만 예약하고, terminal partial
force-sell은 실제 잔여 수량만 고정 3초 뒤 재제출한다.

복구 Operation의 최초 주문은 최신 signed commission 정책과 symbol filter를 통과한 뒤에도
`requested_quantity == submitted_quantity == authoritative Position.quantity`여야 한다.
free balance 또는 LOT_SIZE 내림 때문에 이 등식이 깨지면 여러 부분 청산으로 확대하지
않고 pending journal·REST POST 전에 거부한다. 최초 effect 전 실패는 같은 command ID로
재시도할 수 있도록 `NOT_STARTED`로 원자 복원한다. 실제 terminal partial 뒤의 잔량이 filter를
통과하지 못하면 이미 생긴 fill과 history를 보존하고 operator reconciliation으로 닫는다.

같은 command ID와 payload는 최초 receipt를 재사용한다. 이미 이 청산이
`STOPPING`, `RECONCILIATION_REQUIRED` 또는 `TERMINATED`이면 다른 command ID도 현재
상태만 반환하며 새 force-sell intent를 만들지 않는다. 외부 POST 전 준비 실패는 원래
`NOT_STARTED` 상태로 복원하고, durable journal 생성 이후의 실패나 모호한 응답은 상태를
되돌리지 않고 same-ID reconciliation으로 닫는다.

## 3. 실행 중 REGIME 변경

ADR-001에 따라 active session의 REGIME 변경을 거부한다. 사용자는 stop이 완전히
종료된 뒤 REGIME을 다시 선택하고 새 session을 시작한다. pending 또는 reconciliation
상태에서 “변경 후 계속”하는 경로는 없다.

## 4. 실행 모드

backend는 다음 네 값을 갖는 `ExecutionMode`를 사용한다.

| 모드 | 시장/account 연결 | 주문 adapter | 용도 |
|---|---|---|---|
| `disabled` | public read-only만 허용 가능 | 모든 submit/cancel 차단 | 기본값, UI·조회 개발 |
| `fake` | fixture 또는 local fake | 메모리 fake만 허용 | unit/integration/E2E |
| `testnet` | Binance Spot Testnet | testnet endpoint만 허용 | 명시 실행하는 검증 |
| `live` | Binance production | production endpoint | Phase 13 승인 뒤에만 허용 |

설정 누락, 알 수 없는 문자열, 설정 parsing 실패는 모두 `disabled`다. 다른 모드로
fallback하지 않는다. Domain/STM은 mode를 알지 않으며 Gateway와 bootstrap이 외부 효과
gate를 적용한다.

Testnet의 실제 MARKET BUY는 dust 회계 정책이 추가되기 전까지 signed account commission
응답의 standard/special/tax `taker + buyer` 합이 모두 0일 때만 허용한다. Binance 공식
수수료 규칙상 BNB로 지불하지 않는 BUY의 수수료는 수신 base quantity에서 차감될 수 있어
LOT_SIZE 밖 잔량을 만들기 때문이다. BNB 등 제3 수수료 자산 가능성도 현재 회계 범위 밖이므로
주문 준비 전에 차단한다. 두 discount enable flag가 참이면 할인율이 0이어도 tax/special
수수료는 discount asset으로 전환될 수 있으므로 검증된 string asset과 두 flag로 제3 자산
가능성을 판정한다. 공식 schema는 `discountAsset` string을 요구하지만 2026-08-24 실제 Spot
Testnet은 두 flag가 참인 all-zero 정책에서 explicit `null`을 반환했다. 이 schema drift는
standard/special/tax의 maker/taker/buyer/seller 12개 비율과 discount가 모두 정확히 0일
때만 수수료 자산 부재로 제한 수용하고, 필드 누락이나 하나라도 양수인 null 조합은 거부한다.
이는 live enable 정책이 아니라 Phase 9 Testnet의 보수적 fail-closed 조건이다.

Phase 9의 `10 USDT`는 이미 완료한 개별 Testnet BUY의 승인 cap이다. Phase 13의
후속 actual Testnet Case 2는 local in-scope gate가 모두 통과한 뒤에만 별도 opt-in으로
허용하며, 각 신규 BUY의 decision price × submitted quantity를 `100 USDT`
이하로 제한한다. 이 hard cap은 configured risk policy의 nullable 상한과 다른
Testnet 실행 경계이다. STOP/recovery SELL은 BUY cap으로 막지 않고 authoritative
Position, free ETH와 최신 filter가 허용하는 정확한 보유 수량만 청산한다.

프로젝트 자체의 배포 범위는 비공개·개인용이며 배포 license를 부여하지 않는다.
Python backend는 `LicenseRef-Proprietary`와 `Private :: Do Not Upload`, UI npm package는
`private: true`와 `UNLICENSED`, Tauri crate는 `publish = false`와 repository `LICENSE`를
선언한다. Root·backend proprietary notice와 README도 같은 범위를 고지한다. Local supply
inventory는 이 세 manifest와 repository notice byte를 결합하고 project 자체의
`UNKNOWN` license group이 없음을 검증한다. 이 결정은 dependency의 license, notice와
기타 제3자 의무를 제거하지 않는다. PyInstaller 계열의 두 third-party license group,
raw license scan, SBOM과 release artifact binding은 별도 `NO_GO`로 남는다.

## 5. Live 승인 gate

`live` 주문은 다음 조건을 모두 만족할 때만 가능하다.

1. Phase 0~13 완료 증거와 testnet 전체 trace가 있는 release artifact다.
2. backend 전용 설정에서 `execution_mode=live`를 명시했다.
3. 사용자 승인 record가 존재하며 `schema_version`, 승인 release commit,
   `approved_at`, `approved_by`를 포함한다.
4. versioned `RiskPolicy`가 명시적으로 주입되고 승인 record가 policy version과
   세 상한, daily-loss 범위와 manual-kill 동작을 정확히 bind한다. 세 상한은
   양의 유한 `Decimal` 또는 명시적 `None`이며 누락된 policy를 무제한으로
   대체하지 않는다.
5. 현재 binary의 commit과 승인 record의 commit이 정확히 일치한다.
6. 앱을 시작할 때마다 사용자가 `LIVE ETHUSDT` 문구를 직접 확인한다. 이 확인은 저장해
   다음 실행에 재사용하지 않는다.
7. API credential은 renderer 밖에서 읽고, Spot 거래 권한만 가지며 출금 권한이 없다.
8. reconciliation이 끝났고 open order, Position, Account snapshot이 서로 일치한다.

한 조건이라도 없거나 일치하지 않으면 `LIVE_GATE_NOT_SATISFIED`로 주문을 거부하고
mode를 효과적으로 `disabled`로 취급한다. 2026-08-29 사용자가 확정한
`max_order_notional=None`, `max_position_notional=None`, `max_daily_loss=None`은
configured policy의 **명시적 무제한**이며 policy 부재 `UNAVAILABLE`과 다르다.
`daily_loss_scope=REALIZED_ONLY`, `manual_kill_behavior=CANCEL_AND_LIQUIDATE`를
함께 적용한다. 이 선택은 Phase 13 구현 결정이지 live release 승인은 아니므로,
별도 live 승인 record와 서명된 checklist 전에는 계속 disabled다.

세 상한이 `None`이어도 후보 주문, 현재·예약·예상 Position notional과 KST 실현
손실은 `Decimal`로 계산·게시한다. 각 비교만 건너뛰며 거대한 숫자로 치환하지
않는다. 이 정책에는 단건·누적·일일 운영 cap이 없으므로 전략 손절은 운영
위험 상한의 대체가 아니라는 잔여 위험을 live 승인 전에 다시 검토한다.

유한 `max_daily_loss`에 도달하거나 reconciliation이 필요한 순간에는 신규 BUY를
막는다. 이미 존재하는 Position의 안전한 정리, cancel, same-ID query와 history
복구는 신규 노출 차단으로 막지 않는다. `CANCEL_AND_LIQUIDATE`는 kill receipt를
먼저 durable하게 저장한 뒤 app-owned 주문을 cancel·reconcile하고 정확한 잔여 Position을
기존 STOP/recovery 경로로 청산한다. 현재 구현은 receipt fsync 후 durable
app-owned pending을 same-ID query하고, 취소 가능한 주문을 개별 cancel한 뒤 같은
ID를 다시 조회해 terminal/partial을 History·Position에 reconcile한다. 잔여 보유량은
canonical STOP/recovery SELL로 청산하고, restart는 activation epoch의 같은 cleanup
identity·behavior·policy version으로 자동 재개한다. Controller의 authoritative cleanup
boolean은 account stream readiness, app-owned open order 0, pending/UNKNOWN 0, Position 0이
모두 확인된 뒤에만 `True`다. RECON activation, reconnect same-ID re-cancel, partial residual SELL,
fresh release TOCTOU와 cleanup-incomplete shutdown 차단의 focused local test는 통과했다. 실제 Phase 13
Testnet Case 2와 local suite에 없는 추가 외부 timeout/5xx/persistence 조합은 아직 남아 있다.

Phase 13의 상세 계산, pending reservation, manual kill과 policy-version mismatch는 ADR-006을
적용한다. ADR-006의 risk gate는 신규 노출 BUY만 차단하고, 일반 전략 SELL,
operator가 명시한 STOP/recovery SELL, cancel, same-ID query, reconciliation과 persistence
복구를 계속 허용한다. 두 ADR의 적용 범위가 겹치면 신규 노출 증가와 기존 노출
감축을 이 규칙으로 구분한다.

## 6. 종료와 실패 표시

- stop command는 command ID와 expected Context version으로 idempotent하게 처리하고 typed `TradingSessionResult`를 반환한다.
- recovered-position liquidation은 일반 stop과 분리된 command namespace를 사용하고 typed `TradingSessionResult`를 반환한다.
- 이미 `STOPPING` 또는 `RECONCILIATION_REQUIRED`이면 새 force-sell intent를 만들지 않고 현재 진행 상태를 반환한다.
- `LOGIC_TERMINATED`에서 다시 stop하면 성공 no-op을 반환한다.
- 강제 매도 실패 시 UI에는 order ID, 남은 수량, retry 횟수와 다음 조치가 표시되어야
  하며 “중지 완료”로 표시하지 않는다.
- sidecar/process 종료는 open order 또는 Position이 설명되지 않은 상태에서 자동
  강제 종료하지 않는다.

## 7. 검증 의무

- [x] Spot/Margin/Futures와 short 허용 범위가 확정되었다.
- [x] 포지션 0·보유·pending stop branch와 완료 결과가 확정되었다.
- [x] 네 실행 모드와 default `disabled`가 확정되었다.
- [x] live 승인 gate는 값 누락 시 fail closed하도록 확정되었다.
- [x] Phase 7에서 position 0 sell Action 0회와 세 stop branch를 테스트했다. 실제 Gateway 호출 검증은 Action executor가 구현되는 Phase 8/9 범위다.
- [x] Phase 9에서 설명 가능한 복구 Position의 명시 청산만 허용하고 자동 resume를 금지하는 정책을 확정했다.
- [x] Phase 9 복구 청산은 free·filter 뒤 정확한 Position 전량 한 주문만 허용하고, 신규 Testnet MARKET BUY의 수신 ETH 수수료 가능성을 dust 회계 전까지 차단한다. 이미 durable한 Position의 recovery SELL은 과거 BUY 정책 변화만으로 막지 않으며, max-notional의 승인 의미는 BUY entry이고 STOP/recovery SELL만 예외다.
- [x] Phase 13에서 configured-unbounded 세 상한, `REALIZED_ONLY` 및
  `CANCEL_AND_LIQUIDATE` 정책 값을 사용자가 확정했다.
- [x] Configured-unbounded domain·wire·UI gate와 `CANCEL_AND_LIQUIDATE`의 receipt-first
  cancel·same-ID reconcile·안전 청산, restart resume·activation replay·policy provenance를
  구현하고 focused local test로 검증했다.
- [ ] Public market event로 시작하는 실제 Phase 13 Testnet trace와 local suite에 없는 추가 외부
  timeout/5xx/persistence 조합 evidence를 완료한다.
- [ ] Live release commit, signed checklist와 별도 live 승인 record를 확정한다.

### Phase 7 완료 증거

2026-08-21 KST, 시작 커밋 `3e799e126bbb87a88b1e3a522c8f1a7015e5a8e0`에서
`RUNNING` 세션의 최초 stop branch가 `STOP_CONFIRMED`를 먼저 처리하도록 연결했다.
통합 테스트는 무포지션 G-05에서 sell Action이 0회임을 확인하고, 보유 Position의
G-06 `ForceSellAll`, pending 주문의 G-06P cancel/reconcile Action을 각각 검증한다.
이미 중지·종료 상태인 후속 stop은 성공 no-op을 반환한다. Phase 8 전에는 이 외부
효과를 typed Action으로만 기록하며 실제 Gateway 주문·취소·reconciliation은 실행하거나
호출 횟수를 검증하지 않는다.
