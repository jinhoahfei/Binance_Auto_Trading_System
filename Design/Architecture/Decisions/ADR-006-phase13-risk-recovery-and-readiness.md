# ADR-006 — Phase 13 위험, 장애 복구와 live readiness

| 항목 | 값 |
|---|---|
| 상태 | Accepted; readiness evidence gaps remain |
| 결정일 | 2026-08-24 |
| 최종 검토일 | 2026-08-31 |
| 적용 범위 | P13-01~P13-08 |

## 1. 목적과 범위 방어

Phase 13은 Testnet과 secret 없는 deterministic fault harness에서 장애 복구와 전체 E2E를
검증한다. 이 ADR을 구현하거나 Phase 13 master를 완료하는 것은 live 주문 승인이 아니다.
`live` endpoint, credential과 주문 gate는 별도의 사용자 승인 record가 생기기 전까지 backend,
transport와 UI에서 계속 비활성이다.

## 2. Versioned risk policy

`RiskPolicy`는 다음 field를 명시적으로 가져야 `CONFIGURED`다.

- 1 이상의 정수 policy version
- 0보다 큰 유한 `Decimal` 또는 명시적 `None`인 단건 주문 notional 상한
- 0보다 큰 유한 `Decimal` 또는 명시적 `None`인 누적 position notional 상한
- 0보다 큰 유한 `Decimal` 또는 명시적 `None`인 KST daily loss 상한
- `REALIZED_ONLY` 또는 `REALIZED_AND_UNREALIZED` daily loss 범위
- `BLOCK_NEW_ORDERS` 또는 `CANCEL_AND_LIQUIDATE` manual kill 동작

2026-08-29 사용자는 세 상한을 모두 `None`, daily loss 범위를
`REALIZED_ONLY`, manual kill 동작을 `CANCEL_AND_LIQUIDATE`로 확정했다.
Configured policy의 `None`은 **명시적 무제한**이며 policy 문서·주입 자체가 없는
`UNAVAILABLE`과 다르다. Wire의 explicit `null`은 configured-unbounded로 strict
변환하고, field 누락·parse 실패·policy 미주입은 `UNAVAILABLE`로 fail closed한다.
이 결정을 거대한 Decimal 상수로 대체하지 않는다.

Phase 9의 `10 USDT`는 이미 완료한 Testnet BUY cap이며 policy 상한이 아니다.
Phase 13 actual Testnet Case 2는 local gate 통과 후 고정 Spot Testnet `ETHUSDT`에서만
별도 opt-in하고, 각 신규 BUY decision notional을 `100 USDT` 이하로 제한한다.
이 outer execution cap은 nullable policy와 독립적이며, recovery SELL은 authoritative Position과
free ETH·최신 filter 안에서 정확한 보유 수량을 청산할 때 BUY cap을 적용하지 않는다.
Fake 전용 policy는 network mutation이 불가능한 deterministic test fixture에서만 사용한다.

## 3. Risk 계산과 gate 순서

최종 BUY gate의 owner는 `TradingController.evaluateBuyRisk(order)`다. exchange symbol filter가
실제 `submittedQuantity`를 확정한 뒤, pending journal UPSERT와 REST POST 전에 application RLock
아래에서 다음 순서로 평가한다.

1. policy availability와 session/pending `policyVersion` 일치
2. manual kill 상태
3. 단건 decision notional
4. KST daily loss
5. 현재 Position + active reservation + 후보 주문의 projected position notional

현재 Position notional은 authoritative Position 수량과 같은 `MarketSnapshot` version의 현재가로
계산한다. active/partial/UNKNOWN BUY는 미체결 수량에 decision price를 곱한 금액을 예약하고,
실제 fill은 Position notional로 이동한다. confirmed zero-fill terminal, durable history와 pending
REMOVE가 완료된 부분만 예약을 해제한다. KST daily realized PnL은 durable SELL Trade만 합산한다.
`REALIZED_AND_UNREALIZED`는 그 값에 현재 Position의 mark-to-market PnL을 합친 뒤 음수 부분의
절댓값을 daily loss로 사용한다. 추정 fee나 UI/JavaScript number는 계산에 사용하지 않는다.

Risk 차단은 신규 노출 BUY에만 적용한다. 일반/STOP/recovery SELL, cancel, same-ID query,
reconciliation, pending/history persistence retry, read-only 조회와 안전 종료는 계속 허용한다.
manual kill은 어떤 policy 상태에서도 즉시 신규 BUY를 막는다. 유한 상한은 기존
차단 순서와 equality 경계를 적용하고, `None`인 상한의 비교만 건너뛰되 후보
주문·현재·예약·예상 Position notional과 KST 실현 손실 계산·게시는 계속한다.

`CANCEL_AND_LIQUIDATE` 활성화 command는 kill receipt와 control version을 먼저 fsync한
뒤 app-owned pending/UNKNOWN/open 주문을 same-ID query와 signed stream으로 확정한다.
취소 가능한 주문만 cancel하고 terminal/partial을 reconcile한 뒤, 잔여 Position이 있으면
기존 STOP/recovery path로 정확한 수량을 안전 청산한다. Open app order, pending/UNKNOWN과
Position이 모두 0으로 authoritative하게 확인되기 전에는 완료를 게시하지 않는다.

