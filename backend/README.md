# Binance Auto Backend

`binance-auto-trader-backend`는 RegimeSTM과 TradingSTM, authoritative 시장·계좌
데이터 및 거래 이력 초기 로드 vertical slice를 하나의
`binance_auto_trader` distribution으로 통합한 Python package입니다. 두 STM은
상태와 guard만 판정하고, 외부 효과는 typed action request로 반환합니다.

## 구성

- `domain/common/enums.py`: backend가 공유하는 canonical `RegimeType`/
  `Interval`
- `domain/market/`: Decimal OHLCV `Kline`과 versioned `MarketSnapshot`
- `domain/regime/`: 13개 `EA-*` transition의 4시간봉 REGIME 추천 STM
- `domain/trading/`: 109개 transition의 run-to-completion TradingSTM과
  free/locked balance를 보존하는 `Account`
- `domain/history/`: ADR-004 JSONL v1 `Trade`, KST query `TradeHistory`,
  fee 포함 startup `Performance`
- `adapters/binance/`: 주입된 client의 Binance Spot Kline/account
  REST/WebSocket payload 정규화와 dedup
- `adapters/persistence/`: local JSONL streaming 복원과 partial tail 복구
- `application/market_data_controller.py`: WS 먼저 구독, REST 조회,
  buffer 병합, snapshot 교체 순서 조정
- `application/trading_controller.py`: REST 계좌 적용 뒤 account stream을
  시작하는 `load_account()` slice
- `application/trade_history_controller.py`: Repository → TradeHistory →
  Performance 초기 복원
- `tests/`: STM 회귀와 market/account/history unit·integration 검증
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

Gateway는 네트워크 SDK를 직접 선택하지 않고 주입된 client
Protocol을 사용합니다. 현재 Phase의 자동 검증은 fake REST/WebSocket client만
사용하며, 실제 계좌·주문 API를 호출하지 않습니다.

## 계좌와 거래 이력 초기 로드 계약

`TradingController.load_account()`는 Spot account REST 전체 snapshot을 먼저
`Account`에 적용하고, 이미 준비된 `MarketSnapshot`의 ETHUSDT 가격으로
free+locked ETH 평가금액을 계산한 뒤 User Data Stream을 시작합니다. Binance
`outboundAccountPosition`의 `B`는 변경 가능성이 있는 자산만 담는 absolute
partial patch이므로, 생략된 잔액은 유지합니다. source update time보다 오래된
event와 동일 payload 중복은 무시하되 같은 millisecond의 다른 patch는 수신 순서대로
적용합니다. malformed event, domain callback 실패 또는 공식
`eventStreamTerminated`를 받으면 해당 transport 구독을 닫고 fail closed 처리합니다.

`TradeHistoryRepository.get_trade_history()`는 UTF-8/LF JSONL을 한 줄씩 읽고
Decimal string과 UTC timestamp를 canonical `Trade`로 복원합니다. 파일 없음과
0-byte 파일만 빈 이력으로 취급하며, permission·완결 record 손상은 그대로
실패합니다. JSON parsing에 실패한 non-LF 마지막 tail만 별도 corrupt 파일에
보존하고 backup file과 parent directory를 fsync한 뒤 마지막 정상 LF까지
truncate합니다. directory fsync 실패 시 원본을 유지합니다. 이 startup 복구 구간은
bootstrap process 하나가 history 경로를 독점하고 다른 writer가 없다는 계약입니다.
`TradeHistoryController`는 같은
거래 tuple로 `TradeHistory`와 ADR-004 `Performance`를 모두 만든 뒤 원자적으로
공개합니다. durable `Trade`의 `fee_quote_amount`는 USDT fee면 원래 금액과,
ETH fee면 execution 시 fill별 가격으로 환산해 합친 authoritative 값으로
보존합니다. 여러 fill의 maker/taker 요율이 다를 수 있으므로 ETH 총 fee에 평균
체결가를 다시 곱하지 않으며, 두 fee 금액의 0 여부만 일치시킵니다. 제3 fee asset은
임의 시세를 사용하지 않고 `FEE_ASSET_CONVERSION_REQUIRED`로 복구 절차를 요구합니다.

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

`TYPE_0`은 Phase 0에서 기존 lower-BB registry와 매핑되었지만, Phase 6의
상단 BB 인계 coverage gate 전에는 production trading start를 허용하지
않습니다. `TYPE_1`~`TYPE_4`를 `TYPE_0`으로 대체하지 않습니다.

실제 주문 adapter는 `SubmitOrder`의 `idempotency_key`를 client order key로 사용하고, `NEW`, `PARTIALLY_FILLED`, `UNKNOWN` 결과에서는 새 주문을 제출하지 말고 `ReconcileOrder` 계약에 따라 같은 주문 ID를 조회해야 합니다.

## 테스트

외부 패키지 없이 표준 라이브러리 `unittest`로 실행할 수 있습니다.

```bash
cd backend
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

실제 Binance client 조립, 주문 Gateway, history append/export와 transport는 후속
Phase의 범위입니다. domain package에는 network/file 의존성이 없습니다.
