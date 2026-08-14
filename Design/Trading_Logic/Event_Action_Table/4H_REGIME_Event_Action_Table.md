# 4H REGIME 추천 Event-Action Table

| 항목 | 내용 |
|---|---|
| 문서 상태 | Implementation-normalized — Controller/STM 책임 분리 반영 |
| 최종 수정일 | 2026-08-14 |
| 대상 | `RegimeSTM`, `RegimeController`, 4H REGIME 추천 평가 흐름 |

## 1. 문서 범위와 책임 계약

- 대상 상태 머신: `4H_REGIME_Recommendation.png`
- 목적: 4시간봉 기준 REGIME 추천 상태, 이벤트, 가드, 전이 및 Controller 수행 Action을 구현 가능한 형식으로 정의한다.
- 보조 대조 자료: [regime_design.md](../../Specification/regime_design.md)
- 책임 분리 설계: [RegimeSTM 구현 계획](../../../Trading_STM/Regime_STM_Implementation_Plan.md)
- 범위 제한: 이 표는 `TYPE_0`~`TYPE_4` 추천값 산출까지만 다룬다. 사용자의 실제 REGIME 선택과 30분봉 매매 로직 실행은 포함하지 않는다.
- 이미지에 이름이 없던 이벤트는 구현 계약에 맞춰 `INITIAL_EVALUATION_REQUESTED`, `EVALUATION_READY`로 정규화했다.

이 문서에서 `Action`의 수행 주체는 `RegimeSTM`이 아니라 `RegimeController`이다.

| 구분 | 책임 |
|---|---|
| `RegimeSTM` | 현재 상태와 이벤트를 기준으로 가드를 평가하고, Event-Action ID·다음 상태·필요한 Action 요청을 결정한다. |
| `RegimeController` | 동일 시점의 4H 평가 입력을 준비하고, STM을 실행하고, STM 결과의 Action 요청을 실제로 수행한다. |

다음 원칙을 모든 행에 적용한다.

- STM이 자신의 `current_state`를 다음 상태로 바꾸는 것은 상태 전이이며 Event-Action Table의 Action 수행이 아니다.
- STM은 Action을 직접 실행하지 않고 `StartRegimeEvaluation` 또는 `ApplyRecommendedRegime` 값을 `RegimeSTMResult`에 담아 반환한다.
- Controller만 `recommended_regime`을 변경할 수 있다.
- 추천값은 사용자의 `selected_regime`을 자동으로 덮어쓰지 않는다.
- STM은 Controller, UI, `TradingController`, Gateway 또는 mutable snapshot을 직접 호출하거나 변경하지 않는다.

## 2. 상태, 이벤트 및 입력 정의

### 2.1 상태

| 상태 다이어그램 표기 | 구현 상태 | 의미 |
|---|---|---|
| `Initial` | `INITIAL` | 최초 평가 전 시작 의사 상태 |
| `4H Candle Evaluation` | `FOUR_HOUR_CANDLE_EVALUATION` | 준비된 4H 입력으로 추천 가드를 평가하는 중간 상태 |
| `Type_0 추천 (횡보)` | `TYPE_0_RECOMMENDED` | 횡보 REGIME 추천 상태 |
| `Type_1 추천 (약상승)` | `TYPE_1_RECOMMENDED` | 약상승 REGIME 추천 상태 |
| `Type_2 추천 (강상승)` | `TYPE_2_RECOMMENDED` | 강상승 REGIME 추천 상태 |
| `Type_3 추천 (약하락)` | `TYPE_3_RECOMMENDED` | 약하락 REGIME 추천 상태 |
| `Type_4 추천 (강하락)` | `TYPE_4_RECOMMENDED` | 강하락 REGIME 추천 상태 |

### 2.2 이벤트

| 이벤트 | 생성 주체 | 의미 |
|---|---|---|
| `INITIAL_EVALUATION_REQUESTED` | `RegimeController` | 최초 시장 snapshot과 최소 평가 데이터가 준비된 뒤 평가 cycle을 시작한다. |
| `FOUR_HOUR_CANDLE_CLOSED` | `RegimeController` | 확정된 새 4H 봉을 중복 제거한 뒤 재평가 cycle을 시작한다. |
| `EVALUATION_READY` | `RegimeController` | 동일 `evaluation_id`의 검증된 불변 평가 Context가 준비되었음을 STM에 알린다. |

### 2.3 조건식 기호

