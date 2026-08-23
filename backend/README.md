# Binance Auto Backend

`binance-auto-trader-backend`는 RegimeSTM과 TradingSTM, authoritative 시장·계좌,
주문·거래 이력, versioned loopback transport와 고정 Binance Spot Testnet adapter를
하나의 `binance_auto_trader` distribution으로 통합한 Python package입니다. 두 STM은
상태와 guard만 판정하고, 외부 효과는 typed action request로 반환합니다.

## 구성

- `domain/common/enums.py`: backend가 공유하는 canonical `RegimeType`/
  `Interval`
- `domain/market/`: Decimal OHLCV `Kline`과 versioned `MarketSnapshot`
- `domain/regime/`: 13개 `EA-*` transition의 4시간봉 REGIME 추천 STM
- `domain/trading/`: 109개 transition의 run-to-completion TradingSTM과
  free/locked balance를 보존하는 `Account`
- `domain/history/`: ADR-004 JSONL v1/v2 reader·v2 writer `Trade`, KST query `TradeHistory`,
  fee 포함 startup `Performance`
- `adapters/binance/`: Kline/account/order Gateway와 고정 Spot Testnet
  HMAC REST·combined stream·signed user-data WebSocket client, payload 정규화와 dedup
- `adapters/persistence/`: local JSONL streaming 복원, partial tail 복구와
  fsync pending-order sidecar
- `application/market_data_controller.py`: WS 먼저 구독, REST 조회,
  buffer 병합, snapshot 교체 순서 조정
- `application/trading_controller.py`: account·REGIME/session, serial Action 실행,
  same-ID 주문 reconciliation, Position/history 반영과 startup/reconnect 복구
- `application/trade_history_controller.py`: Repository → TradeHistory →
  Performance 초기 복원
- `bootstrap/`: Entity/Gateway/Controller 조립, 실행 mode, market → account →
  history/performance startup과 이중 opt-in Testnet 전용 composition
- `transport/`: `127.0.0.1` random-port HTTP/WebSocket, schema v2 DTO,
  인증·replay·full-resync 및 TypeScript contract 생성
- `tests/`: STM·market/account/history/order 회귀, architecture와 opt-in Testnet 검증
- `tests/architecture/`: package, enum, import 경계와 coding convention 검증

## 시장 데이터 초기화 계약

`MarketDataController.initialize_market_data()`는 `1m`, `30m`, `4h`, `1d`
Kline stream buffer를 REST 조회보다 먼저 시작합니다. 네 REST 응답을
모두 내부 `Kline`으로 정규화한 뒤 buffer를 배출하고, 같은
`(symbol, interval, open_time)`에서 WebSocket 값이 우선하도록 병합합니다.
네 주기 전체가 유효할 때만 `MarketSnapshot` state 참조를 한 번에
교체하며, 실패나 disconnect에서 기존 snapshot과 version을 유지합니다.
현재 ETH 가격은 snapshot 시각을 포함하는 최신 진행 4시간봉의
`close` 하나만 사용합니다.
초기화 중 disconnect나 payload 오류가 발생하면 현재 구독을 종료하고
해당 시도를 fail closed 처리합니다. 재연결 후 같은 Operation을 다시
호출하면 네 주기를 전체 재동기화하고 version을 성공 시에만 증가시킵니다.
초기 buffer는 terminal drain으로 세대를 동결하고 구독을 닫은 뒤
snapshot을 commit합니다. 지속 live market stream consumer는 후속 Phase 범위입니다.

Gateway는 네트워크 구현을 직접 선택하지 않고 주입된 client Protocol을 사용합니다.
기본 suite는 fake/in-memory client로 network 없이 실행하고, Testnet 전용 bootstrap만
고정 `testnet.binance.vision` REST·WebSocket client를 조립합니다. production adapter와
fixture 계약은 로컬 검증했지만 사용자 credential을 사용한 외부 parity는 아직 실행하지
않았습니다.

## 계좌와 거래 이력 초기 로드 계약

