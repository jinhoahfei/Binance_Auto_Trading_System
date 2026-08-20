# Binance Auto Backend

`binance-auto-trader-backend`는 RegimeSTM과 TradingSTM을 하나의
`binance_auto_trader` distribution으로 통합한 Python package입니다. 두 STM은
상태와 guard만 판정하고, 외부 효과는 typed action request로 반환합니다.

## 구성

- `domain/common/enums.py`: 두 STM이 공유하는 canonical `RegimeType`
- `domain/regime/`: 13개 `EA-*` transition의 4시간봉 REGIME 추천 STM
- `domain/trading/`: 109개 transition의 run-to-completion TradingSTM
- `tests/`: 기존 RegimeSTM 31개와 TradingSTM 24개 회귀 테스트
- `tests/architecture/`: package, enum, import 경계와 coding convention 검증

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

실제 Gateway, Repository, Controller와 transport는 후속 Phase의 범위이며 현재
domain package에는 network/file 의존성이 없습니다.
