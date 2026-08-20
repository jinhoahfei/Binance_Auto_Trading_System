# ADR-001 — Canonical REGIME 타입과 Trading registry 매핑

| 항목 | 값 |
|---|---|
| 상태 | Accepted |
| 결정일 | 2026-08-20 |
| 적용 결정 | D-01, D-02, D-03, D-04, D-09 |
| 기준 커밋 | `2a70b45adbc443a9c782cf3699e76ec527e2d6be` (`main`) |

## 1. 배경

현재 세 영역은 같은 개념을 서로 다르게 표현한다.

- RegimeSTM은 `TYPE_0`~`TYPE_4`를 사용한다.
- UI wire 계약은 `type0`~`type4`를 사용한다.
- TradingSTM은 `LOWER_BB`만 허용한다.

또한 Communication 명세의 `RegimeSTM.run(...) : RegimeType`과 인자 없는
`TradingSTM.orderFinished()`는 이미 구현된 두 STM의 안전한 public 계약과 다르다.
이 차이를 코드 통합 전에 해소하지 않으면, 미지원 REGIME이 다른 전략으로 조용히
대체되거나 주문 결과가 잘못된 전이로 전달될 수 있다.

## 2. 결정

### 2.1 Canonical 타입과 wire 값

도메인의 유일한 REGIME 타입은 `TYPE_0`~`TYPE_4`다. Python 통합 package에서는
`domain/common/enums.py` 한 곳만 이 enum을 소유한다. TypeScript와 transport는 다음의
엄격한 일대일 변환만 사용한다.

| Domain | Wire | 표시 의미 |
|---|---|---|
| `TYPE_0` | `type0` | 횡보 |
| `TYPE_1` | `type1` | 약상승 |
| `TYPE_2` | `type2` | 강상승 |
| `TYPE_3` | `type3` | 약하락 |
| `TYPE_4` | `type4` | 강하락 |

변환 규칙은 다음과 같다.

- Domain 내부, 저장 schema와 trace에는 대문자 domain 이름을 쓴다.
- HTTP/WS와 UI 계약에서만 소문자 wire 값을 쓴다.
- 대소문자 보정, 숫자 추출, `TYPE_0` 기본값 같은 관대한 변환은 금지한다.
- 표에 없는 입력은 `INVALID_REGIME_TYPE`으로 거부한다.
- `LOWER_BB`는 REGIME 타입이 아니다. `TYPE_0`의 현재 trading transition registry를
  식별하는 private registry key로만 유지하고 Phase 1에서 공용 `RegimeType` enum에서
  제거한다.

### 2.2 Phase 0 baseline의 Trading registry 매핑

현재 구현과 근거 문서를 기준으로 다음 매핑을 잠근다.

| REGIME | Trading registry | 상태 | 선택 후 start 동작 | 근거 |
|---|---|---|---|---|
| `TYPE_0` | `LOWER_BB` 109개 transition | 매핑됨 / 구현 부분 완료 | Phase 6 전에는 `TRADING_LOGIC_INCOMPLETE`; 상단 BB 인계 계약까지 검증된 뒤 enable | `regime_design.md` §9의 “기존 30m 횡보 로직”, `UI_Behavior.md` §3의 “Basic Iterative 횡보장 조건”, 현재 유일한 30m 구현인 하단 BB registry |
| `TYPE_1` | 없음 | 미지원 | `UNSUPPORTED_TRADING_LOGIC` | 약상승 30m Event-Action Table/registry 없음 |
| `TYPE_2` | 없음 | 미지원 | `UNSUPPORTED_TRADING_LOGIC` | 상위 gate 설명만 있고 강상승 30m Event-Action Table/registry 없음 |
| `TYPE_3` | 없음 | 미지원 | `UNSUPPORTED_TRADING_LOGIC` | 약하락 30m Event-Action Table/registry 없음 |
| `TYPE_4` | 없음 | 미지원 | `UNSUPPORTED_TRADING_LOGIC` | 하위 gate 설명만 있고 강하락 30m Event-Action Table/registry 없음 |

`TYPE_0 -> LOWER_BB`는 기존 문서의 이름이 완전히 일치해서 발견된 매핑이 아니라,
위 세 근거를 함께 적용해 Phase 0에서 확정한 architecture decision이다. 향후 근거가
달라지면 이 ADR을 대체하고 Communication 명세와 Event-Action Table을 먼저 변경한다.

현재 lower-BB registry의 `G-07`은 상단 BB 상태로 인계하지만 대상 정책의 Operation과
Event-Action Table이 아직 없다. 따라서 Phase 0 baseline에서는 다섯 REGIME 모두
production start가 비활성이다. `TYPE_0`은 mapping 자체는 확정되었지만 Phase 6에서 상단
BB 인계 계약과 registry coverage를 완성하거나, 별도의 안전한 종료 정책을 명세·검증한
뒤에만 start를 enable한다. 이를 조용히 no-op 상태로 운용하지 않는다.

미지원 타입도 추천·표시·선택할 수는 있다. 다만 UI는 지원 상태를 함께 표시하고,
`TradingController.fetchSelectedTradingLogic(...)`과 `/v1/trading/start`는 각각
`TRADING_LOGIC_INCOMPLETE` 또는 `UNSUPPORTED_TRADING_LOGIC`으로 시작을 거부한다. 어떤
경우에도 `TYPE_0`이나 `LOWER_BB`로 fallback하지 않는다. 새 REGIME을 지원하려면 해당
30m Event-Action Table, 상태도, registry ID 목록과 경계 테스트가 먼저 있어야 한다.