현재 Controller는 이 순서를 구현했다. Activation receipt를 fsync한 뒤 durable
app-owned pending을 same-ID query → 개별 cancel → same-ID terminal requery로 확정하고,
partial fill을 History·Position에 먼저 반영한 뒤 canonical STOP/recovery SELL을 실행한다.
Restart reconciliation은 신규 strategy `run()` 없이 control version에서 재구성한 같은 cleanup
identity로 자동 재개한다. 활성 epoch 중 policy hot-swap과 active no-op은 최초
activation의 behavior·policy version provenance를 바꾸지 못하며, exact activation replay는
cancel 또는 SELL effect를 중복 생성하지 않는다.

Reconnect 뒤에도 같은 app order가 취소 가능한 상태이면 같은 ID로 다시 cancel하고 terminal을
재조회한다. 이 사이 발생한 partial fill은 먼저 durable reconcile한 뒤 residual Position만 한 번의
STOP/recovery SELL로 줄인다. Cleanup boolean이 false인 process는 shutdown durability ACK를
거부하며, release 시도마다 app-owned open order, pending/UNKNOWN, memory/Context pending과
Position을 fresh하게 다시 검증한다. 과거 한 번의 zero snapshot은 release 근거로 재사용하지 않는다.

Transport는 manual-kill activation receipt가 durable하지만 cleanup 중이면 HTTP `202`, 모든
authoritative zero 조건이 확인되면 `200`을 반환한다. Activation epoch의 고정 behavior·policy
version은 현재 hot-swapped policy와 별도 field로 게시한다. `RiskBudgetSnapshot` wire는 세 상한이
configured `null`이어도 current/reserved/projected position exposure, candidate notional과 KST
realized PnL을 계속 전달하며 UI가 무제한을 unknown 또는 계산 부재로 표시하지 않게 한다.

`manual_kill_cleanup_complete`는 account stream readiness, REST app-owned open order 0, durable
pending/UNKNOWN 0, unresolved memory order 0, Context pending 0과 Position 0이 모두 맞을 때만
`True`를 게시한다. 조회·journal·history 불확실성은 cleanup을 완료로 완화하지 않고
`RECONCILIATION_REQUIRED`로 유지한다. Receipt-first 순서, partial 반영·잔량 청산,
restart no-run resume, activation replay와 active-epoch provenance 변조 거부는 focused local
integration·restart·repository test를 통과했다. 실제 Phase 13 Testnet public-path trace는 아직
evidence GAP이다. Local suite 밖의 추가 actual timeout/5xx/persistence 주문은 자동 완료 조건이
아니며, 필요할 경우 예상 주문 수와 최대 노출에 대한 별도 사용자 승인을 받는다.

### 3.1 Binance Spot 공식 계약 확인

2026-08-31 현재 cleanup, recovery SELL과 Phase 13 public Testnet preflight 경계는 다음 Binance
공식 문서와 다시 대조했다.

- [Spot trading endpoints](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/trade)은
  `DELETE /api/v3/order` 취소와 `orderId` 또는 `origClientOrderId` 식별자를 정의한다.
- [Spot account endpoints](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/account-endpoints)은
  `GET /api/v3/order` same-order 조회와 주문 상태를 정의한다.