`TradingController.load_account()`는 Spot account REST 전체 snapshot을 먼저
`Account`에 적용하고, 이미 준비된 `MarketSnapshot`의 ETHUSDT 가격으로
free+locked ETH 평가금액을 계산한 뒤 User Data Stream을 시작합니다. Binance
`outboundAccountPosition`의 `B`는 변경 가능성이 있는 자산만 담는 absolute
partial patch이므로, 생략된 잔액은 유지합니다. source update time보다 오래된
event와 동일 payload 중복은 무시하되 같은 millisecond의 다른 patch는 수신 순서대로
적용합니다. malformed event, domain callback 실패 또는 공식
`eventStreamTerminated`를 받으면 해당 transport 구독을 닫고 fail closed 처리합니다.
Testnet은 legacy listen-key 대신 signed `userDataStream.subscribe.signature`를 사용합니다.
disconnect 뒤에는 주문 command를 잠그고 첫 REST snapshot → 새 signed stream → 두 번째
REST snapshot으로 사이 gap을 reconciliation한 뒤에만 다시 연결된 상태를 공개합니다.
account event는 receive loop에서 bounded 단일 FIFO worker로 전달되어 callback 순서를
보존합니다. enqueue부터 callback 완료와 queue drain까지 `account_ready=false`이고,
overflow·consumer/worker failure는 socket close와 reconciliation-required로 전환됩니다.
command/start/startup/reconnect와 주문 POST 직전 gate는 이 readiness를 요구하므로 blocked
application callback이 ping/close 수신을 막거나 stale 연결 상태로 주문을 허용하지 않습니다.

`TradeHistoryRepository.get_trade_history()`는 UTF-8/LF JSONL을 한 줄씩 읽고
Decimal string과 UTC timestamp를 canonical `Trade`로 복원합니다. 파일 없음과
0-byte 파일만 빈 이력으로 취급하며, permission·완결 record 손상은 그대로
실패합니다. JSON parsing에 실패한 non-LF 마지막 tail만 별도 corrupt 파일에
보존하고 backup file과 parent directory를 fsync한 뒤 마지막 정상 LF까지
truncate합니다. directory fsync 실패 시 원본을 유지합니다. 이 startup 복구 구간은
bootstrap process 하나가 history 경로를 독점하고 다른 writer가 없다는 계약입니다.
`TradeHistoryController`는 같은 거래 tuple로 `TradeHistory`와 ADR-004 `Performance`를
모두 만든 뒤 원자적으로 공개합니다. repository는 JSONL v1/v2를 읽고 신규 record는
실제 자산 흐름을 보존하는 v2로 씁니다. v2 BUY의 base-asset fee는 취득 수량에서
차감하며 quote cost에 다시 더하지 않습니다. `fee_quote_amount`는 USDT fee면 원래
금액과 같고, ETH fee면 execution 시 fill별 가격으로 환산한 합계입니다. 여러 fill의
maker/taker 요율이 다를 수 있으므로 ETH 총 fee에 평균 체결가를 다시 곱하지 않습니다.
제3 fee asset은 임의 시세 없이 `FEE_ASSET_CONVERSION_REQUIRED`, 열린 v1 ETH-fee lot은
`HISTORY_ACCOUNTING_MIGRATION_REQUIRED`로 주문을 fail closed합니다.

## 실행 계약

1. Controller가 `TradingContextView`를 만든 뒤 event를 `SerialEventQueue`에 넣습니다.
2. `RunToCompletionEventProcessor`가 한 event에 대해 `TradingSTM.handle()`을 정확히 한 번 호출합니다.
3. action batch 전체를 순서대로 실행한 뒤에만 `QueueEvent`와 주문 결과 event를 내부 우선순위로 넣습니다.
4. 다음 microstep이 시작될 때 새 Context snapshot을 만들기 때문에, 병렬 Region의 상태와 주문 pending 값이 서로 어긋난 상태로 다음 시장 event를 처리하지 않습니다.
5. `ScheduleReevaluation`은 즉시 다시 enqueue하지 않습니다. 시장 값 변경, candle close, deadline 또는 retry/backoff가 실제로 도래했을 때 Controller scheduler가 event를 생성해야 합니다.

여러 Region을 같은 시장 값으로 함께 평가할 때는 `MARKET_DATA_UPDATED`를 전달합니다. STM이 이를 활성 Region별 `RETRY_*` event로 내부 정규화해 한 microstep에서 후보를 모으므로, Case B와 Case C가 동시에 매수 가능해도 Case C 주문 하나만 반환됩니다.

