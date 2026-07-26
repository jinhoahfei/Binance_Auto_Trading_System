# UPBIT_TRADE 4H Regime 판단 및 30m 실행 게이트 설계

## 1. 기본 구조

REGIME 추천 판단은 4H 기준으로 한다.

실제 적용 REGIME은 사용자가 GUI 버튼으로 직접 선택한다.

실제 매매 실행은 30m 로직에서 수행한다.

핵심 원칙:

```text
TYPE 1 약상승 / TYPE 0 횡보 / TYPE 3 약하락
-> 선택 즉시 해당 30m 로직 실행

TYPE 2 강상승 / TYPE 4 강하락
-> 선택 즉시 실행하지 않음
-> 버튼 ON 이후 내부 30m 하위 신호 게이트를 통해 실행
```

REGIME 추천 로직은 장세 분류까지만 담당한다.

TYPE 2/4 내부의 볼린저밴드 상하단 접촉, EMA9 근처 조건은 추천 로직이 아니라 버튼 선택 이후 실행 게이트로만 사용한다.

## 2. 4H EMA9 LR Slope 계산

REGIME 판단의 핵심 기준선은 4H EMA9이다.

EMA20은 4H 기준에서 너무 늦게 반응할 수 있으므로, 강추세 눌림/반등 판단에는 EMA9를 사용한다.

계산식:

```text
4H EMA9 LR slope
= 최근 6개 4H EMA9 값의 선형회귀 기울기 / 현재가 * 100
```

단위:

```text
% / 4H봉
```

의미:

```text
EMA9 slope +0.30%/봉
= 4H EMA9가 평균적으로 4시간마다 현재가 대비 0.30%씩 상승
```

## 3. 4H 스윙 구조

스윙 고점/저점은 4H 캔들 기준으로 계산한다.

초기 기준:

```text
left = 2
right = 2
threshold = 0.3%~0.5%
```

정의:

```text
HH = 최근 스윙 고점이 이전 스윙 고점보다 threshold 이상 높음
HL = 최근 스윙 저점이 이전 스윙 저점보다 threshold 이상 높음

LH = 최근 스윙 고점이 이전 스윙 고점보다 threshold 이상 낮음
LL = 최근 스윙 저점이 이전 스윙 저점보다 threshold 이상 낮음
```

강추세 확인:

```text
강상승 확인 = HH + HL
강하락 확인 = LH + LL
```

## 4. 4H REGIME 추천 조건

실시간 기준값:

```text
current_price = 현재가
live_4h_ema9 = 실시간 4H EMA9
```

결정 트리:

```text
if ema9_slope >= +0.30:
    if HH + HL and current_price >= live_4h_ema9:
        추천 REGIME = TYPE 2 강상승
    else:
        추천 REGIME = TYPE 1 약상승

elif +0.15 < ema9_slope < +0.30:
    추천 REGIME = TYPE 1 약상승

elif -0.15 <= ema9_slope <= +0.15:
    추천 REGIME = TYPE 0 횡보

elif -0.30 < ema9_slope < -0.15:
    추천 REGIME = TYPE 3 약하락

elif ema9_slope <= -0.30:
    if LH + LL and current_price <= live_4h_ema9:
        추천 REGIME = TYPE 4 강하락
    else:
        추천 REGIME = TYPE 3 약하락
```

핵심 철학:

```text
횡보는 넓게 잡는다: -0.15%~+0.15%
약추세는 EMA9 slope만으로 판단한다.
강추세는 EMA9 slope + 스윙 구조 + 실시간 EMA9 위치가 필요하다.
```

## 5. 사용자가 REGIME 선택 후 동작

```text
TYPE 1 약상승 선택
-> 즉시 30m 약상승 로직 실행

TYPE 0 횡보 선택
-> 즉시 30m 횡보 로직 실행

TYPE 3 약하락 선택
-> 즉시 30m 약하락 로직 실행
```

```text
TYPE 2 강상승 선택
-> TYPE2_MASTER = ON
-> 30m 강상승 로직 전체를 즉시 실행하지 않음
-> 내부 하위 신호 게이트가 열릴 때 해당 30m 로직 실행

TYPE 4 강하락 선택
-> TYPE4_MASTER = ON
-> 30m 강하락 로직 전체를 즉시 실행하지 않음
-> 내부 하위 신호 게이트가 열릴 때 해당 30m 로직 실행
```

## 6. 4H Bollinger %B 계산

4H Bollinger Band 기준:

```text
bb_upper = 4H Bollinger upper
bb_lower = 4H Bollinger lower
bb_width = bb_upper - bb_lower
```

현재가의 %B:

```text
price_pb = (current_price - bb_lower) / bb_width
```

EMA9의 %B:

```text
ema9_pb = (live_4h_ema9 - bb_lower) / bb_width
```

주의:

```text
0.05는 0.05%가 아니다.
0.05는 Bollinger %B 기준 0.05, 즉 볼밴 폭의 5% 위치 차이다.
```