| 기호 | 구현 해석 |
|---|---|
| `ema9_slope` | 4H EMA9 기울기. 단위는 `% / 4H봉`이며 모든 분기에서 이 이름으로 통일한다. |
| `HH` | 최근 스윙 고점이 이전 스윙 고점보다 유의하게 높은 상태(Higher High) |
| `HL` | 최근 스윙 저점이 이전 스윙 저점보다 유의하게 높은 상태(Higher Low) |
| `LH` | 최근 스윙 고점이 이전 스윙 고점보다 유의하게 낮은 상태(Lower High) |
| `LL` | 최근 스윙 저점이 이전 스윙 저점보다 유의하게 낮은 상태(Lower Low) |
| `HH && HL` | `HH`와 `HL`이 모두 참인 강상승 구조. 이미지의 `HH + HL`을 논리곱으로 정규화한 표현이다. |
| `LH && LL` | `LH`와 `LL`이 모두 참인 강하락 구조. 이미지의 `LH + LL`을 논리곱으로 정규화한 표현이다. |
| `current_price` | 평가 Context에 고정된 현재가 |
| `live_ema9` | 같은 평가 Context에 고정된 실시간 4H EMA9. 이미지의 `realtime_4h_ema9`를 정규화한 이름이다. |

### 2.4 평가 Context 불변식

- `ema9_slope`, `HH/HL/LH/LL`, `current_price`, `live_ema9`는 하나의 `MarketSnapshot` version에서 파생한다.
- 수치는 finite `Decimal`을 사용하며 `current_price`와 `live_ema9`는 0보다 커야 한다.
- 재평가 Context는 방금 확정된 4H 봉의 `source_candle_id`를 포함한다.
- Controller는 Context를 완전히 계산하고 검증한 뒤에만 평가 시작 이벤트를 STM에 전달한다.
- 입력이 부족하거나 계산에 실패하면 STM을 호출하지 않고 마지막 정상 추천과 상태를 유지한다.

## 3. Event-Action Table

### 3.1 최초 평가 및 추천 판정

| ID | 현재 상태 | 이벤트 | 가드 조건 | RegimeSTM 결정 | RegimeController 수행 Action | 다음 상태 | 비고 |
|---|---|---|---|---|---|---|---|
| `EA-001` | `INITIAL` | `INITIAL_EVALUATION_REQUESTED` | 없음 | `StartRegimeEvaluation(INITIAL)` 요청을 반환한다. | 준비한 Context의 `evaluation_id`를 검증하고 `EVALUATION_READY`를 다음 microstep으로 전달한다. | `FOUR_HOUR_CANDLE_EVALUATION` | 시작 화살표의 무명 이벤트를 구현 이벤트로 정규화했다. |
| `EA-002` | `FOUR_HOUR_CANDLE_EVALUATION` | `EVALUATION_READY` | `-0.15 <= ema9_slope <= 0.15` | `ApplyRecommendedRegime(TYPE_0)` 요청을 반환한다. | `recommended_regime`을 `TYPE_0`으로 적용하고 `RegimeResult`를 기록·통지한다. | `TYPE_0_RECOMMENDED` | 양쪽 경계 `-0.15`, `0.15`를 포함한다. |
| `EA-003` | `FOUR_HOUR_CANDLE_EVALUATION` | `EVALUATION_READY` | `0.15 < ema9_slope < 0.30` | `ApplyRecommendedRegime(TYPE_1)` 요청을 반환한다. | `recommended_regime`을 `TYPE_1`로 적용하고 `RegimeResult`를 기록·통지한다. | `TYPE_1_RECOMMENDED` | 기울기만으로 약상승을 추천한다. |
| `EA-004` | `FOUR_HOUR_CANDLE_EVALUATION` | `EVALUATION_READY` | `(ema9_slope >= 0.30) && !((HH && HL) && (current_price >= live_ema9))` | `ApplyRecommendedRegime(TYPE_1)` 요청을 반환한다. | `recommended_regime`을 `TYPE_1`로 적용하고 `RegimeResult`를 기록·통지한다. | `TYPE_1_RECOMMENDED` | 강상승 복합조건 전체가 충족되지 않은 경우다. |
| `EA-005` | `FOUR_HOUR_CANDLE_EVALUATION` | `EVALUATION_READY` | `(ema9_slope >= 0.30) && ((HH && HL) && (current_price >= live_ema9))` | `ApplyRecommendedRegime(TYPE_2)` 요청을 반환한다. | `recommended_regime`을 `TYPE_2`로 적용하고 `RegimeResult`를 기록·통지한다. | `TYPE_2_RECOMMENDED` | 강상승 기울기·구조·가격 위치를 모두 요구한다. |
| `EA-006` | `FOUR_HOUR_CANDLE_EVALUATION` | `EVALUATION_READY` | `-0.30 < ema9_slope < -0.15` | `ApplyRecommendedRegime(TYPE_3)` 요청을 반환한다. | `recommended_regime`을 `TYPE_3`으로 적용하고 `RegimeResult`를 기록·통지한다. | `TYPE_3_RECOMMENDED` | 기울기만으로 약하락을 추천한다. |
| `EA-007` | `FOUR_HOUR_CANDLE_EVALUATION` | `EVALUATION_READY` | `(ema9_slope <= -0.30) && !((LH && LL) && (current_price <= live_ema9))` | `ApplyRecommendedRegime(TYPE_3)` 요청을 반환한다. | `recommended_regime`을 `TYPE_3`으로 적용하고 `RegimeResult`를 기록·통지한다. | `TYPE_3_RECOMMENDED` | 원본 이미지의 조건 공백을 제거했다. 강하락 복합조건 전체의 실패를 처리한다. |
| `EA-008` | `FOUR_HOUR_CANDLE_EVALUATION` | `EVALUATION_READY` | `(ema9_slope <= -0.30) && ((LH && LL) && (current_price <= live_ema9))` | `ApplyRecommendedRegime(TYPE_4)` 요청을 반환한다. | `recommended_regime`을 `TYPE_4`로 적용하고 `RegimeResult`를 기록·통지한다. | `TYPE_4_RECOMMENDED` | 강하락 기울기·구조·가격 위치를 모두 요구한다. |