STM이나 action executor에서 `handle()`/`process_next()`를 재귀 호출하면 예외가 발생합니다.

## 최소 사용 예

```python
from binance_auto_trader.domain.regime import RegimeSTM
from binance_auto_trader.domain.trading import RegimeType, TradingSTM

regime_stm = RegimeSTM()
trading_stm = TradingSTM.get_stm_instance(RegimeType.TYPE_0)
```

Phase 6의 불변 coverage registry에서 `TYPE_0`은 `SUPPORTED`, `LOWER_BB`
정확히 109개 transition, start Guard `READY`, 상단 BB 정책
`SAFE_TERMINATION`입니다. `TYPE_1`~`TYPE_4`는 registry가 없는
`UNSUPPORTED`이며 `UNSUPPORTED_TRADING_LOGIC`으로 거부합니다. 인자를 생략해
`TYPE_0`을 기본으로 선택하거나 미지원 REGIME을 lower-BB로 대체하지
않습니다.

상단 BB 안전 종료에서는 pending 주문을 포지션보다 우선해 취소하고
같은 ID를 `stop_after_reconciliation = True`로 조정합니다. pending이 없고
포지션이 있으면 기존 STOPPING/force-sell 계약을 재사용하고, 둘 다
없으면 lower event·Case Context를 정리한 뒤 runtime을 즉시 종료합니다.
이는 새 상단 매매 전략이 아닙니다.

실제 주문 adapter는 `SubmitOrder` intent와 session namespace로 36자 이하 `bat-`
client order ID를 만들며, 한 session의 같은 intent/attempt에서는 같은 ID를 사용하고
다른 process session과는 충돌하지 않게 합니다. `NEW`, `PARTIALLY_FILLED`, `UNKNOWN`,
accepted-response timeout과 Binance `-2010` duplicate 응답에서는 새 주문을 제출하지
않고 `ReconcileOrder` 계약으로 같은 ID를 조회합니다.

MARKET 제출 전 symbol의 `TRADING` 상태, Spot·`MARKET` 허용 여부, base precision,
`LOT_SIZE`·`MARKET_LOT_SIZE` 수량 규칙과 `MIN_NOTIONAL`·`NOTIONAL`의 MARKET 적용
flag를 Decimal로 검사합니다. price/stopPrice가 없는 MARKET 주문에 `PRICE_FILTER`를
로컬 적용하지 않습니다. `BINANCE_TESTNET_MAX_NOTIONAL`도 decision price × 준비 수량의
로컬 사전 상한일 뿐입니다. 실제 체결 금액과 거래소 average/reference 가격 filter가
가격 변동·slippage를 포함한 최종 권위이므로 이를 예산의 하드 상한으로 사용하면 안 됩니다.

주문을 준비한 뒤 exact order를 fsync한 `PREPARED` sidecar에 기록하고, account stream
연결을 POST 직전에 다시 확인합니다. disconnect면 POST하지 않고 sidecar를 남겨 startup
reconciliation으로 넘깁니다. 재시작은 기록된 client ID를 먼저 조회하며 반복된 `-2013`
만으로 record를 삭제하거나 새 주문을 제출하지 않습니다. open/recent result 또는 exact
history execution으로 identity를 확정한 뒤에만 pending record를 정리합니다.

Testnet reset 뒤 숫자 `orderId`가 재사용돼도 그 값만으로 identity를 인정하지 않습니다.
`(clientOrderId, orderId)` pair와 terminal status, 누적 수량·금액, 평균가, 수수료·수수료
자산, 마지막 체결 시각이 durable Trade와 정확히 같아야 합니다. 숫자 ID 충돌이나 execution
summary 불일치는 Position delta 적용과 sidecar 제거 전에 reconciliation-required로 막습니다.