## 7. TYPE 2 강상승 내부 30m 하위 신호

TYPE 2 강상승 버튼을 누르면 상위 신호가 켜진다.

```text
TYPE2_MASTER = ON
```

TYPE 2 내부에는 2개의 30m 하위 신호가 있다.

```text
TYPE2_UPPER_TOUCH_30M_SIGNAL
TYPE2_EMA9_PULLBACK_30M_SIGNAL
```

### 7.1 TYPE2_UPPER_TOUCH_30M_SIGNAL

ON 조건:

```text
price_pb >= 0.95
또는
4H high >= 4H BB upper
```

역할:

```text
4H 상단 접촉 이후 강상승 상단 관리/돌파/눌림 감시 로직을 연다.
추격 매수용 신호가 아니라 상단 구간 관리 신호다.
```

### 7.2 TYPE2_EMA9_PULLBACK_30M_SIGNAL

ON 조건:

```text
price_pb <= ema9_pb + 0.05
```

역할:

```text
현재가가 4H Bollinger %B 기준으로 실시간 4H EMA9 위치보다 0.05 위 이내까지 눌렸을 때
30m 강상승 눌림 매수 로직을 연다.
```

예:

```text
ema9_pb = 0.50
price_pb <= 0.55
-> 실행 가능
```

### 7.3 TYPE 2 하위 신호 유지

각 조건을 한 번 만족하면 해당 하위 신호를 ON 상태로 유지한다.

```text
type2_seen_upper_touch = True
type2_seen_ema9_pullback = True
```

둘 다 한 번씩 만족하면 이후에는 두 30m 하위 신호 모두 ON 상태로 둔다.

```text
TYPE2_UPPER_TOUCH_30M_SIGNAL = ON
TYPE2_EMA9_PULLBACK_30M_SIGNAL = ON
```

## 8. TYPE 4 강하락 내부 30m 하위 신호

TYPE 4 강하락 버튼을 누르면 상위 신호가 켜진다.

```text
TYPE4_MASTER = ON
```

TYPE 4 내부에는 2개의 30m 하위 신호가 있다.

```text
TYPE4_LOWER_TOUCH_30M_SIGNAL
TYPE4_EMA9_REBOUND_30M_SIGNAL
```

### 8.1 TYPE4_LOWER_TOUCH_30M_SIGNAL

ON 조건:

```text
price_pb <= 0.05
또는
4H low <= 4H BB lower
```

역할:

```text
4H 하단 접촉 이후 강하락 하단 관리/돌파/반등 감시 로직을 연다.
추격 숏용 신호가 아니라 하단 구간 관리 신호다.
```

### 8.2 TYPE4_EMA9_REBOUND_30M_SIGNAL

ON 조건:

```text
price_pb >= ema9_pb - 0.05
```

역할:

```text
현재가가 4H Bollinger %B 기준으로 실시간 4H EMA9 위치보다 0.05 아래 이내까지 반등했을 때
30m 강하락 반등 숏 로직을 연다.
```

예:

```text
ema9_pb = 0.50
price_pb >= 0.45
-> 실행 가능
```

### 8.3 TYPE 4 하위 신호 유지

각 조건을 한 번 만족하면 해당 하위 신호를 ON 상태로 유지한다.

```text
type4_seen_lower_touch = True
type4_seen_ema9_rebound = True
```

둘 다 한 번씩 만족하면 이후에는 두 30m 하위 신호 모두 ON 상태로 둔다.

```text
TYPE4_LOWER_TOUCH_30M_SIGNAL = ON
TYPE4_EMA9_REBOUND_30M_SIGNAL = ON
```

## 9. TYPE 1 / TYPE 0 / TYPE 3 실행

TYPE 1 약상승:

```text
사용자가 TYPE 1 버튼 선택
-> 30m 약상승 로직 즉시 실행
```

TYPE 0 횡보:

```text
사용자가 TYPE 0 버튼 선택
-> 기존 30m 횡보 로직 즉시 실행
```

TYPE 3 약하락:

```text
사용자가 TYPE 3 버튼 선택
-> 30m 약하락 로직 즉시 실행
```

## 10. 최종 동작 흐름

```text
1. 4H EMA9 LR slope 계산
2. 4H 스윙 구조 HH/HL/LH/LL 확인
3. current_price와 live_4h_ema9 위치 확인
4. 추천 REGIME 산출
5. 사용자가 TYPE 0~4 중 하나 선택
6. TYPE 1/0/3이면 즉시 해당 30m 로직 실행
7. TYPE 2이면 TYPE2_MASTER ON 후 내부 2개 하위 신호 조건을 감시
8. TYPE 4이면 TYPE4_MASTER ON 후 내부 2개 하위 신호 조건을 감시
```

최종 한 문장:

```text
REGIME 추천은 4H EMA9 slope와 구조로 장세만 분류하고,
TYPE 2/4의 볼밴 상하단 접촉 및 EMA9 %B 근처 조건은 버튼 선택 이후 30m 실행 게이트로만 사용한다.
```
