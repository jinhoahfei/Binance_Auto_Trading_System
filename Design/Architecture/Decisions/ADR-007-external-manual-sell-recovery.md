# ADR-007 — 외부 수동 매도 이후 재시작 복구

| 항목 | 값 |
| --- | --- |
| 상태 | Accepted |
| 결정일 | 2026-09-10 |
| 적용 범위 | 앱의 열린 ETHUSDT Position에 대응하는 외부 SELL의 fresh startup 복구 |
| 선행 계약 | ADR-004 이력·원가·멱등성, ADR-006 process-lifetime reconciliation |

## 1. 문제와 책임

앱 매수 뒤 사용자가 Binance에서 직접 매도하면, 실행 중 account stream은 외부 체결을
감지하고 주문 gate를 닫는다. 기존 fresh startup은 앱 client ID prefix 주문만 복구하므로
수동 매도 이후에도 BUY Position이 남고, 실제 ETH 잔고가 부족하다는 이유로 시작에 실패했다.

실행 중 외부 체결을 감지한 process의 영구 차단은 유지한다. 복구는 새 process가 소유권을
확보한 뒤 `TradingController.reconcile_startup_state()`에서만 수행한다. `TradingSTM`의
Case B/C 전략 EVENT, 진입 조건, 청산 guard를 만들거나 건너뛰는 동작이 아니다.

## 2. 조회와 적용 순서

1. 기존 durable history와 pending journal을 복원하고 앱 주문의 same-ID 조정을 완료한다.
2. 열린 Position이 있으면 마지막 durable 주문 ID부터 모든 client의 ETHUSDT 주문을 조회한다.
   `allOrders(orderId)`는 페이지당 1,000개, 최대 10페이지다. 각 체결 주문의 `myTrades(orderId)`는
   최대 1,000개를 요청하며, 상한에 도달하거나 누락·중복·잘못된 방향/ID/시각이 있으면 거부한다.
3. 기존 이력과 겹치는 주문은 client/order ID, side, fill 집계를 정확히 대조한다. 이미 복구된
   외부 주문은 원 요청 수량도 대조한다. 설명되지 않는 `bat-` 주문을 외부 매도로 처리하지 않는다.
4. 계좌 전체 open orders와 open order lists가 없음을 확인한다. 첫 계좌 조회 뒤 주문·fill 전체를
   다시 조회하고, 두 번째 계좌 조회의 ETH free/locked와 비교한다. 두 체결 조회와 ETH 잔고가
   각각 동일하고 stream이 준비됐으며 영구 차단이 없을 때만 후보를 검증한다.
5. 원본 Position의 복사본에 모든 외부 SELL을 시간순으로 적용한다. 모든 후보의 실제 매도량과
   수수료를 반영한 `Position.quantity + 기존 residual.quantity`가 REST ETH 잔고와 정확히 같아야 한다.
6. 전체 후보 검증 후 외부 SELL을 기존 Trade 저장·성과·알림 경로로 순서대로 기록한다.
   원 주문 ID를 멱등 key로 사용한다. 저장 후 중단되면 다음 startup이 저장된 prefix부터 복구한다.
7. 체결 결과를 stream에 rebase하고, 기존 잔여 장부 조정과 최종 startup gate를 완료한다.
   결과는 `NOT_STARTED`이며 자동매매 시작 명령을 발생시키지 않는다.

## 3. 자동 복구 범위와 거부 조건

지원 대상은 마지막 durable 체결 이후 생성·체결된, 열린 단일 앱 lot에 귀속 가능한 외부 SELL이다.
`FILLED` 또는 체결이 있는 `CANCELED`, `EXPIRED`, `EXPIRED_IN_MATCH`만 허용한다. 실제 `origQty`가
해당 시점의 앱 Position보다 크면, 부분 체결량만 맞더라도 거부한다.

| 상황 | 처리 |
| --- | --- |
| 검증된 전량 수동 매도 | 실제 SELL·손익을 저장하고 Position을 닫는다. |
| 검증된 부분 수동 매도 | 실제 체결분만 저장하고 남은 Position과 원가를 유지한다. |
| 매수 ETH 수수료 때문에 남은 sub-LOT_SIZE 잔량 | 아래 잔여 장부 조건을 만족하면 수량·원가를 별도로 보존한다. |
| 외부 BUY, 입출금 또는 다른 보유량으로 잔고 불일치 | 자동 귀속하지 않고 startup을 차단한다. |
| 미체결 주문/목록, 진행 중 부분 체결, 조회 중 증거 변경 | 기록 전 차단한다. |
| 불완전한 조회, 오래된 주문의 시간 겹침, legacy base-fee lot | 기존 검증 실패를 유지한다. |
| SELL에서 양수 ETH 수수료 발생 | 기존 Position의 base depletion 회계가 지원하지 않으므로 차단한다. |

BNB 수수료는 기존 체결 시점 평가 근거가 완전할 때만 사용한다. 현재 BNB 가격으로 과거 수수료를
추정하지 않는다. 복구 과정에서 주문 제출·취소·자동 청산을 호출하지 않는다.

## 4. Trade schema와 UI

외부 매도는 `schema_version=4`, `side=SELL`, `exit_reason=EXTERNAL_MANUAL`로 저장한다.
실제 주문 ID, client ID, `origQty`, fill ID·수량·가격·수수료·체결 시각과 fee 평가 근거를 보존한다.
앱의 판단 시세는 존재하지 않으므로 `market_price_at_decision=null`이다. 체결가로 대신 채우지 않는다.
v4 외부 복구 이유로 앱 `Order`를 만들 수 없다.

원가와 손익은 ADR-004의 average-cost 공식을 사용한다. 금융 계산은 `Decimal precision=34`이며
기존 BUY의 strategy/REGIME을 회계 귀속으로 유지한다. UI의 최근 주문과 전체 거래 이력에는
`외부 수동 매도`를 표시하고, CSV에는 원래 strategy와 `EXTERNAL_MANUAL`, 실제 fee 근거를 기록한다.
CSV의 판단 시세는 빈 field다. 기존 v1/v2/v3 row는 변경하지 않는다.

## 5. 잔여 장부

열린 lot에 매수 ETH 수수료가 있고, 외부 주문의 실제 요청량과 체결량이 같으며,
`executed_quantity == floor(quantity_before / LOT_SIZE.stepSize) * LOT_SIZE.stepSize`이면
매도 가능한 전량의 체결로 인정한다. 남은 수량이 양수이고 stepSize 미만일 때만 기존
`ResidualSettlement`가 history hash·수량·원가·stepSize를 durable 장부에 보존한다.

원래 `origQty`를 앱의 net Position 수량으로 바꾸지 않는다. 재시작은 저장된 이관 당시
stepSize와 history hash를 다시 검증한다. 부분 매도 잔량을 임의로 지우거나 이관하지 않는다.

## 6. 근거와 검증

- [Binance 공식 Account endpoints](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/account): `allOrders`의 orderId cursor와 `myTrades` 주문별 조회.
- [수정 검증 기록](../../Validation/External_Manual_Sell_Recovery_Fix_2026-09-10.md).