### 3.2 확정 4H 봉 마감 후 재평가

원본 이미지의 “현재 진행 중인 4H봉 마감 / 마감된 봉을 포함하여 재판단”을 다음 구현 이벤트와 Action 계약으로 정규화한다.

| ID | 현재 상태 | 이벤트 | 가드 조건 | RegimeSTM 결정 | RegimeController 수행 Action | 다음 상태 | 비고 |
|---|---|---|---|---|---|---|---|
| `EA-101` | `TYPE_0_RECOMMENDED` | `FOUR_HOUR_CANDLE_CLOSED` | 없음 | `StartRegimeEvaluation(FOUR_HOUR_CANDLE_CLOSE)` 요청을 반환한다. | 방금 마감된 4H 봉을 포함한 준비 Context로 `EVALUATION_READY`를 전달한다. | `FOUR_HOUR_CANDLE_EVALUATION` | 새 추천 적용 전까지 기존 `TYPE_0` 추천값을 유지한다. |
| `EA-102` | `TYPE_1_RECOMMENDED` | `FOUR_HOUR_CANDLE_CLOSED` | 없음 | `StartRegimeEvaluation(FOUR_HOUR_CANDLE_CLOSE)` 요청을 반환한다. | 방금 마감된 4H 봉을 포함한 준비 Context로 `EVALUATION_READY`를 전달한다. | `FOUR_HOUR_CANDLE_EVALUATION` | 새 추천 적용 전까지 기존 `TYPE_1` 추천값을 유지한다. |
| `EA-103` | `TYPE_2_RECOMMENDED` | `FOUR_HOUR_CANDLE_CLOSED` | 없음 | `StartRegimeEvaluation(FOUR_HOUR_CANDLE_CLOSE)` 요청을 반환한다. | 방금 마감된 4H 봉을 포함한 준비 Context로 `EVALUATION_READY`를 전달한다. | `FOUR_HOUR_CANDLE_EVALUATION` | 새 추천 적용 전까지 기존 `TYPE_2` 추천값을 유지한다. |
| `EA-104` | `TYPE_3_RECOMMENDED` | `FOUR_HOUR_CANDLE_CLOSED` | 없음 | `StartRegimeEvaluation(FOUR_HOUR_CANDLE_CLOSE)` 요청을 반환한다. | 방금 마감된 4H 봉을 포함한 준비 Context로 `EVALUATION_READY`를 전달한다. | `FOUR_HOUR_CANDLE_EVALUATION` | 새 추천 적용 전까지 기존 `TYPE_3` 추천값을 유지한다. |
| `EA-105` | `TYPE_4_RECOMMENDED` | `FOUR_HOUR_CANDLE_CLOSED` | 없음 | `StartRegimeEvaluation(FOUR_HOUR_CANDLE_CLOSE)` 요청을 반환한다. | 방금 마감된 4H 봉을 포함한 준비 Context로 `EVALUATION_READY`를 전달한다. | `FOUR_HOUR_CANDLE_EVALUATION` | 새 추천 적용 전까지 기존 `TYPE_4` 추천값을 유지한다. |