- [Spot User Data Stream](https://developers.binance.com/en/docs/products/spot/user-data-stream)은
  주문 갱신을 `executionReport` event로 전달한다. Stream event가 없거나 결과가 불명확하면
  [Spot REST 일반 규칙](https://developers.binance.com/en/docs/products/spot/rest-api)에 따라
  API로 상태를 조회한다.
- [Spot symbol filters](https://developers.binance.com/en/docs/products/spot/filters)은
  `LOT_SIZE`, `MARKET_LOT_SIZE`, `MIN_NOTIONAL`/`NOTIONAL`의 quantity·notional 경계를 정의한다.
  현재 계약은 이 MARKET `MIN_NOTIONAL`/`NOTIONAL` notional에 non-null reference price를 우선
  사용한다. 공식 `MAX_ASSET` 정의는 base asset에는 quantity, quote asset에는 notional을 적용한다고만
  명시하며 quantity 기반 MARKET 주문의 quote notional 환산 가격식은 제공하지 않는다. 따라서
  Phase 13은 base `MAX_ASSET`만 제출 quantity로 평가하고 quote `MAX_ASSET`이 반환되면
  `referencePrice * quantity`를 합성하지 않은 채 고정 fail closed한다. Recovery SELL도 submit 직전
  최신 `exchangeInfo` filter로 exact holdings를 정규화하며 같은 차단 계약을 적용한다.
- [Spot REST `myFilters`](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#query-relevant-filters-user_data)는
  특정 account와 symbol에 관련된 exchange/symbol/asset filter 세 scope를 반환한다.
  Phase 13 preflight와 각 prepare는 이 signed read를 별도로 수행하고 요청 symbol context에
  immutable composite DTO를 결속한다. 공식 type별 exact field·JSON type과 scope를 검증하며,
  malformed/unknown/duplicate/extra field와 평가식이 공개되지 않은 `T_PLUS_SELL`을
  추정하거나 무시하지 않는다. Quantity 기반 MARKET에서는 base `MAX_ASSET`만
  quantity로 평가하며 quote filter는 journal/POST 전에 차단한다.
- [Current open orders](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#current-open-orders-user_data)와
  [current open order lists](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#query-open-order-lists-user_data)는
  account count filter의 완전한 현재 상태를 증명하는 signed read다. `openOrders`는 symbol을 생략해
  exchange-wide로 조회하고 `openOrderList`는 전용 endpoint를 사용한다. Preflight와
  submit-time에 두 exact empty snapshot과 각각의 server-aligned 관찰 시각을 보존하며,
  non-empty/malformed에서 order/list identity를 오류나 trace로 내보내지 않고 차단한다.
- [Spot REST reference price](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#query-reference-price)는
  symbol, reference price와 exchange timestamp를 반환한다. 이번 좁은 actual target은 null 또는
  `-2043`에서 VWAP·last price fallback을 합성하지 않고 신규 BUY를 닫는다. 이 값은 MARKET
  `MIN_NOTIONAL`/`NOTIONAL` 검증용이며 quote `MAX_ASSET` 환산 근거로 사용하지 않는다.
- Prepared composite evidence는 account filter, public rule, 두 account-wide empty-state와 reference
  price 중 가장 이른 관찰부터 고정 `30초`까지만 유효하다. 정확히 30초는 허용하지만 30초+1ms와
  clock regression은 차단한다. Fingerprint는 submit에서 한 번 소모하고, 실제 transport의 socket I/O
  직전 `before_send` guard가 같은 server-aligned 시각으로 freshness를 다시 검증한 뒤에만 최초
  submission-attempt evidence를 만든다. Trace v3는 signed composite와 별도의
  `public_relevant_filters` projection을 보존하고 quantity·notional·count·passive·`MAX_POSITION`
  overlap, public scalar binding과 같은 30초 인과를 runtime 규칙으로 재검증한다. Preserved v2는
  schema version을 바꾸지 않고 그 계약으로만 계속 검증한다.
- [Spot Testnet REST](https://developers.binance.com/en/docs/products/spot/testnet/rest-api)는
  실제 Phase 13 증거를 고정 Spot Testnet endpoint에서 수집할 때 같은 query/cancel 계약을
  검증할 기준이다.
- [Spot Testnet WebSocket API](https://developers.binance.com/en/docs/products/spot/testnet/web-socket-api)와
  [market stream](https://developers.binance.com/en/docs/products/spot/testnet/web-socket-streams)은
  signed account subscription과 public Kline stream의 고정 Testnet 연결·event 계약을 검증할
  기준이다.

공식 문서가 REST 결과를 application의 최종 reconciliation owner라고 표현하는 것은 아니다.
Stream과 REST 사실을 durable History·Position에 결합하고 same-ID terminal requery를 요구하는
부분은 위 exchange 계약을 fail-closed하게 적용한 이 ADR의 설계 결정이다.

Manual kill의 active 상태와 단조 control version, 최근 1,024개 이하의 성공 command ID·payload,
결과 및 behavior·policy version provenance는 history/pending과 분리한 strict JSONL journal에
보존한다. Schema v2는 toggle뿐 아니라 no-op receipt도 HTTP 성공 전에 append한 뒤 file과 parent
directory를 fsync하고, 성공 뒤에만 in-memory 결과를 게시한다. Startup은 v1 toggle을 호환
replay하되 v2의 expected/result version, ID/payload와 결과를 검증하고, 최근 receipt 전부를 일반
UI command eviction과 독립된 bounded command cache에 복원한다. 모든 append는
`O_NOFOLLOW|O_APPEND`로 연 same-inode single-link regular file에서 disk replay와 cached
state/receipt, size·mtime·ctime을 결합하고 write 뒤 같은 descriptor를 다시 strict replay한다.
따라서 실행 중 empty·unlink·valid-prefix rollback, path 교체, 중복 key·비표준 숫자·부분 파일·추가
field·비연속 version과 ID의 다른 payload 재사용은 fail closed한다. 저장 결과가 불명확한 process는
activation 요청이면 메모리 kill도 즉시 켠 뒤 ownership·stream reconciliation gate를 닫으며, 추가
control command는 fresh restart 전까지 허용하지 않는다. Cached receipt가 있는 shutdown은 path
부재도 strict replay 대상으로 취급해 durability ACK와 정상 종료를 거부한다.

## 4. Durable intent와 crash cut point

한 intent는 process 재시작을 넘어 하나의 durable identity와 총 5회 제출 예산을 가진다.
sidecar journal은 append-only/fsync로 `PREPARED -> SUBMITTED -> PARTIAL|UNKNOWN|TERMINAL ->
HISTORY_COMMITTED -> REMOVE`를 기록한다. 공식 pre-matching zero-fill rejection만
`SUBMISSION_REJECTED_CONFIRMED`로 전이할 수 있다. timeout, 5xx, 429와 response decode failure는
주문 실패로 단정하지 않고 `UNKNOWN`으로 기록해 같은 client order ID만 조회한다.

Phase 13 actual public Case 2 target은 위 일반 예산보다 더 좁은 예외 계약을 사용한다. Controller의
intent 예산은 `1`, permission proxy의 process-local permit은 `ETHUSDT CASE_C` 최초 BUY 한 번과
서로 다른 client order ID의 exact STOP SELL 한 번뿐이다. Permit은 delegate 진입 전에 소비하고
delegate 예외나 `UNKNOWN`에서도 복구하지 않는다. 두 번째 permit을 소비하는 순간 후속 제출을
영구 차단하며 cancel mutation도 delegate 전에 거부한다. REST adapter는 이 target에서 주문
timestamp 오류를 내부 재전송하지 않아 한 logical order에 wire `POST /api/v3/order`가 두 번
발생하지 않는다. Legacy Phase 9 target과 broad discovery는 세 번째 public-case opt-in이 켜지면
상호 배타적으로 skip한다.

Schema v3은 `SUBMITTED` file·directory fsync을 REST POST보다 먼저 완료하고, 전이 실패
시 POST를 시작하지 않는 writer 계약을 가진다. 따라서 replay된 v3 `PREPARED`는 POST
미시작 근거이며 1·2·4·8초 same-ID 조회가 모두 exact absence일 때만 REMOVE한다.
Legacy v1/v2 `PREPARED`는 POST 수락 직후 crash를 배제할 수 없어 계속 fail closed하고,
후속 v3 UPSERT로 provenance를 세탁하지 않는다. REMOVE 후에도 최초 UPSERT의
intent·attempt·schema와 REMOVE line이 남아 총 제출 예산 audit을 복원한다.

각 attempt와 policy version은 POST 전에 durable 예약한다. restart는 journal에서 intent별 최대
attempt와 reservation을 복원한다. legacy record 또는 다른 policy version의 active record는 먼저
same-ID reconciliation하며 신규 BUY는 `RISK_POLICY_VERSION_MISMATCH`로 차단한다. partial fill은
새 fill key만 Position과 Trade 후보에 적용하며 Trade와 pending REMOVE가 모두 durable해지기
전에는 성공 publication이나 새 주문을 허용하지 않는다.

## 5. Process ownership와 orphan 정책

Process ownership evidence는 하나의 파일에 모든 필드를 복제하지 않고 소유자별로 분리한다.
Tauri native lifecycle은 직접 spawn한 PyInstaller launcher child handle과 launcher PID를 소유한다.
Python이 FD4로 보내는 strict READY는 loopback port, session ID, actual Python `runtime_pid`와
canonical `process_start_id`를 전달하고, native lifecycle은 launcher identity와 Python identity를 구분해
같이 보존한다. Python app-data의 `.backend-runtime.lock`은
`schema_version`, `runtime_pid`, `process_start_id`, `owner_state`만 담는 non-secret exact artifact이며
OS advisory single-instance lock을 runtime lifetime 동안 유지한다. Port와 parent PID는 이 durable
artifact에 기록하지 않아 stale network endpoint나 변하는 조상 PID를 다음 launch의 신뢰 근거로
삼지 않는다.

Artifact state는 시작 중 `ACTIVE`, parent control FD5 EOF 후 `ORPHANED`, 정상 종료와
durable flush 후 `RELEASED`다. Parent control channel이 끊기면 backend는 신규 BUY gate를 즉시
닫고 artifact를 `ORPHANED`로 fsync한 뒤 listener, child와 lock을 유지한다. 자동 process kill,
자동 cancel, 자동 liquidation과 새 order submit은 하지 않는다.

별도 주기 timer의 heartbeat timeout으로 parent 생존을 추측하지 않는다. Native parent가 독점
소유한 FD5 writer는 process 종료 시 kernel이 닫으므로 Python reader의 EOF를 liveness 신호로
사용한다. EOF를 읽은 같은 waiter iteration에 ownership gate와 `ORPHANED` fsync를 적용하며,
실패한 durable 장벽만 listener를 유지한 채 반복한다.

새 Python runtime은 OS lock이 비어 있어도 기존 artifact가 exact `RELEASED`가 아니면 임의로
덮어쓰지 않고 startup을 fail closed한다. Tauri native startup은 credential 조회와 Python spawn 전에
artifact를 `O_NOFOLLOW`로 열어 owner, regular-file, single-link, 0600, bounded exact schema와 exclusive
nonblocking lock을 검증한다. `ACTIVE`/`ORPHANED`이면서 recorded PID가 확정 부재한 경우에만 exact
state, runtime PID와 `process_start_id`를 native operator dialog에 표시한다.

운영자가 명시적으로 해제·재시작을 확인하면 native owner는 dialog 이후 lock을 다시 획득하고 같은
device/inode, schema, runtime identity와 state 및 PID 부재를 재검증한다. 모두 일치할 때만 같은 inode의
state를 `RELEASED`로 바꾸고 `fsync`한 뒤 애플리케이션을 재시작한다. 취소, live/permission-ambiguous
PID, lock 경합, invalid artifact와 확인 이후 identity 변경은 기록을 바꾸지 않고 fail closed한다. 이
workflow는 process kill, order cancel, liquidation 또는 새 order submit을 수행하지 않는다.

## 6. 지속 market event와 deterministic evidence

초기 REST/WS merge 뒤 Gateway는 같은 generation의 buffer를 live observer로 원자 전환한다.
30분 지표는 period `9`, 첫 9개 시간순 확정봉 종가의 SMA seed, alpha `0.2`, 최근
EMA9 `6`개의 `x=0..5` OLS, Decimal 유효숫자 `34`, threshold 비교 전 최종
8자리 `ROUND_HALF_EVEN`으로 확정했다. 진행 30분봉의 realtime price,
Case C `tp_price`와 확정 1분봉 `close_1m`은 각각 직전 확정 EMA에서 독립
후보 EMA를 계산하며 확정 시계열에 commit하지 않는다. Raw OLS slope는
`raw_ols_slope / candidate_price * 100`으로 정규화하고 단위는 `%/30분봉`이다.
Actual close, realtime, Case C `tp_price`, 확정 1분봉 `close_1m`은 각각 해당 계산에
실제로 대입한 가격을 동일 계산의 분모로 사용한다.

MarketDataController는 versioned MarketSnapshot과 공식 Kline 시간에서
`MarketEvaluationSnapshot`을 만드는 concrete builder를 소유하고 public
`observeMarketEvaluation` Operation만 호출한다. Shared Decimal helper, production bootstrap
observer wiring, monotonic duration tracker의 reset/rebase와 immutable evaluation/version queue
preparer는 구현했다. Concrete builder는 기존 `MarketEvaluationBuilder` seam의 application
구현 세부사항이며 신규 business lifeline 또는 package public API가 아니다. Architecture audit에는
포함한다. Production builder golden·seed·threshold·HALF_EVEN·non-accumulation·actual close·1분
결합·reset/rebase 직접 회귀 20개와 연속 market-version queue provenance를 통과했다. 4H 경계
12개와 UTC 자정 180개 유효 arrival permutation도 한 atomic version과 같은-version REGIME 평가를
검증했다. Public `observeKline`에서 시작하는 local immediate/partial/UNKNOWN/failure/SELL/STOP과
SELL-intent 변경 방어의 일곱 흐름, production Spot REST memory-HTTP E2E를 private Action seam 없이
통과했다.

Case C SELL은 최초 판단의 `realtime_pct_b`를 pending runtime과 immutable order intent에 함께
고정하고 pending-order sidecar v4에도 저장한다. 따라서 partial, UNKNOWN, same-ID reconciliation
사이에 새 market evaluation이 도착해도 terminal `case_c_exit_pct_b`와 PC-27/PC-28 인계는 현재
시장값이 아니라 최초 SELL intent 값을 사용한다. Sidecar v1~v3는 필드 부재를 명시적으로 허용해
backward replay하되, 새 주문의 provenance와 혼합하지 않는다. 일반 Case C SELL은 최초 reason과
finite `%B`가 runtime intent와 정확히 같은지 trace·journal·REST 전에 검증한다. REMOVE 이후에도
intent별 최초 `%B` audit을 보존해 동일 intent 재시도나 journal replay에서 값이 달라지면 fail closed한다.

Public market event에서 주문 decision price는 queue에서 claim된 immutable
`MarketEvaluationSnapshot.realtime_price`다. `MarketSnapshot`의 4H current price 재조회는
evaluation이 없는 legacy/direct 경로의 fallback에만 허용하며 public event 가격을 덮어쓰지 않는다.
Testnet/E2E harness가 허용받는 seam은 외부 market/account event, clock과 fault 제어뿐이며
`TradingController._execute_action` 같은 private Action 경계를 직접 호출한 성공은 완료 증거가
아니다.

Production `MarketDataController`와 `TradingController` 사이에는 test-only 합성 trace가 아닌
bounded observer가 있다. 한 evaluation의 실제 `1L.1 KLINE 관찰 -> 1L.2 전략 평가 -> 1L.3
SubmitOrder effect 직전`을 같은 immutable evaluation/version과 command identity로 기록하고,
retention 한계에서는 evaluation 단위 전체만 제거한다. 기대한 market chain이 없거나 이미 제거된
경우 주문 effect 전에 fail closed한다. 주문 trace는 BUY/SELL별 고정 prefix, 최대 네 번의 same-ID
query branch, 최소 한 번의 fill 적용과 durable suffix를 exact grammar로 검증한다. Exchange order
ID는 처음 관찰된 뒤 `None`으로 후퇴할 수 없고 terminal fill·durable suffix는 해당 terminal
`OrderResult.exchange_order_id`와 정확히 같아야 한다. 연속 entry의
`context_version_before`는 직전 `context_version_after` 이상이어야 하므로 겹치는 version 후퇴도
성공 trace에 포함할 수 없다.

Actual 실행 중 예외가 durable BUY 뒤 발생하면 failure finalizer는 제출을 닫기 전에 단 한 번만
recovery 가능성을 판정한다. 이번 run의 durable Trade가 정확히 BUY 한 건이고, guard가 BUY permit
한 건만 소비했으며, pending/UNKNOWN·reconciliation이 없고, authoritative Position이 BUY 체결량과
같으며, account stream에서 effective free ETH가 같은 수량 이상임을 확인한 경우에만 public STOP을
발행해 terminal SELL과 zero Position을 기다린다. Account 확인 중 guard·Position·pending 상태가
바뀌면 SELL을 만들지 않는다. 모호한 상태, 이미 소비된 SELL permit 또는 `UNKNOWN`에서는 중복
mutation보다 관찰 가능한 exposure 보존을 우선한다. 그 뒤 성공·skip·실패와 무관하게 제출 permit과
scheduler를 닫고 primary runtime을 종료한 뒤 fresh read-only snapshot을 수집한다. 원 예외의 type,
message와 traceback은 bare re-raise로 보존하고, 별도 `FAILED` artifact에는 stable typed reason과
recovery outcome만 기록한다. Guard의 BUY/SELL client ID는 각각 durable Trade client ID와 정확히
결속한다. `SUCCESS/NOT_REQUIRED`는 first runtime과 canonical timestamp·모든 필수 field가 concrete한
fresh runtime의 Position/pending/reconciliation/open-order zero 사실에 교차 결속하며, 최종 확인이
불완전하면 각각 `FAILED/SKIPPED`로 낮춘다.

현재 Kline generation의 disconnect, gap, parse 또는 observer 실패는 한 번만 full-resync owner에
전달한다. 복구 worker는 새 generation buffering → 전체 REST Kline 조회 → 동일 handle의
live promotion과 buffer drain → REST/WS `MarketSnapshot` 병합 → `reconcileRegime` 순서를
지킨다. 새 4H source candle은 정상 재평가하고, 동일 candle은 지표·추천·STM state를
재검증한 뒤 duplicate STM transition이나 evaluation trace 없이 새 market version에 결과를
재결합한다. `RegimeResult.sourceMarketVersion`, authoritative snapshot/completion version과
same-generation live readiness가 모두 일치할 때만 시장 source blocker를 해제하며, `None`,
version mismatch 또는 평가 실패는 completion을 게시하지 않고 새 구독을 닫는다.
장애 시점에 RUNNING이던 session provenance는 자동으로 지우지 않는다. 이후 same-ID 주문 outcome이
pending을 끝내고 Context를 IDLE로 바꿔도 공개 status, `commandEnabled`와
`reconciliationRequired`는 계속 fail closed다. 단, exposure를 늘리지 않는 same-ID cancel/query와
history reconciliation은 중단 provenance 아래에서도 계속 허용한다.

Fault fixture는 credential 없는 canonical JSON과 expected digest를 가진다. wall clock, random UUID,
PID와 port는 명시 규칙으로 정규화한다. 같은 입력을 여러 번 replay해 state, order mutation count,
Trade와 UI event sequence digest가 같아야 한다.

## 7. 통합 gate와 soak

`scripts/check_all.sh` 기본 실행은 외부 주문 없이 backend, UI, Rust, release script, contract drift,
typecheck/build, secret/dependency/license/security scan과 deterministic E2E를 실행한다. UI 검증은
lockfile로 이미 설치된 local `vitest`, `tsc`, `vite`만 사용해 package-manager network 또는 서명
상태와 source 검증을 분리한다. Testnet 주문은 별도 명시 flag, 고정 Testnet
endpoint, order opt-in, configured policy와 `100 USDT` BUY hard cap을 모두 만족한
뒤에만 실행한다. 24시간 이상 Testnet soak와 memory/task/socket leak 판정은
사용자 결정으로 Phase 13에서 영구 제외했다. `phase13_soak.py`는 선택적
진단 도구로만 보존하며 master 완료 조건이나 통합 실행 목록이 아니다.

Actual target은 artifact root에 owner-only regular lockfile을 두고 parent directory와 leaf를
`O_NOFOLLOW` directory FD로 검증한 뒤 nonblocking exclusive `flock`을 runtime/client 생성 전에
획득한다. Lock 뒤에도 parent와 leaf device/inode를 재검증하고 process teardown까지 descriptor를
유지한다. Preexisting leaf는 `fchmod` 전에 regular type, owner와 single-link를 검증해 hardlink로
가리킨 unrelated inode의 bytes/mode를 바꾸지 않는다. 이는 같은 workspace·artifact target의 동시 실행만 막는 advisory lease이므로, 다른
Testnet 자동화, legacy target, 직접 REST client와 동일 account의 외부 주문은 절차적으로 모두
중지한 상태에서만 actual run을 시작한다.

Read-only와 actual preflight는 app prefix만 조회하지 않고 모든 client ID의 symbol-scoped open order와
최근 order 최대 1000개를 signed 조회한다. Open order는 0이어야 하며 recent
`(exchange order ID, client order ID)` set은 owner/inode/digest와 semantic replay를 통과한 closed
baseline Trade identity set과 정확히 같아야 한다. Fresh baseline은 양쪽 empty만 허용한다. Missing,
duplicate, manual/외부 order와 unstable identity는 mutation 전에 차단하고 failure 문장에는 raw
`OrderResult`, `Trade`, ID와 fill을 포함하지 않는다. Snapshot 뒤 동일 account의 별도 activity까지
kernel lock이 막지는 못하므로 외부 process·수동 주문 중지 조건은 그대로 유지한다.

REST transport는 모든 3xx를 non-success로 거부하고 redirect를 따라가지 않는다. 따라서 signed
query, API key header와 request body가 다른 origin으로 재전송되지 않는다. Actual success/failure
artifact는 같은 directory의 owner-only `O_EXCL|O_NOFOLLOW` 임시 inode에 canonical bytes를 쓰고
file fsync 후 hard-link no-clobber publish와 directory fsync를 수행한다. Publish 뒤 최종 inode,
mode, link count와 bytes를 다시 확인하며 collision이나 fault에서는 기존 destination을 보존하고
소유한 임시 파일만 제거한다.

Visual·supply evidence reader도 parent directory chain을 FD-relative `O_NOFOLLOW`로 열고 regular
leaf의 size/identity/metadata를 read 전후 검증한다. App content-tree digest는 정렬된 FD-relative
DFS와 streaming SHA-256을 사용하고 파일별 64 MiB, 전체 512 MiB, entry 10,000개 한계를 넘으면
fail closed한다. Visual 비교 입력은 검증한 bytes를 0600 `O_EXCL` 임시 파일로 복제하고 비교 성공·
실패 모두에서 제거한다.

Figma 16-state reference는 versioned manifest에서 fixture key, PNG 이름, 1440×1024 viewport와
SHA-256을 결합하고, axe WCAG A/AA scanner는 16개 DOM state를 hand-written assertion과 독립적으로
검사한다. Actual browser 16개도 DOM 1440×1024, DPR1과 explicit
`clip={x:0,y:0,width:1440,height:1024}`로 캡처해 repository `visual-regression/current/`에
보존했다. Output이 PNG가 아닌 JPEG/JFIF이므로 별도 manifest가 각 `.jpg` SHA-256과 format을
고정한다. Local FFmpeg 8.0은 양쪽에 동일한
`gblur=sigma=0.5:steps=1`·`yuv444p` anti-alias normalization을 적용한 뒤 frame별 SSIM `All`을
비교하며 threshold는 `0.980000`이다. Baseline 자동 갱신 option은 없고 current/tool/file 하나라도
누락되면 fail closed한다. Repository에 보존된 2026-08-29 capture는
`0.914102~0.981147`이며 `4/16` PASS, `12/16` FAIL이다. 2026-08-31 최종 source를 별도 임시
actual-browser capture로 다시 진단한 결과도 `0.915904~0.981311`, `4/16` PASS였으므로 실패
capture를 repository manifest에 승격하지 않고 visual SSIM gate를 정직하게 `NO_GO`로 유지한다.
Actual Storybook browser에서 addon-a11y를 각 상태마다 재실행해 jsdom이 계산하지 못하는
layout 기반 color contrast를 포함한 `16/16 Violations 0`은 별도로 완료했다. Public Case 2 local
trace와 production Spot REST memory transport를 포함해
Case 1~4의 positive/negative test reference를 닫았고, current-host Tauri harness가 production
directory picker의 선택→absolute UTF-8과 취소→`null`을 경로 비노출로 직접 검증했다.
Communication checker는 `126 COMPLETE / 0 GAP`이다.

Soak의 영구 제외 결정은 실행 또는 PASS 증거가 아니다. Readiness manifest는 이 항목을
`EXCLUDED / USER_SCOPE_EXCLUSION`으로 보존하고 in-scope GAP 집계에서만 뺀다.
다른 미완료 항목을 PASS로 바꾸거나 live readiness를 열지 않는다.

현재 generic readiness JSON schema v2는 evidence digest를 실제 artifact path와 byte에 bind하지
못한다. 따라서 `phase13_readiness.py`는 모든 in-scope check를 명시적 `GAP`으로 기록하는
scaffold만 허용하되 영구 제외 soak만 `EXCLUDED`로 구분하며, 임의 64자리 digest로 만든
외부 `PASS` manifest는 거부한다. 프로젝트 자체의 private/personal 정책 검토는 완료됐으므로
generic manual GAP은 `third_party_license_review`와 `third_party_notice`로 한정한다.

별도 supply evidence schema v4는 세 lockfile·local package inventory뿐 아니라 Python
`LicenseRef-Proprietary`/`Private :: Do Not Upload`, npm `private: true`/`UNLICENSED`, Cargo
`publish = false`/repository `LICENSE`를 현재 manifest byte와 root notice byte에 결합한다.
Backend proprietary notice와 README도 같은 private/personal 정책을 고지한다. 이로써 local
project license `UNKNOWN` group은 0이다. Retained scan은 현재 project manifest bytes를 결합하지 않아
`current_vs_scanned_match=false`다. Schema v4는 exact lockfile 868-component CycloneDX 1.6 SBOM,
coordinate-complete license inventory와 non-release-ready notice review, historical Phase 12 app content-tree/
DMG digest를 raw byte hash로 결합한다. Version-matched local installed metadata에서
third-party 494개의 license 선언과 source hash를 관찰했지만 법적 승인은 아니며,
나머지 372개는 `NOASSERTION`이다. 또한
historical artifact는 current Phase 13 source provenance와 결합되지 않았다. 따라서 PyInstaller
계열의 historical `non-standard`, `GPL-2.0` 두 group, current raw offline OSV/license output,
완전한 third-party metadata/final notice와 current Phase 13 release artifact는 미해결이므로
`artifact_binding.complete=false`와 `NO_GO`를 유지한다. Generic release evidence-binding
schema와 signer가 구현되기 전에는 두 보조 도구 모두 live gate를 열 수 없다.

Dependency·lockfile metadata를 외부 OSV 서비스로 전송하는 것은 영구 불허다.
`scripts/run_phase13_offline_osv.py`만 vulnerability/license 검사의 실행 경계로 사용하고,
OS network deny와 scanner offline cached DB를 함께 강제한다. 최신 advisory 확인 불가 또는
local cache 부재는 fail closed한다. 실제 local-only vulnerability/license 실행도 local DB
부재로 각각 exit `127`을 반환했으며 supply-chain/readiness `NO_GO`를 유지한다.

## 8. 확정 범위와 잔여 검증 경계

확정된 Phase 13 결정은 다음과 같다.

- 세 risk 상한은 configured `None`, daily loss는 `REALIZED_ONLY`, manual kill은
  `CANCEL_AND_LIQUIDATE`다.
- 30분 EMA는 period 9, 첫 9개 확정봉 SMA seed, alpha 0.2, 최근 EMA9 6개 OLS,
  Decimal 유효숫자 34와 최종 8자리 `ROUND_HALF_EVEN`을 사용한다. Raw slope는
  `raw_ols_slope / candidate_price * 100`, `%/30분봉`으로 정규화하고 네 사용 위치마다
  실제 계산 입력 가격을 분모로 사용한다.
- Phase 13 actual Testnet은 local gate 통과 후 `ETHUSDT`, 각 신규 BUY 최대
  `100 USDT`, 정확한 보유 수량의 recovery SELL만 허용한다. Preflight와 submit 직전에 current
  `exchangeInfo`, signed account `myFilters`, all-symbol open order/open order-list state와 public
  reference price를 각각 새로 읽는다. Signed/public filter의 공통 type·limit drift, count limit 0,
  account-wide non-empty 상태와 관찰 시각 순서 drift는 journal/POST 전에 fail closed한다.
  Base `MAX_ASSET`만 제출 quantity로 검증하고 quantity 기반 MARKET의 quote `MAX_ASSET`은 공식
  환산 가격식이 없으므로 고정 fail closed한다. Reference price는 MARKET
  `MIN_NOTIONAL`/`NOTIONAL`에만 사용한다. 완전한 account exposure evaluator가 필요한
  `MAX_POSITION`이 BUY에 존재하거나 reference price가 authoritative하지 않으면 fail closed한다.
  이 composite evidence는 가장 이른 관찰부터 고정 30초와 clock monotonicity를 만족해야 하며,
  transport `before_send`가 order POST 직전에 freshness와 최초 attempt evidence를 같은 시각으로
  결속한다. Trace v3의 별도 `public_relevant_filters` projection도 signed/public overlap과 같은
  30초 인과관계를 증명해야 한다.
- 프로젝트 자체는 비공개·개인용이며 Python/npm/Cargo의 license·publish metadata와
  repository notice로 강제한다. 제3자 dependency 의무는 별도로 준수한다.
- 외부 OSV 서비스 전송은 영구 불허이며 OS network deny·offline cached DB만 허용한다.
  최신 advisory 확인 불가 또는 cache 부재는 fail closed한다.
- 24시간 soak는 영구 제외하고 PASS로 기록하지 않는다.

2026-08-31 one-shot 외부 증거는 위 30초 transport guard와 trace overlap 보강 **직전 source**에
귀속된다. 그 source의 fixed-Keychain memory-only signed read-only는 `4/4` PASS했지만, 조건부
actual은 durable BUY 전에 reconciliation-required로 `FAILED`했다. 해당 Phase 13 harness의
app-attributable submission attempt·order POST delegate·BUY·STOP SELL·durable Trade는 0건이고
runtime Position/pending도 0이지만, 별도 fresh verification은 `INCOMPLETE`다. 따라서 동일 account의
독립 외부 mutation 부재나 종료 후 fresh zero exposure를 주장하지 않는다.

이후 보강한 current tree는 Backend `940/940`, scripts `183/183`, secure runner `14/14`, current
집중 `102` OK·external `4` safe skip, Communication `126/126`을 통과했지만 signed external target과
새로 결속하지 않았다. 다음 작업은 Keychain·Binance network·주문 없이 reconciliation 원인과 fresh
verification 실패 stage를 stable secret-free enum으로 먼저 분리하고 deterministic test로 닫는 것이다.
그 뒤 external 재실행이 필요하면 세 범위를 새로 명시 승인받으며 어떤 실패도 자동 재시도하지 않는다.

Configured-unbounded domain·wire·UI gate와 `CANCEL_AND_LIQUIDATE`의 receipt-first cleanup,
restart resume, exact activation replay, active-epoch provenance와 authoritative completion boolean은
focused local test evidence를 갖춘다. Private/personal 프로젝트 선언과 registry 배포 차단도
local byte-bound evidence를 갖추고 project `UNKNOWN` group을 제거했다. 30분 production builder,
all-interval 원자 경계, public local Case 2와 Communication `126/126`은 완료했다. 실제 Phase 13
Testnet public Case 2의 `SUCCESS`, visual SSIM·third-party supply evidence는 미완료이므로 P13-04,
P13-06~P13-08 전체 판정은
유지한다. P13-01 local implementation은 완료 상태를 유지하고 Phase 13 master만 나머지 전체 gate를
기다린다. 별도 live release commit·signed checklist·사용자 승인은
Phase 13 결정과 다른 경계이며, 그 승인 전에는 live를 계속 disabled로 유지한다.
