# 하단 볼린저밴드 대응 전략 기술서

이 문서는 하단 볼린저밴드 터치 이후 동작하는 두 개의 독립 전략을 정리한다.

- Case B: `BBW < 0.02` 하단 BB 눌림매수 전략
- Case C: 하단 BB 과이탈, 이른바 칼날잡기 반등 전략

목표는 두 전략을 하나의 하단 BB 대응 체계 안에서 유기적으로 연결하되, 각각의 백테스트 case 수와 승률 특성이 크게 훼손되지 않도록 운용 규칙을 명확히 하는 것이다.

---

## 1. 전체 목적

하단 볼린저밴드에 닿았다고 즉시 매수하지 않는다.

하단 BB 터치 이후 시장은 크게 두 가지로 나뉜다.

1. 변동성이 좁은 상태에서 하단을 건드린 뒤 회복하는 눌림 구간
2. 하단을 과하게 이탈한 뒤 짧은 급반등이 나오는 칼날잡기 구간

따라서 하단 BB 터치 이벤트가 발생하면 두 전략을 독립적으로 감시한다.

```text
30m 하단 BB 터치
  -> Case B 후보 감시: BBW < 0.02 눌림매수
  -> Case C 후보 감시: %B 과이탈 + CCI 과매도 칼날잡기
```

단, 실제 포지션 진입은 둘 중 하나만 허용한다.

---

## 2. 공통 전제

- 기준 타임프레임: 30분봉
- 실시간 감시: 현재가 기준 realtime %B
- Bollinger Band: 30분봉 close 기준 20기간, 표준편차 2
- `%B = (current_price - lower) / (upper - lower)`
- 하단 BB 대응 목적이므로 상단 BB에 닿으면 새 상단 전략을 시작하지 않고
  주문·포지션 상태에 맞는 안전 종료 절차로 전환한다.

공통 원칙:

- 터치 직후 저점 예측 매수를 하지 않는다.
- 회복 조건 또는 과이탈 이후 반등 조건을 확인한 뒤 진입한다.
- Case B와 Case C는 setup, entry, exit 성격이 다르므로 상태를 분리한다.
- 한 전략이 포지션을 열면 다른 전략의 신규 진입은 중지한다.

---

## 3. Case B: BBW < 0.02 하단 BB 눌림매수 전략

### 3.1 목적

좁은 변동성 구간에서 30분봉 하단 BB를 터치한 뒤, 바로 저점매수하지 않고 회복 신호를 확인한다.

그 뒤 3시간 안에 다시 눌리는 구간에서 매수해 짧은 평균회귀 구간을 먹는 전략이다.

핵심 흐름:

```text
하단 BB 터치 + BBW < 0.02
  -> WAIT_SIGNAL
  -> 최초 회복 신호 확정
  -> WAIT_PULLBACK
  -> realtime %B <= 0.30 눌림매수
  -> %B >= 0.60 익절
  -> 강하면 TREND_HOLD
```

### 3.2 하단 BB 터치 감지

30분봉 기준이다.

```python
if 30m_low <= lower_band and bbw < 0.02:
    state = "WAIT_SIGNAL"
```

주의:

- `BBW < 0.02`는 매수봉 기준이 아니다.
- 반드시 하단 BB를 터치한 30분봉 기준의 BBW다.

### 3.3 신호 확정 조건

하단 BB 터치 이후, 각 30분봉 종가 확정 시점마다 확인한다.

```python
if (
    ema_slope > -0.03
    and pct_b_close > 0.25
    and signal_candle.low >= min(previous_3_candles.low)
):
    signal_time = candle_close_time
    state = "WAIT_PULLBACK"
```

규칙:

- 최초로 조건을 만족한 30분봉만 signal candle로 인정한다.
- 같은 터치 이벤트에서 신호를 여러 번 만들면 안 된다.
- `signal_candle.low >= min(previous_3_candles.low)`는 하단 터치봉 기준이 아니라 signal candle 기준이다.

의미:

- 종가상 회복한 것처럼 보여도 봉 내부에서 직전 3봉 저점을 갱신한 위험 신호는 제외한다.

### 3.4 실제 매수 조건

Signal 확정 이후 3시간 동안 realtime %B 눌림을 기다린다.

```python
if (
    state == "WAIT_PULLBACK"
    and elapsed_time_from_signal <= 3 * 60 * 60
    and realtime_pct_b <= 0.30
):
    buy()
    state = "POSITION_OPEN"
```

3시간 안에 체결되지 않으면 해당 signal은 폐기한다.

```python
if elapsed_time_from_signal > 3 * 60 * 60:
    reset_case_b_state()
```

백테스트 기준:

- signal 이후 6개 30분봉 안에 `candle.low <= %B 0.30 price <= candle.high`이면 체결로 본다.

### 3.5 기본 익절

실시간 기준이다.

```python
if realtime_pct_b >= 0.60 for 5 seconds:
    check_trend_hold_condition()
```

일반 익절:

```python
if realtime_pct_b >= 0.60 for 5 seconds and realtime_ema_slope <= 0.08:
    sell_take_profit()
    reset_case_b_state()
```

### 3.6 Trend Hold 진입

강한 반등이면 즉시 익절하지 않고 trend hold로 전환한다.

```python
if realtime_pct_b >= 0.60 for 5 seconds and realtime_ema_slope > 0.08 for 5 seconds:
    state = "TREND_HOLD"
```