### 2.3 RegimeSTM public 계약

RegimeSTM의 canonical operation은 다음 하나다.

```text
handle(event : RegimeEvent, context : RegimeEvaluationContext?) : RegimeSTMResult
```

- 시작 event와 4H 마감 event는 첫 microstep에서 `StartRegimeEvaluation`을 반환한다.
- RegimeController는 같은 `evaluation_id`의 Context를 검증한 뒤
  `EVALUATION_READY`를 다음 microstep으로 전달한다.
- 두 번째 결과의 `ApplyRecommendedRegime`을 수행한 뒤에만
  `recommendedRegime`을 갱신한다.
- `recommendRegime(...) : RegimeType`은 Controller façade다. STM의 `run(...)`을
  호출하는 별도 판정 경로가 아니며 반드시 위 두 microstep을 사용한다.
- 추천값은 사용자 `selectedRegime`을 바꾸지 않는다.

### 2.4 TradingSTM 주문 결과 계약

TradingSTM의 canonical decision operation은 다음과 같다.

```text
handle(event : TradingEvent, context : TradingContextView) : TradingSTMResult
```

Communication 호환 operation은 다음과 같이 구체화한다.

```text
orderFinished(
    event : TradingEvent,
    context : TradingContextView
) : TradingSTMResult
```

`orderFinished`는 `handle`의 검증 adapter일 뿐 별도의 상태 추론 경로가 아니다. 다음의
구체적인 정규화 event만 허용한다.

- `CASE_B_POSITION_OPENED`, `CASE_B_BUY_FAILED`
- `CASE_B_SELL_FILLED`, `CASE_B_SELL_FAILED`
- `CASE_C_POSITION_OPENED`, `CASE_C_BUY_FAILED`
- `CASE_C_SELL_FILLED`, `CASE_C_SELL_FAILED`
- `FORCE_SELL_FINISHED`, `FORCE_SELL_FAILED`

Controller가 실제 fill을 Position에 반영하고 거래 이력 저장을 완료한 뒤에만 성공
event를 전달한다. 인자 없는 호출이나 pending Context로 성공·실패를 추론하는 구현은
금지한다.

### 2.5 실행 중 REGIME 변경

첫 release에서는 active TradingSTM hot-swap을 금지한다.

- `tradingPhase`가 `IDLE` 또는 `TERMINATED`일 때만 선택 REGIME을 바꿀 수 있다.
- `ENTRY_ORDER_PENDING`, `EXIT_ORDER_PENDING`, `STOPPING`,
  `RECONCILIATION_REQUIRED`를 포함해 세션이 active이면 `TRADING_ACTIVE`로 거부한다.
- 거부 시 기존 `selectedRegime`, STM 인스턴스와 Context는 바뀌지 않는다.
- 사용자는 정상 stop 완료 뒤 REGIME을 선택하고 새 session을 시작해야 한다.
- UI 문서의 실행 중 즉시 적용 표현은 이 결정으로 대체되며, UI 연결 Phase에서 동일
  오류와 안내를 반영한다.

## 3. 대안과 기각 이유

- 다섯 REGIME을 모두 `LOWER_BB`로 연결: 서로 다른 30m 매매 규칙을 추측하므로 기각한다.
- `LOWER_BB`를 여섯 번째 REGIME으로 유지: 4H 분류와 trading registry 식별자를
  혼합하므로 기각한다.
- 실행 중 STM 교체: pending 주문과 Position owner의 의미가 바뀔 수 있어 기각한다.
- 인자 없는 `orderFinished()`: 결과 종류와 대상 전략을 안전하게 식별할 수 없어 기각한다.

## 4. 구현 및 검증 의무

- [x] 다섯 domain/wire 값의 일대일 표가 확정되었다.
- [x] `TYPE_0` mapping은 부분 완료, `TYPE_1`~`TYPE_4`는 미지원이며 현재 다섯 타입 모두 start disabled로 확정되었다.
- [x] lower-BB registry의 소속과 근거가 기록되었다.
- [x] 두 STM의 canonical signature가 실제 구현과 일치한다.
- [x] active session의 REGIME 변경 거부가 확정되었다.
- [x] Phase 1에서 중복 Python `RegimeType`을 `domain/common/enums.py` 한 enum으로 통합했다.
- [ ] Phase 6에서 TYPE_0 상단 BB 인계 gap을 닫고 mapping/fallback gate를 코드·테스트로 고정한다.
- [ ] UI 연결 Phase에서 미지원 표시와 `TRADING_ACTIVE` 오류를 반영한다.

## 5. Baseline 증거

2026-08-20 20:39 KST, 기준 커밋 `2a70b45`에서 실행했다.

| 영역 | 명령 | 결과 |
|---|---|---|
| RegimeSTM | `PYTHONPATH=src python3 -m unittest discover -s tests -v` | 31/31 통과 |
| TradingSTM | `PYTHONPATH=src python3 -m unittest discover -s tests -v` | 24/24 통과 |
| UI | `./node_modules/.bin/vitest run --reporter=dot` | 26 files, 88/88 통과 |
| UI typecheck | `./node_modules/.bin/tsc -b --pretty false` | 통과 |
| UI build | `./node_modules/.bin/vite build` | 274 modules, 성공 |

검증 과정에서 production source 동작은 변경하지 않았다.