pending sidecar schema v2는 `PREPARED` UPSERT와
`SUBMISSION_REJECTED_CONFIRMED` TRANSITION을 각각 file+directory fsync하며 legacy v1
UPSERT는 `PREPARED`로 읽습니다. typed `-1013`/재동기화 뒤의 `-1021`/`-1022` 거부는 후속 query 예약 전에
durable 전이를 기록합니다. 재시작은 그 전이와 네 번의 exact
`-2013`/`ORDER_NOT_VISIBLE`를 모두 확인한 경우에만 journal을 제거하고 READY로 복귀하며,
일반 `PREPARED`에는 이 예외를 적용하지 않습니다.
history commit과 sidecar REMOVE는 별도 marker로 추적합니다. history 저장 뒤 REMOVE fsync가
실패하면 reconnect가 실제 sidecar를 재조회·제거할 때까지 public command gate를 닫습니다.
signed subscription의 비정상 ACK 오류는 검증된 정수 code만 외부로 내보내며, 문자열·객체
값과 request payload를 예외에 반사하지 않아 credential이 로그에 섞이지 않게 합니다.

## Application startup과 loopback read 계약

`create_application_runtime()`은 하나의 `RLock` 아래 기존 MarketSnapshot,
Account, RegimeSTM과 Controller/Gateway identity를 조립합니다.
`start_application()`은 market 초기화와 REGIME 추천 준비를 검증한 뒤 account REST
commit·stream 시작, history/performance 복원을 순서대로 실행합니다. 어느 단계든
실패하면 ready를 공개하지 않고 stage/code가 있는 `ApplicationStartupError`로 닫으며,
이미 열린 account subscription을 정리합니다. 실행 mode는
`disabled | fake | testnet | live`이고 누락·unknown 값의 기본은 `disabled`입니다.

`run_transport_process()`는 session token을 argv, URL, environment 또는 log가 아닌
inherited anonymous FD에서 한 번 읽습니다. 같은 factory 호출에서 event stream observer,
runtime startup, ready 사후조건, `127.0.0.1` random-port server와 cleanup을 조립합니다.
HTTP는 Bearer token, canonical request ID, exact Host/Origin과 command idempotency를
검증하고, WebSocket은 2초 이내 첫 `AUTHENTICATE` frame 뒤 bounded replay를 제공합니다.
snapshot과 `last_sequence`는 application lock 아래 원자적으로 읽습니다.

현재 snapshot은 ETHUSDT/USDT, REGIME 추천/선택, 다섯 REGIME의
`support_status`/`start_guard`와 trading session 상태를 제공합니다. 미지원 REGIME은
추천·표시·선택할 수 있지만 start는 차단됩니다. 검증된 in-process fake composition과
주문 opt-in Testnet composition만 연결 상태에서 command를 허용합니다. `disabled`,
read-only Testnet과 `live`는 fail closed이고, account stream disconnect 즉시 주문
command를 잠급니다. generic factory로 fake/Testnet 주문 권한을 우회할 수 없으며
packaged Tauri sidecar lifecycle은 Phase 12 범위입니다.

## 테스트

기본 suite는 Testnet flag를 끄고 표준 `unittest`로 실행합니다.

```bash
cd backend
BINANCE_RUN_TESTNET=0 BINANCE_RUN_TESTNET_ORDERS=0 \
  PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py' -v
```

deterministic accepted-timeout·partial/disconnect fault는 실제 Binance network 없이
in-memory transport로 2/2 통과했습니다. `tests/testnet/test_binance_testnet_read_only.py`
와 `test_binance_testnet_order_lifecycle.py`는 credential과 명시적 opt-in 없이는
자동 skip됩니다. lifecycle harness는 production Testnet runtime을 사용하지만 BUY
trigger만 테스트 전용 private action seam이므로 market-event→strategy E2E 증거는
아닙니다. 실제 주문 run의 history/pending artifact는 ignored `.testnet-artifacts/`의
run별 directory에 남고, 실패에는 credential 없는 path와 client ID가 표시됩니다. 불명
상태에서 이 artifact를 삭제하거나 새 lifecycle을 실행하지 마십시오. submit 거부 →
query 전 crash → restart의 네 번 exact absence, 재제출 0회와
safe journal cleanup은 local integration test로 검증했습니다. 사용자 credential이 없어
authenticated read-only, capped BUY/force-sell과
open-order/position 실제 restart scenario는 아직 실행하지 않았으며 Phase 9 master
완료 조건도 열어 둡니다.

Trade History details와 CSV export는 Phase 10~11, packaged Tauri sidecar는 Phase 12,
market-event→strategy E2E와 live readiness는 Phase 13 범위입니다. domain package에는
transport/network/file 의존성이 없습니다.