주의:

- trend hold 판단의 `%B`와 `realtime_ema_slope`는 모두 실시간 기준이다.
- `realtime_ema_slope`는 현재가를 임시 close로 넣어 계산한다.
- trend hold는 익절 이후 확장 상태이며 신규 매수 조건이 아니다.

### 3.7 Trend Hold 종료

Trend hold 상태에서는 아래 조건 중 하나라도 만족하면 매도한다.

```python
if realtime_ema_slope <= 0.04 for 5 seconds:
    sell_trend_hold_exit()
```

또는:

```python
if realtime_pct_b < 0.60 for 5 seconds:
    sell_trend_hold_exit()
```

또는:

```python
if upper_band_touched:
    apply_upper_band_safe_termination()
```

### 3.8 손절

전략 손절은 30분봉 종가 기준이다.

```python
if confirmed_30m_close and ema_slope < -0.08:
    sell_stop_loss()
    reset_case_b_state()
```

### 3.9 비상 손절

급락 방어용 실시간 손절이다.

```python
if realtime_price <= entry_price * 0.99:
    sell_emergency_stop()
    reset_case_b_state()
```

### 3.10 시간청산

매수 후 6시간 안에 익절, 손절, 비상손절, trend hold 종료가 없으면 청산한다.

```python
if holding_time >= 6 * 60 * 60:
    sell_time_exit()
    reset_case_b_state()
```

### 3.11 백테스트 성과

기준:

- `BBW < 0.02`
- 익절: `%B >= 0.60`
- 손절: 30분봉 종가 `ema_slope < -0.08`
- 시간청산: 매수 후 6시간
- trend hold 미반영

| 구분 | 거래 | 승률 | 평균수익 | 중앙수익 | 누적수익 | 익절 | 손절 | 시간청산 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 2025-12 | 42 | 85.71% | +0.4608% | +0.4063% | +19.3517% | 36 | 4 | 2 |
| 2026-04 | 44 | 75.00% | +0.2895% | +0.4051% | +12.7359% | 33 | 9 | 2 |
| 합산 | 86 | 80.23% | +0.3731% | +0.4054% | +32.0876% | 69 | 13 | 4 |

사유별 평균:

| 사유 | 횟수 | 평균수익 | 중앙수익 | 합계 |
|---|---:|---:|---:|---:|
| 익절 | 69 | +0.5707% | +0.4646% | +39.3763% |
| 손절 | 13 | -0.5438% | -0.5049% | -7.0696% |
| 시간청산 | 4 | -0.0548% | -0.0317% | -0.2191% |

---

## 4. Case C: 하단 BB 과이탈 반등 전략

### 4.1 목적

Case C는 30분봉 볼린저밴드 하단을 과하게 이탈한 뒤 짧은 회복이 나오는 구간만 먹는 전략이다.

핵심은 저점 예측이 아니다.

```text
하단 과이탈
  -> 더 깊은 flush_low 확인
  -> 짧은 회복 확인
  -> 아직 충분히 낮은 위치에서 진입
  -> %B 0.10 회복 구간에서 기본 수익 확보
  -> 회복 강도가 좋아지는 일부 케이스만 트레일링
```

전략 성격:

- 장기 추세 추종이 아니다.
- 과매도 이후 짧은 평균회귀 반등 포착 전략이다.

### 4.2 기준

- 지표 계산 기준: 30분봉
- 실거래 감시/체결 기준: 실시간
- 백테스트 대체 기준: 1분봉 OHLC
- CCI: 30분봉 표준 CCI 20
- EMA slope: 30분봉 EMA 기준

### 4.3 SETUP 조건

```python
if realtime_pct_b <= -0.15 and cci_30m <= -140:
    state = "CASE_C_SETUP"
```

백테스트:

```python
if low_1m_pct_b <= -0.15 and cci_30m <= -140:
    state = "CASE_C_SETUP"
```

### 4.4 중복 방지

한 30분봉 안에서는 Case C를 1회만 인정한다.

통합 운용에서는 청산 후 또는 Case 종료 후 아래 조건 전까지 새 Case C setup을 찾지 않는다.

```python
if realtime_pct_b >= 0.25:
    allow_new_case_c_setup = True
```

주의:

- 단독 백테스트의 기존 쿨다운 기준이 `%B > 0.30`이었다면, 통합 운용에서는 `%B >= 0.25`로 낮춘다.
- 이유는 Case B의 1차 회복 신호가 30분봉 종가 기준 `%B_close > 0.25`이므로, Case C 회복 판정을 이 구간에 맞추면 이후 Case B 판정만 켜두는 전환이 자연스럽다.

### 4.5 1차 신호

SETUP 이후 더 깊은 하락이 나와야 한다.

```python
if realtime_pct_b <= -0.25:
    flush_low = current_price
    flush_low_pct_b = realtime_pct_b
    flush_low_time = now
    timer_base_pct_b = flush_low_pct_b
    timer_base_time = now
```

### 4.6 flush_low 갱신

매수 전까지 더 낮은 저점이 나오면 계속 갱신한다.

```python
if current_price < flush_low:
    flush_low = current_price
    flush_low_pct_b = realtime_pct_b
    flush_low_time = now
    timer_base_pct_b = flush_low_pct_b
    timer_base_time = now
```