## 4. 정규화된 판정 구간과 경계값

| `ema9_slope` 범위 | 추가 조건 | 추천 결과 | transition ID |
|---|---|---|---|
| `ema9_slope <= -0.30` | `(LH && LL) && current_price <= live_ema9` | `TYPE_4 강하락` | `EA-008` |
| `ema9_slope <= -0.30` | 위 강하락 복합조건이 성립하지 않음 | `TYPE_3 약하락` | `EA-007` |
| `-0.30 < ema9_slope < -0.15` | 없음 | `TYPE_3 약하락` | `EA-006` |
| `-0.15 <= ema9_slope <= 0.15` | 없음 | `TYPE_0 횡보` | `EA-002` |
| `0.15 < ema9_slope < 0.30` | 없음 | `TYPE_1 약상승` | `EA-003` |
| `ema9_slope >= 0.30` | `(HH && HL) && current_price >= live_ema9` | `TYPE_2 강상승` | `EA-005` |
| `ema9_slope >= 0.30` | 위 강상승 복합조건이 성립하지 않음 | `TYPE_1 약상승` | `EA-004` |

경계값은 다음처럼 고정한다.

- `-0.30`은 강하락 복합 판정에 포함한다.
- `-0.15`와 `0.15`는 횡보에 포함한다.
- `0.30`은 강상승 복합 판정에 포함한다.
- 강한 구조가 성립할 때 `current_price == live_ema9`는 `TYPE_2` 또는 `TYPE_4`에 포함한다.
- 유효한 평가 Context에는 `EA-002`~`EA-008` 중 정확히 하나가 적용되어야 한다.

## 5. Controller Action 수행 계약

### 5.1 두 단계 평가 cycle

한 번의 최초 평가 또는 재평가는 다음 두 transition으로 처리한다.

1. `EA-001` 또는 `EA-101`~`EA-105`가 평가 상태로 전이하고 `StartRegimeEvaluation`을 요청한다.
2. `RegimeController`가 요청을 수행해 같은 `evaluation_id`의 `EVALUATION_READY`를 전달한다.
3. `EA-002`~`EA-008` 중 하나가 추천 상태로 전이하고 `ApplyRecommendedRegime`을 요청한다.
4. `RegimeController`가 추천값과 평가 결과 metadata를 원자적으로 적용한다.

`RegimeSTM`의 canonical 결과는 추천 타입 자체가 아니라 다음 정보를 가진 불변 `RegimeSTMResult`이다.

```text
decision_id
consumed
transition_id
state_before
state_after
action_requests
evaluation_id
```

### 5.2 Action 요청과 실제 수행

| Action 요청 | RegimeSTM 책임 | RegimeController 수행 |
|---|---|---|
| `StartRegimeEvaluation` | 필요한 평가 시작 Action의 종류와 trigger를 결정한다. | 준비 Context와 `evaluation_id`를 확인하고 `EVALUATION_READY`를 직렬 처리 queue의 다음 microstep에 등록한다. |
| `ApplyRecommendedRegime` | 적용할 `RegimeType`을 가드와 전이로 결정한다. | `recommended_regime`을 갱신하고 `RegimeResult`를 기록·반환·통지한다. |

Action 요청에는 실행 함수, coroutine, Controller/Gateway/UI 객체 또는 mutable snapshot을 넣지 않는다.

### 5.3 추천값과 사용자 선택값

| 값 | 의미 | 변경 가능한 경로 |
|---|---|---|
| `recommended_regime` | 마지막 정상 4H 평가 결과 | `RegimeController`의 `ApplyRecommendedRegime` handler |
| `selected_regime` | 사용자가 실제 거래에 적용한 REGIME | 사용자 요청을 받은 `set_regime_type()` 경로 |

- 추천 결과가 바뀌어도 `selected_regime`은 유지한다.
- 추천 Action handler는 `TradingController` 또는 TradingSTM을 선택하지 않는다.
- 같은 TYPE이 다시 추천되어도 평가 결과와 transition trace는 기록한다.
- “추천 변경” 통지는 추천값이 실제로 바뀐 경우에만 발행하고, 매 평가 완료가 필요하면 별도 “평가 완료” 통지를 사용한다.

### 5.4 실패·중복·stale 처리

