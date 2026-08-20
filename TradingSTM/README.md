# TradingSTM

`TradingSTM`은 Trading Logic Event-Action Table의 109개 transition을 구현한 동기식 결정 엔진입니다. STM은 상태와 Guard만 판정하고, Context 변경·주문·timer·queue 작업은 직렬화 가능한 action request로 반환합니다.

## 구성

- `states.py`: 최상위 상태와 3개 병렬 Region을 한 번에 보존하는 불변 상태 구성
- `events.py`, `context.py`: 불변 event 및 동일 평가 시점의 Context snapshot
- `action_requests.py`, `results.py`: Controller가 실행할 typed action과 STM 결과
- `transitions/`: G/O/PB/PC/B/C Event-Action Table 구현
- `stm.py`: Region broadcast, Case C 우선 arbitration, 원자적 상태 commit
- `event_queue.py`: Controller에서 사용할 내부-event 우선 FIFO와 run-to-completion processor

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
from binance_auto_trader.trading import RegimeType, TradingSTM

stm = TradingSTM.get_stm_instance(RegimeType.LOWER_BB)
initial_result = stm.run(context_view)
next_result = stm.handle(event, next_context_view)
```

실제 주문 adapter는 `SubmitOrder`의 `idempotency_key`를 client order key로 사용하고, `NEW`, `PARTIALLY_FILLED`, `UNKNOWN` 결과에서는 새 주문을 제출하지 말고 `ReconcileOrder` 계약에 따라 같은 주문 ID를 조회해야 합니다.

## 테스트

외부 패키지 없이 표준 라이브러리 `unittest`로 실행할 수 있습니다.

```bash
cd Trading_STM
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

retry 간격·최대 횟수, 부분 체결 잔여 주문 정책, 실제 Gateway/Repository 연결은 설계 문서 16.2절의 미확정 Controller 정책이므로 이 패키지에 임의의 거래 정책으로 넣지 않았습니다.