### 4.7 3분 회복 타이머

매수는 `timer_base_pct_b` 기준으로 3분 안에 `%B +0.06` 회복해야 한다.

```python
entry_pct_b = timer_base_pct_b + 0.06

if (
    realtime_pct_b >= entry_pct_b
    and now - timer_base_time <= 3 * 60
    and entry_pct_b < -0.15
):
    buy()
    state = "POSITION_OPEN"
```

3분 안에 회복이 안 나와도 Case를 폐기하지 않는다.

```python
if now - timer_base_time > 3 * 60:
    timer_base_pct_b = current_open_pct_b
    timer_base_time = now
```

단, 더 낮은 저점 발생 시에는 flush_low 갱신이 우선이다.

### 4.8 회복했지만 매수하지 않는 경우

회복 조건은 만족했지만 `entry_pct_b >= -0.15`이면 매수하지 않는다.

이 경우:

- Case는 유지한다.
- 매수하지 않는다.
- 이후 다시 내려가면 flush_low를 재갱신할 수 있다.

의미:

- 충분히 낮은 가격이 아니면 추격매수하지 않는다.

### 4.9 매수 전 Case 종료

매수 전 가격이 충분히 회복되면 Case C를 종료한다.

```python
if realtime_pct_b >= 0.25:
    reset_case_c_without_trade()
```

이 종료는 하단 BB 이벤트 전체 종료가 아니다.

Case C만 소모시키고, 동일 하단 터치 이벤트 안에서 Case B의 `WAIT_SIGNAL` 또는 `WAIT_PULLBACK`은 계속 유지할 수 있다.

### 4.10 익절권 진입

매수 후 가격이 `%B 0.10`에 도달하면 익절권에 진입한다.

```python
if realtime_pct_b >= 0.10:
    tp_price = lower + 0.10 * (upper - lower)
    previous_trail_ema_slope = ema_slope_30m(tp_price)
    state = "TP_TRAILING"
```

주의:

- `tp_price`는 매수가가 아니다.
- `tp_price`는 `%B 0.10` 기본 익절 기준가다.

### 4.11 익절 트레일링

익절권 진입 후 바로 매도하지 않고 회복 강도가 유지되는지 확인한다.

실시간 보호:

```python
if realtime_pct_b < 0.10:
    sell_at_tp_price()
    reset_case_c_state()
```

1분봉 마감 기준 트레일링:

```python
current_close_ema_slope = ema_slope_30m(close_1m)

if current_close_ema_slope > previous_trail_ema_slope:
    previous_trail_ema_slope = current_close_ema_slope
    hold()
else:
    sell_at_1m_close()
    reset_case_c_state()
```

주의:

- 트레일링 판단도 30분봉 EMA slope 기준이다.
- 1분봉 EMA를 사용하지 않는다.

### 4.12 손절

현재 최종 적용 손절:

```python
if ema_slope_30m_realtime <= -0.55 for 3 minutes:
    sell_stop_loss()
    reset_case_c_state()
```

백테스트:

- 1분봉 close 기준 30분봉 EMA slope `<= -0.55`
- 3개 1분봉 연속 발생하면 세 번째 1분봉 close 가격으로 손절

미적용 후보:

- 매수가 대비 `-1.1%` 고정 손절
- 현재 최종 전략에는 포함하지 않는다.

### 4.13 시간청산

```python
if holding_time >= 60 * 60:
    sell_time_exit()
    reset_case_c_state()
```

백테스트:

- entry 이후 60번째 1분봉 open 가격으로 청산

### 4.14 청산 후 쿨다운

청산 후 바로 새 Case C를 보지 않는다.

```python
if realtime_pct_b >= 0.25:
    allow_new_case_c_setup = True
```

통합 운용 의미:

- Case C가 손절, 시간청산, `%B 0.10` 익절, 트레일링 익절 중 어떤 방식으로 끝났든 우선 Case C는 해당 하단 터치 이벤트에서 소모된 것으로 본다.
- 실시간 `%B >= 0.25`가 확인되면 시장이 과이탈 구간에서 회복했다고 판단한다.
- 이후 다음 하단 BB 터치 전까지는 Case C를 다시 켜지 않고 Case B 판정만 유지하는 것이 기본이다.

### 4.15 청산 우선순위

매수 후 익절권 진입 전:

```text
1. %B 0.10 익절권 진입 확인
2. 손절 조건 확인
3. 시간청산 확인
```

익절권 진입 후:

```text
1. %B 0.10 이탈 fallback 확인
2. 1m close 기준 30m ema_slope 상승/하락 확인
3. 시간청산 확인
```

### 4.16 백테스트 성과

대상 데이터:

- 2025-12
- 2026-03
- 2026-04

월별 결과:

| 월 | 거래 | 총수익 |
|---|---:|---:|
| 2025-12 | 29 | +11.0677% |
| 2026-03 | 34 | +16.8640% |
| 2026-04 | 22 | +7.7256% |

합산:

| 항목 | 값 |
|---|---:|
| trades | 85 |
| TP_TRAIL | 8 |
| TP_FALLBACK | 64 |
| STOP | 7 |
| TIME | 6 |
| 승률 | 84.7% |
| 평균 수익률 | +0.4195% |
| 총수익률 | +35.6573% |

매도 %B 평균:

| 사유 | 평균 매도 %B |
|---|---:|
| TP_TRAIL | 0.2577 |
| TP_FALLBACK | 0.1013 |
| STOP | -0.4178 |
| TIME | -0.0702 |
| 전체 | 0.0612 |

---

## 5. Case B와 Case C의 유기적 결합 방식

### 5.1 핵심 문제

두 전략을 단순히 합치면 백테스트 성격이 깨질 수 있다.

이유:

- Case B는 비교적 위에서 회복 확인 후 눌림매수한다.
- Case C는 하단을 깊게 과이탈한 뒤 매우 낮은 위치에서 짧은 반등을 먹는다.
- 둘을 완전히 배타적으로 만들면 한쪽 백테스트 case 수가 줄어든다.
- 둘을 완전히 독립 매매시키면 같은 하단 BB 이벤트에서 중복 진입이 발생한다.

따라서 결합 목표는 다음이다.

```text
setup 감시는 병렬
실제 포지션 진입은 단일
한 전략 체결 후 다른 전략은 제한적으로만 유지
각 전략의 백테스트 case 분포를 최대한 보존
```

### 5.2 병렬 setup 감시

하단 BB 터치 이벤트가 발생하면 두 전략을 동시에 켠다.

```text
LOWER_BB_TOUCH_EVENT
  -> Case B monitor ON if touch_candle_bbw < 0.02
  -> Case C monitor ON if realtime %B <= -0.15 and CCI <= -140
```

이때 Case B와 Case C는 서로의 setup을 막지 않는다.

의미:

- BBW가 좁은 하단 터치였더라도 과이탈이 발생할 수 있다.
- 과이탈 감시 중에도 이후 회복 후 눌림매수 기회가 생길 수 있다.
- 따라서 setup 단계에서는 병렬 감시가 맞다.

### 5.3 포지션 진입 우선권

포지션 진입은 먼저 entry 조건을 만족한 전략이 가져간다.

```text
Case C buy 발생
  -> Case C POSITION_OPEN
  -> Case B 신규 매수 금지

Case B buy 발생
  -> Case B POSITION_OPEN
  -> Case C 신규 매수 금지
```

이 원칙은 중복 매수를 막기 위한 것이다.

### 5.4 Case C 체결 후 Case B를 어떻게 유지할 것인가

사용자 의도상 중요한 결합 규칙이다.

Case C가 먼저 setup과 buy를 만족하면, 칼날잡기 포지션을 우선 처리한다.

Case C가 종료된 뒤에는 종료 사유를 구분한다.

기본 회복 기준은 실시간 `%B >= 0.25`다.

- Case C가 종료됐다는 것은 손절했거나, 시간청산했거나, `%B 0.10` 이상 회복 구간에서 익절했다는 뜻이다.
- 여기서 다시 `%B >= 0.25`까지 회복하면 과이탈 칼날잡기 국면은 일단 끝난 것으로 본다.
- Case B의 신호 확정도 30분봉 종가 기준 `%B_close > 0.25`이므로, 실시간 `%B >= 0.25`는 Case C를 끄고 Case B 판정만 남기는 자연스러운 연결점이다.
- 이후 다시 하단 BB에 닿으면 새로운 하단 터치 이벤트로 보고 Case C를 다시 켤 수 있다.

Case C 종료 후에는 종료 사유와 매도 위치에 따라 Case B 인계 방식을 나눈다.

Case B를 이어서 켜는 가장 합리적인 경우는 `TP_TRAIL` 매도 후에도 가격 위치가 아직 너무 높지 않을 때다.

```python
if case_c_exit_reason == "TP_TRAIL" and exit_pct_b < 0.40:
    case_b_enabled = True
    case_c_consumed_for_event = True
    case_b_only_until_next_lower_touch = True
else:
    keep_case_b_wait_only_and_lock_case_c_until_recovery()
```

이유:

- Case C 백테스트에서 `TP_TRAIL`은 8회로 전체 85회 중 약 10%다.
- `TP_TRAIL` 평균 매도 `%B`는 `0.2577`로, Case B의 회복 신호 영역과 가깝다.
- 이 구간에서 매도 후에도 `%B < 0.40`이면 아직 상단 추세가 아니라 하단 회복 이후 눌림 가능성이 남아 있다.
- `%B 0.40`은 `0.50`보다 모멘텀 관점에서 보수적인 상한이다. 너무 위로 회복한 뒤 Case B를 억지로 이어붙이는 상황을 줄인다.
- 따라서 이때는 Case B 판정을 적극 유지한다.
- 반대로 `TP_FALLBACK`, `STOP`, `TIME` 종료는 흐름이 약하거나 실패한 종료이므로 Case B 매수까지 자동으로 밀어붙이지 않는다.
- 다만 이 경우에도 Case B 판정 대기는 켜둔다. Case C만 `%B >= 0.25` 회복 전까지 잠근다.

정리:

```text
Case C exit 후
  -> Case B wait/signal 판정은 유지 가능
  -> Case C는 realtime %B >= 0.25 회복 전까지 재가동 금지
  -> 회복조건 만족 후 다시 하단 BB에 닿으면 새 하단 터치 이벤트로 B/C 모두 재판정
```

권장 흐름:

```text
하단 BB 터치
  -> Case B monitor ON
  -> Case C monitor ON

Case C buy
  -> Case B buy PAUSED
  -> Case C exit

Case C exit 후
  -> realtime %B >= 0.25 회복 확인
  -> 같은 하단 터치 이벤트 안에서는 Case C 재탐색 OFF
  -> if exit_reason == TP_TRAIL and exit %B < 0.40:
       Case B 판정만 ON
       Case B WAIT_SIGNAL 또는 WAIT_PULLBACK 조건 유지
     else:
       Case B 판정 대기만 ON
       Case C는 회복조건 전까지 OFF
  -> Case B 조건을 만족하지 못한 채,
     Case C 회복조건(%B >= 0.25)을 만족한 뒤 다시 하단 BB에 닿으면
     새 이벤트로 보고 Case B / Case C 모두 처음처럼 재판정
```

이렇게 하면:

- Case C의 백테스트 흐름은 보존된다.
- Case C 종료 직후 다시 과이탈을 반복 진입하는 위험을 줄인다.
- TP_TRAIL처럼 실제로 강한 회복을 보인 일부 케이스에서만 Case B setup을 이어받는다.
- STOP/TIME처럼 약한 종료에서도 Case B의 확인 신호 대기는 남겨두되, Case C는 회복 전까지 잠가 과잉 재진입을 막는다.
- `%B 0.25`는 Case C 종료 후 회복 확인선이고, `%B 0.40`은 TP_TRAIL 이후 Case B 인계 허용 상한선이다.

### 5.5 Case B 체결 후 Case C 처리

Case B가 먼저 매수되면 이미 눌림매수 포지션이 열린 상태다.

이후 같은 이벤트 안에서 Case C setup이 발생해도 신규 매수하지 않는다.

```text
Case B buy
  -> Case C monitor OFF for entry
  -> Case B exit rules only
```

다만 Case B 포지션 중 급락하면 Case B의 비상 손절 또는 30분봉 손절이 책임진다.

Case C는 Case B 포지션의 추가 매수 근거로 쓰지 않는다.

중요 예외:

Case B 포지션 보유 중 급락이 발생하면, Case C setup 조건이 동시에 보일 수 있다.

특히 Case B의 30분봉 손절 조건인 `ema_slope < -0.08`은 구조 실패를 보는 조건이고, Case C setup은 `%B <= -0.15`와 `CCI <= -140`의 과이탈 조건이다. 일반적으로 30분봉 손절이 먼저 명확하게 잡히는 경우에는 Case C까지 동시에 열리지 않는 경우가 많지만, 실시간 비상손절 영역에서는 둘이 겹쳐 보일 수 있다.

이때도 규칙은 단순하다.

```text
Case B POSITION_OPEN
  -> Case C entry OFF
  -> Case C setup 기록은 가능하지만 매수 권한 없음
  -> Case B 비상손절 또는 Case B 30m 손절만 실행
  -> 손절 매도 후 현재 가격/확정봉이 하단 BB 이벤트 조건이면 새 하단 터치 이벤트로 즉시 재판정
```

이유:

- Case B가 이미 포지션 owner이므로 Case C가 같은 이벤트에서 추가 매수하면 독립 백테스트 분포가 깨진다.
- Case B 비상손절은 급락 방어용 안전장치이므로, 손절 직전에 Case C로 갈아타는 식의 구조는 피한다.
- Case B 손절 후 현재 가격이 여전히 하단 BB에 닿아 있거나 과이탈 조건이면, 백테스트와 비슷하게 새 하단 터치 이벤트로 보고 Case B와 Case C를 다시 병렬 감시한다.

백테스트 유사성 기준:

Case B 포지션 중에는 Case C가 개입하지 않는다. 그러나 Case B가 손절 또는 비상손절로 매도된 뒤에는 포지션 owner가 사라졌으므로, 그 직후의 시장 상태를 새 하단 BB 이벤트로 다시 판정한다.

```text
Case B stop/emergency stop sell 완료
  -> owner = None
  -> if current_price <= lower_band or confirmed_30m_low <= lower_band:
       start_new_lower_event()
       if touch_candle_bbw < 0.02:
           Case B monitor ON
       Case C monitor ON
```

이렇게 해야 하는 이유:

- 단독 백테스트에서 Case C는 Case B 포지션 존재 여부를 알지 못하고, 과이탈 setup이 나오면 독립적으로 기회를 센다.
- 통합 실거래에서는 중복 포지션을 막기 위해 Case B 보유 중 Case C를 꺼두지만, Case B가 손절로 끝난 뒤에도 Case C를 계속 꺼두면 단독 백테스트 대비 Case C 기회가 과도하게 줄어든다.
- 따라서 보유 중에는 Case C off, 손절 매도 후에는 새 이벤트로 B/C 재판정하는 방식이 백테스트 case 분포에 더 가깝다.

세부 기준:

- Case B가 30분봉 `ema_slope < -0.08` 손절로 끝났고 해당 확정봉의 low가 하단 BB를 터치했다면, 그 확정봉을 새 touch candle로 본다.
- Case B가 실시간 비상손절로 끝났고 매도 직후 현재가가 하단 BB 이하라면, 그 시점을 새 하단 터치 이벤트로 본다.
- 단, 새 이벤트에서 Case B는 다시 `BBW < 0.02` touch candle 조건부터 판단한다.
- 새 이벤트에서 Case C도 다시 `%B <= -0.15`, `CCI <= -140` setup 조건부터 판단한다.
- 같은 포지션에서 손절 전 Case C 조건이 보였다는 이유만으로 즉시 Case C 매수를 실행하지 않는다. 매도 완료 후 새 이벤트에서 다시 조건을 본다.