- 입력 부족이나 계산 실패는 STM 호출 전에 Controller가 차단한다.
- 최초 평가 실패 시 `recommended_regime`은 `None`, 재평가 실패 시 마지막 정상 추천을 유지한다.
- 기본 `TYPE_0` 또는 다른 추천값을 임의 적용하지 않는다.
- 확정 4H 봉은 `symbol + interval + open_time`에 해당하는 candle ID로 중복 제거한다.
- Action의 `evaluation_id`가 준비 Context와 다르면 적용하지 않는다.
- 한 symbol의 평가 cycle은 Controller의 단일 writer queue 또는 lock으로 직렬화한다.

## 6. 원본 대비 확정한 정규화

| 항목 | 확정 내용 |
|---|---|
| Action 수행 주체 | STM은 Action 요청만 결정하고 실제 Action은 `RegimeController`가 수행한다. |
| `EA-007` 조건 공백 | `ema9_slope <= -0.30`에서 강하락 복합조건 전체가 실패하면 `TYPE_3`을 추천한다. |
| slope 변수명 | `ema_9_slope`를 사용하지 않고 `ema9_slope`로 통일한다. |
| live EMA9 변수명 | `realtime_4h_ema9`를 구현 이름 `live_ema9`로 통일한다. |
| swing 구조 | `HH + HL`, `LH + LL`을 각각 `HH && HL`, `LH && LL`로 구현한다. |
| 최초 평가 이벤트 | 최소 데이터가 포함된 최초 `MarketSnapshot` 준비 후 `INITIAL_EVALUATION_REQUESTED`를 생성한다. |
| 재평가 이벤트 | 새 확정 4H 봉을 포함한 snapshot 준비 후 `FOUR_HOUR_CANDLE_CLOSED`를 생성한다. |
| 결측치 처리 | STM을 호출하지 않고 마지막 정상 상태와 추천값을 유지한다. |

`EA-007`은 원본 이미지의 아래 표현과 다르다.

```text
원본:
(ema9_slope <= -0.30)
&& ((LH + LL) && !(current_price <= realtime_4h_ema9))

구현 기준:
(ema9_slope <= -0.30)
&& !((LH && LL) && (current_price <= live_ema9))
```

변경 이유는 `LH && LL`이 성립하지 않는 강한 음의 slope 구간까지 `TYPE_3` 실패 분기로 완전하게 처리하기 위해서다.

## 7. 구현 전에 추가로 확정할 항목

다음 항목은 책임 분리와 추천 결정 트리에는 영향을 주지 않지만 실제 입력 준비와 운영 동작을 위해 확정해야 한다.

| 항목 | 필요한 결정 |
|---|---|
| 지표 최소 데이터 | EMA9, 최근 6개 EMA9 LR slope, 두 swing point 쌍을 계산할 최소 확정 4H 봉 수 |
| swing threshold | HH/HL/LH/LL 유의 변화율을 `0.3%`~`0.5%` 중 어떤 값으로 사용할지 |
| 현재가 고정 시점 | `MarketSnapshot`을 생성하는 정확한 tick과 진행 중 4H 봉 update 기준 |
| 계산 실패 retry | 다음 market snapshot, 다음 4H close 또는 별도 backoff 중 재시도 시점 |
| process restart | 마지막 처리 candle ID와 추천값을 복원할지 시작 시 다시 평가할지 |

미확정 값을 임의 상수나 암묵적 기본 추천으로 구현하지 않는다.

## 8. 구현 기준 결정 트리

아래 결정 트리는 `EA-002`~`EA-008`의 완전하고 배타적인 구현 기준이다. 추천값 적용은 이 결정을 받은 `RegimeController`가 수행한다.

```text
if ema9_slope >= 0.30:
    if HH_and_HL and current_price >= live_ema9:
        request ApplyRecommendedRegime(TYPE_2)  # EA-005
    else:
        request ApplyRecommendedRegime(TYPE_1)  # EA-004
elif 0.15 < ema9_slope < 0.30:
    request ApplyRecommendedRegime(TYPE_1)      # EA-003
elif -0.15 <= ema9_slope <= 0.15:
    request ApplyRecommendedRegime(TYPE_0)      # EA-002
elif -0.30 < ema9_slope < -0.15:
    request ApplyRecommendedRegime(TYPE_3)      # EA-006
elif ema9_slope <= -0.30:
    if LH_and_LL and current_price <= live_ema9:
        request ApplyRecommendedRegime(TYPE_4)  # EA-008
    else:
        request ApplyRecommendedRegime(TYPE_3)  # EA-007
```
