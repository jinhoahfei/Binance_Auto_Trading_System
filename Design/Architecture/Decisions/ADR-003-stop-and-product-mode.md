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

## 5. Live 승인 gate

`live` 주문은 다음 조건을 모두 만족할 때만 가능하다.

1. Phase 0~13 완료 증거와 testnet 전체 trace가 있는 release artifact다.
2. backend 전용 설정에서 `execution_mode=live`를 명시했다.
3. 사용자 승인 record가 존재하며 `schema_version`, 승인 release commit,
   `approved_at`, `approved_by`를 포함한다.
4. `max_order_notional`, `max_position_notional`, `max_daily_loss`가 사용자에 의해
   각각 0보다 큰 Decimal로 설정되어 있다. 안전한 임의 기본값은 없다.
5. 현재 binary의 commit과 승인 record의 commit이 정확히 일치한다.
6. 앱을 시작할 때마다 사용자가 `LIVE ETHUSDT` 문구를 직접 확인한다. 이 확인은 저장해
   다음 실행에 재사용하지 않는다.
7. API credential은 renderer 밖에서 읽고, Spot 거래 권한만 가지며 출금 권한이 없다.
8. reconciliation이 끝났고 open order, Position, Account snapshot이 서로 일치한다.

한 조건이라도 없거나 일치하지 않으면 `LIVE_GATE_NOT_SATISFIED`로 주문을 거부하고
mode를 효과적으로 `disabled`로 취급한다. 한도 값은 Phase 13의 별도 사용자 승인 대상이며
이 ADR에서 숫자를 추측하지 않는다.

`max_daily_loss`에 도달하거나 reconciliation이 필요한 순간에는 신규 BUY/SELL을 막는다.
이미 존재하는 Position의 안전한 정리는 운영자 확인과 동일 주문 reconciliation 정책으로만
수행한다.

## 6. 종료와 실패 표시

- stop command는 command ID와 expected Context version으로 idempotent하게 처리하고 typed `TradingSessionResult`를 반환한다.
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
- [ ] Phase 13에서 사용자가 한도와 release 승인을 별도로 확정한다.

### Phase 7 완료 증거

2026-08-21 KST, 시작 커밋 `3e799e126bbb87a88b1e3a522c8f1a7015e5a8e0`에서
`RUNNING` 세션의 최초 stop branch가 `STOP_CONFIRMED`를 먼저 처리하도록 연결했다.
통합 테스트는 무포지션 G-05에서 sell Action이 0회임을 확인하고, 보유 Position의
G-06 `ForceSellAll`, pending 주문의 G-06P cancel/reconcile Action을 각각 검증한다.
이미 중지·종료 상태인 후속 stop은 성공 no-op을 반환한다. Phase 8 전에는 이 외부
효과를 typed Action으로만 기록하며 실제 Gateway 주문·취소·reconciliation은 실행하거나
호출 횟수를 검증하지 않는다.