### 5.6 동일 터치 이벤트의 종료 기준

같은 하단 BB 터치 이벤트는 아래 중 하나로 종료한다.

```text
1. 두 전략 모두 setup 폐기
2. 포지션 진입 후 청산 완료
3. realtime %B >= 0.25 회복 후 Case C 소모 처리
4. 새로운 30분봉에서 다시 하단 BB 터치 발생
5. 상단 BB 터치로 lower-BB session 안전 종료
```

### 5.7 백테스트 case 수 보존을 위한 규칙

각 독립 알고리즘의 백테스트 승률과 case 수를 보존하려면 아래 규칙이 필요하다.

1. setup 단계는 병렬 감시한다.
2. entry 단계는 선착순 단일 포지션으로 제한한다.
3. Case C 종료 후 같은 하단 터치 이벤트 안에서는 Case C 재진입을 막는다.
4. Case C가 `TP_TRAIL`로 끝났고 매도 시점 `%B < 0.40`이면 Case B signal 또는 pullback 대기를 적극 유지한다.
5. Case C가 `TP_FALLBACK`, `STOP`, `TIME`으로 끝났어도 Case B 판정 대기는 유지할 수 있다. 다만 Case C는 `%B >= 0.25` 회복 전까지 다시 켜지 않는다.
6. Case C 회복조건 만족 후 Case B가 확정되기 전에 다시 하단 BB를 터치하면, 새 하단 터치 이벤트로 보고 Case B와 Case C를 처음처럼 모두 재판정한다.
7. Case B가 먼저 체결되면 Case C는 해당 이벤트에서 신규 진입하지 않는다.
8. Case B의 `BBW < 0.02` 기준은 반드시 터치봉 기준으로 고정한다.
9. Case C의 `CCI <= -140`과 `%B <= -0.15/-0.25`는 별도 과이탈 기준으로 유지한다.
10. 손절과 익절은 각 포지션을 연 전략의 규칙만 따른다.

### 5.8 통합 상태 설계안

실제 구현 시에는 하단 BB 이벤트 단위의 컨트롤러가 필요하다.

예시 상태:

```text
LOWER_EVENT_IDLE
LOWER_EVENT_MONITORING
CASE_B_WAIT_SIGNAL
CASE_B_WAIT_PULLBACK
CASE_B_POSITION_OPEN
CASE_B_TREND_HOLD
CASE_C_SETUP
CASE_C_FLUSH_TRACKING
CASE_C_POSITION_OPEN
CASE_C_TP_TRAILING
LOWER_EVENT_COOLDOWN
```

공통 이벤트 상태:

```text
lower_event_id
lower_event_touch_time
lower_event_touch_candle_bbw
lower_event_owner = None | CASE_B | CASE_C
case_b_enabled
case_c_enabled
case_b_entry_paused
case_c_entry_paused
case_c_consumed_for_event
case_c_recovered_for_case_b
case_c_exit_reason
case_c_exit_pct_b
case_b_only_until_next_lower_touch
case_c_recovery_confirmed
```

핵심:

- `lower_event_owner`는 실제 포지션을 연 전략이다.
- `case_c_consumed_for_event`는 Case C가 한 번 끝난 뒤 같은 이벤트에서 재진입하지 못하게 막는다.
- `case_c_recovered_for_case_b`는 Case C 종료 후 실시간 `%B >= 0.25` 회복이 확인되어 Case B 판정만 남긴 상태를 뜻한다.
- `case_c_exit_reason`은 `TP_TRAIL`, `TP_FALLBACK`, `STOP`, `TIME` 등을 기록한다.
- `case_c_exit_pct_b`는 Case C 매도 시점의 realtime `%B`다.
- `case_b_only_until_next_lower_touch`는 Case C를 재가동하지 않고 Case B만 감시하는 구간을 뜻한다.
- `case_c_recovery_confirmed`는 Case C 종료 후 realtime `%B >= 0.25` 회복이 확인되었음을 뜻한다.
- Case B는 touch candle BBW가 `< 0.02`였던 이벤트에서만 활성화한다.

### 5.9 통합 의사코드

```python
if lower_bb_touched:
    start_lower_event()
    if touch_candle_bbw < 0.02:
        case_b_enabled = True
    case_c_enabled = True

while lower_event_active:
    if owner is None:
        if case_b_enabled:
            update_case_b_signal_and_pullback()

        if case_c_enabled and not case_c_consumed_for_event:
            update_case_c_setup_and_flush()

        if case_c_buy_signal:
            owner = "CASE_C"
            pause_case_b_entry()
            execute_case_c_buy()

        elif case_b_buy_signal:
            owner = "CASE_B"
            disable_case_c_entry()
            execute_case_b_buy()

    elif owner == "CASE_C":
        update_case_c_exit()
        if case_c_closed:
            owner = None
            case_c_consumed_for_event = True
            wait_until_realtime_pct_b_reaches_0_25()
            if case_c_exit_reason == "TP_TRAIL" and case_c_exit_pct_b < 0.40:
                case_c_recovered_for_case_b = True
                case_b_only_until_next_lower_touch = True
                allow_case_b_only_until_next_touch()
            else:
                case_b_only_until_next_lower_touch = True
                allow_case_b_wait_only_until_next_touch()

            if realtime_pct_b >= 0.25:
                case_c_recovery_confirmed = True

    elif owner == "CASE_B":
        update_case_b_exit()
        if case_b_closed:
            owner = None
            if case_b_exit_reason in ["STOP", "EMERGENCY_STOP"] and is_lower_bb_event_now():
                start_new_lower_event()
                if touch_candle_bbw < 0.02:
                    case_b_enabled = True
                case_c_enabled = True
            else:
                end_lower_event_or_enter_cooldown()

    if upper_band_touched:
        apply_upper_band_safe_termination()
```

### 5.10 상단 BB 안전 종료

Case B 또는 Case C 진행 중 상단 BB에 닿으면 `G-07`이
`UpperBandPolicy.SAFE_TERMINATION`을 적용한다. 이 정책은 미구현 상단
상태 머신으로 인계하는 전략이 아니라 lower-BB session을 종료하는
안전 정책이다.

```python
if current_price >= upper_band:
    if pending_order_id is not None:
        enter_stopping()
        cancel_pending_order(reason="UPPER_BAND_SAFE_TERMINATION")
        reconcile_same_order(stop_after_reconciliation=True)
    elif position_owner is not None:
        enter_stopping()
        cancel_strategy_evaluation()
        force_sell_all()
    else:
        close_lower_event_and_reset_case_context()
        set_trading_phase_terminated()
        cancel_session_evaluation()
        stop_trading_runtime(reason="UPPER_BAND_SAFE_TERMINATION")
```

pending 주문 branch는 포지션 보유 branch보다 우선한다. 두 상태가 함께
있어도 `ForceSellAll`을 즉시 요청하지 않고, 같은 주문 ID의 취소·조정으로
실제 fill과 잔여 수량을 먼저 확정한다. 조정 후 포지션이 남으면 기존
STOP 계약의 전량 매도로 이어진다.

포지션만 있는 branch는 `STOPPING`으로 전이해 전략 평가를 취소하고
전량 매도를 요청한다. 후속 체결·실패는 기존 `G-06F`/`G-06R`이
처리한다. 주문과 포지션이 모두 없는 branch는 lower event, Case B/C
Context, pending 필드를 정리하고 즉시 `LOGIC_TERMINATED`로 종료한다.

---

## 6. 최종 압축

### Case B

```text
하단 BB 터치 + 터치봉 BBW < 0.02
  -> 최초 회복 signal 확인
  -> 3시간 안에 realtime %B <= 0.30 눌림매수
  -> realtime %B >= 0.60 익절
  -> ema_slope 강하면 trend hold
  -> 30m close ema_slope < -0.08 손절
  -> -1% 비상손절
  -> 6시간 시간청산
```

### Case C

```text
realtime %B <= -0.15 + CCI <= -140
  -> realtime %B <= -0.25 flush 확인
  -> flush_low 이후 3분 안에 %B +0.06 회복
  -> entry %B < -0.15일 때만 매수
  -> %B 0.10 익절권 진입
  -> 30m ema_slope 상승 지속 시 트레일링
  -> %B 0.10 이탈 시 fallback 익절
  -> ema_slope <= -0.55 3분 손절
  -> 60분 시간청산
```

### 통합

```text
하단 BB 터치
  -> Case B / Case C setup 병렬 감시
  -> 먼저 entry 만족한 전략이 포지션 owner
  -> 다른 전략은 신규 진입 중지
  -> Case C 종료 후 realtime %B >= 0.25 회복 확인
  -> 같은 이벤트에서는 Case C 재진입 금지
  -> TP_TRAIL 종료 + exit %B < 0.40일 때만 Case B 판정 유지
  -> 그 외 Case C 종료도 Case B 판정 대기는 유지 가능
  -> Case C 회복조건 만족 후 다시 하단 BB 터치 시 B/C 모두 새 이벤트로 재판정
  -> 상단 BB 터치 시 pending 우선의 lower-BB session 안전 종료
```

---

## 7. 최종 합의된 통합 의도

이 섹션은 Case B와 Case C를 실제로 합칠 때 백테스트 분포를 최대한 덜 훼손하기 위한 최종 합의 사항이다.

### 7.1 핵심 철학

두 전략은 같은 하단 BB 영역을 보지만 성격이 다르다.

```text
Case B:
  하단 BB 터치 후 회복 확인
  -> 이후 위쪽 눌림에서 진입
  -> 비교적 확인매수

Case C:
  하단 BB 과이탈
  -> 더 깊은 flush 확인
  -> 짧은 회복 반등만 포착
  -> 과이탈 칼날잡기
```

따라서 통합 원칙은 다음이다.

```text
setup 감시는 가능한 한 병렬
실제 포지션 owner는 하나
보유 중에는 다른 전략 entry off
매도 후에는 현재 시장 상태를 다시 판정
```

### 7.2 Case B에는 별도 회복 쿨다운이 없다

Case B는 별도의 외부 회복조건을 두지 않는다.

이유:

- Case B 자체가 회복 확인 후 눌림매수하는 구조다.
- 하단 BB 터치 이벤트마다 `BBW < 0.02`이면 다시 Case B 후보가 될 수 있다.
- 이후 30분봉 종가 기준 `%B_close > 0.25` 등 signal 조건을 만족해야만 다음 단계로 넘어간다.

즉 Case B의 회복 필터는 전략 내부에 있다.

```text
새 하단 BB 터치
  -> touch candle BBW < 0.02
  -> Case B ON
  -> 30m close 기준 signal 대기
```

### 7.3 Case C에는 회복조건이 필요하다

Case C는 과이탈 칼날잡기 전략이므로 같은 급락 이벤트에서 반복적으로 켜지면 거래 횟수가 백테스트보다 과도하게 늘어날 수 있다.

따라서 Case C는 같은 과이탈 이벤트 안에서 소모된 뒤, 아래 회복조건 전까지 재가동하지 않는다.

```text
Case C consumed
  -> realtime %B >= 0.25 회복 전까지
  -> Case C 재가동 금지
```

단, 이 회복조건은 포지션이 없을 때만 적용한다.

```text
Case C 매수 전:
  %B >= 0.25
  -> Case C setup 종료 또는 소모 가능

Case C 매수 후:
  %B >= 0.25
  -> 초기화 아님
  -> Case C TP / TP_TRAIL / STOP / TIME까지 계속 관리

Case C 매도 후:
  -> 그때 Case C 초기화
  -> exit_reason과 exit %B로 Case B 인계 여부 판단
```

### 7.4 Case C TP_TRAIL 후 Case B 인계

Case C가 종료되면 Case B 판정 대기는 유지할 수 있다.

그중 가장 적극적으로 Case B에 이어붙일 수 있는 경우는 `TP_TRAIL` 종료이며, 매도 시점 `%B < 0.40`인 경우다.

```text
Case C exit_reason == TP_TRAIL
AND case_c_exit_pct_b < 0.40
  -> Case C는 같은 이벤트에서 재진입 금지
  -> Case B 판정만 ON
  -> 다음 하단 BB 터치 전까지 Case B signal/pullback 감시
```

의도:

- Case C의 `TP_TRAIL`은 전체 Case C 백테스트 중 약 10% 수준이다.
- 평균 매도 `%B`가 `0.2577`로 Case B 회복 신호 영역과 가깝다.
- `%B < 0.40`이면 아직 상단 추세라기보다 하단 회복 이후 눌림 가능성이 남아 있다고 본다.
- `TP_FALLBACK`, `STOP`, `TIME` 종료도 Case B 판정 대기는 유지할 수 있다.
- 단, 이 경우 Case C는 `%B >= 0.25` 회복 전까지 잠근다.
- Case B 확정 전에 `%B >= 0.25` 회복 후 다시 하단 BB를 터치하면, 처음 하단 BB 터치처럼 Case B와 Case C를 둘 다 다시 켠다.

### 7.5 Case B 보유 중 Case C 조건이 보일 때

Case B가 먼저 매수되어 포지션 owner가 된 상태에서는 Case C가 개입하지 않는다.

```text
Case B POSITION_OPEN
  -> Case C entry OFF
  -> Case C setup 기록은 가능
  -> Case C 매수 권한 없음
  -> Case B 익절/손절/비상손절/시간청산만 적용
```

이유:

- Case B 포지션 중 Case C 추가 매수를 허용하면 독립 백테스트 분포가 깨진다.
- 특히 Case B 비상손절 부근에서는 Case C setup처럼 보이는 과이탈 조건이 동시에 나타날 수 있다.
- 이때는 Case C로 갈아타는 것이 아니라 Case B 손절 규칙을 먼저 완료한다.

### 7.6 Case B 손절 후에는 새 이벤트로 재판정한다

백테스트와 비슷하게 만들려면, Case B 손절 후에도 Case C를 계속 꺼두면 안 된다.

Case B 보유 중에는 중복 진입 방지를 위해 Case C를 off한다. 그러나 Case B가 손절 또는 비상손절로 매도된 뒤에는 포지션 owner가 사라졌으므로, 그 직후 시장 상태를 새 하단 BB 터치 이벤트로 다시 판정한다.

```text
Case B STOP 또는 EMERGENCY_STOP 매도 완료
  -> owner = None
  -> if current_price <= lower_band
     or confirmed_30m_low <= lower_band:
       start_new_lower_event()
       if touch_candle_bbw < 0.02:
           Case B monitor ON
       Case C monitor ON
```

의도:

- 단독 백테스트에서 Case C는 Case B 포지션 존재 여부를 알지 못하고 과이탈 setup을 독립적으로 센다.
- 통합 실거래에서는 보유 중 중복 매수만 막는다.
- 매도 후에도 Case C를 계속 막으면 Case C 백테스트 기회가 과도하게 줄어든다.
- 따라서 “보유 중 off, 손절 매도 후 새 이벤트 재판정”이 백테스트 case 분포에 가장 가깝다.

### 7.7 최종 상태 규칙 한 줄 요약

```text
Case B:
  회복 쿨다운 없음.
  하단 BB 터치 이벤트마다 BBW < 0.02이면 다시 판단.

Case C:
  같은 과이탈 이벤트 안에서는 %B >= 0.25 회복 전까지 재가동 금지.
  단, 포지션 보유 중에는 %B 0.25를 초기화 조건으로 쓰지 않음.

통합:
  setup은 병렬, entry는 단일 owner.
  보유 중 다른 전략 entry off.
  손절/매도 후 현재 상태가 하단 BB 이벤트면 B/C 다시 재판정.
```
