# Trading Logic Event-Action Table



## 1. 상태 구성

### 1.1 최상위 상태

| STATE ID | STM 표기 | 의미 |
| ----------------- | -------------- | ------------------------------------------- |
| LOGIC_ENABLED | Logic 가동 State | 하단 BB 감시와 진입·매매 관리를 포함하는 최상위 복합 상태 |
| LOWER_TOUCH_WATCH | 하단 터치 감시 상태 | 실시간 현재가 또는 확정 30분봉 저가의 하단 BB 접촉을 감시하는 상태 |
| TRADE_MANAGEMENT | 전략 진입 및 매매 관리 | 포지션 영역, Case B 영역, Case C 영역을 병렬 실행하는 복합 상태 |
| UPPER_BB_STATE_MACHINE | 상단 BB 상태 머신 | `realtime_price >= upper_band`이면 하단 BB 로직에서 인계하는 외부 상태 머신 |
| LOGIC_TERMINATED | Final State 기호 | 매매 중지 상태 |


### 1.2 ==병렬 진입·포지션 소유권== 영역 (Region_1)

| 상태 ID | STM 표기 |
| ------------------ | ---------------------------------------- |
| NO_POSITION | 포지션 미보유 |
| CASE_B_POSITION_MANAGEMENT | Case B 포지션 관리 복합 상태 |
| CASE_B_HOLDING | Lower_BB_30M_Pullback (Case_B) 포지션 보유 |
| CASE_B_TREND_HOLD | 강한 반등 상태 (TREND_HOLD) |
| CASE_B_CLOSED | Lower_BB_30M_Pullback (Case_B) 포지션 청산 완료 |
| CASE_C_POSITION_MANAGEMENT | Case C 포지션 관리 복합 상태 |
| CASE_C_HOLDING | Blade_Catching (Case_C) 포지션 보유 |
| CASE_C_TP_TRAILING | 익절권 진입 (TP_TRAILING) |
| CASE_C_CLOSED | Blade_Catching (Case_C) 포지션 청산 완료 및 `%B >= 0.25` 회복 대기 |
| CASE_C_RECOVERY_SUCCEEDED | Case C 청산 후 `realtime_pct_b >= 0.25` 만족 |


### 1.3 Case B 신호 검사 Region

| 상태 ID | STM 표기 |
| ------------------------- | ------------------------------- |
| B_WAIT_TOUCH | Case B 터치봉 조건 대기 |
| B_WAIT_SIGNAL | `touch_candle_bbw < 0.02` 만족 (WAIT_SIGNAL) |
| B_WAIT_PULLBACK | 최초 확정 signal 만족 (WAIT_PULLBACK) |
| B_POSITION_OPEN_SIGNALLED | `3시간 이내 && realtime_pct_b <= 0.30` 만족 (POSITION_OPEN) |
| CASE_B_FINAL_STATE | Case B 신호 검사 종료 |


### 1.4 Case C 신호 검사 Region

| 상태 ID | STM 표기 |
| -------------------- | ----------------------- |
| C_WAIT_SETUP | Case C setup 조건 대기 |
| C_SETUP | `realtime_pct_b <= -0.15 && cci_30m_realtime <= -140` 만족 (SETUP) |
| C_POSITION_OPEN_SIGNALLED | 3분 회복 매수 조건 만족 (POSITION_OPEN) |
| CASE_C_FINAL_STATE | Case C 신호 검사 종료 |


### 1.5 공통 변수·판정 기준

| 변수·판정 | 정의 |
| ------------------------- | ------------------------------------------------------------ |
| `lower_band`, `upper_band` | 30분봉 close 기준 Bollinger Band(20기간, 표준편차 2) |
| `realtime_pct_b` | `(realtime_price - lower_band) / (upper_band - lower_band)` |
| `touch_candle` | 하단 BB를 최초로 터치한 30분봉. `lower_band_at_touch`와 Case B의 `BBW`는 이 봉의 값으로 고정한다. |
| `confirmed_30m_close` | 현재 30분봉이 마감되어 종가와 지표값이 확정된 경우에만 `True` |
| `confirmed_1m_close` | 현재 1분봉이 마감되어 `close_1m`이 확정된 판정 시점에만 `True` |
| `signal_candle` | 하단 터치 뒤 Case B signal 조건을 최초로 모두 만족한 확정 30분봉 |
| `ema_slope_30m_close` | 확정 30분봉 close로 계산한 30분봉 EMA slope |
| `realtime_ema_slope` | 현재가를 진행 중인 30분봉의 임시 close로 넣어 계산한 30분봉 EMA slope |
| `current_close_ema_slope` | 확정 1분봉의 `close_1m`을 진행 중인 30분봉의 임시 close로 넣어 계산한 30분봉 EMA slope |
| `cci_30m_realtime` | 30분봉 표준 CCI(20)를 실시간 현재가까지 반영해 계산한 값 |
| `holding_time` | 매수 체결시각부터 현재 판정시각까지의 경과 시간 |
| `position_owner` | `None`, `CASE_B`, `CASE_C` 중 하나. 매수 체결이 정상 완료된 뒤에만 설정한다. |
| `case_b_entry_paused` | Case C 보유 또는 비적극적 인계 구간에서 Case B 매수만 금지하는 플래그. signal 감시는 유지할 수 있다. |
| `case_b_only_until_next_lower_touch` | Case C를 재가동하지 않고 Case B만 감시하는 구간임을 나타내는 플래그 |
| `allow_new_case_c_setup` | 현재 하단 이벤트에서 새 Case C setup을 만들 수 있는지 나타내는 플래그 |
| `case_c_consumed_for_event` | 같은 하단 터치 이벤트에서 Case C가 종료된 뒤 재진입하지 못하게 하는 플래그 |
| `case_c_recovery_confirmed` | Case C 종료 후 `realtime_pct_b >= 0.25` 회복을 확인했는지 나타내는 플래그 |
| `current_open_pct_b` | Case C의 새 3분 회복 구간을 시작하는 시점의 `realtime_pct_b` 스냅샷 |
| `entry_pct_b` | Case C 회복 진입선. `timer_base_pct_b + 0.06`으로 계산한다. |
| `case_c_exit_reason` | Case C 청산 사유. `TP_TRAIL`, `TP_FALLBACK`, `STOP`, `TIME` 중 하나이다. |
| `case_c_exit_pct_b` | Case C 매도 체결 시점의 `realtime_pct_b` |


## 2. Event-Action Table

### 2.1 최상위 및 공통 Event-Action Table

| ID | 현재 상태 | EVENT | 가드·판정 조건 | Action | 다음 상태 |
| ---- | ----------------- | ------------------- | ------------------------- | ------------------------------------- | ----------------- |
| G-01 | 시작 | 로직 시작 | None | 1) `LOGIC_ENABLED` 초기화, 2) `position_owner = None` | LOWER_TOUCH_WATCH |
| G-02 | LOWER_TOUCH_WATCH | 하단 BB 접촉 | `realtime_price <= lower_band` OR (`confirmed_30m_close == True` && `current_30m_low <= lower_band`) | 1) `lower_event_id`, 터치 시각, `touch_candle`, `touch_candle_bbw` 저장, 2) `case_b_enabled = (touch_candle_bbw < 0.02)`, 3) `case_c_enabled = True`, `allow_new_case_c_setup = True`, 4) `case_c_consumed_for_event = False`, `case_c_recovery_confirmed = False`, `case_c_exit_reason = None`, `case_c_exit_pct_b = None`, 5) `case_b_entry_paused = False`, `case_b_only_until_next_lower_touch = False`, 6) ACTIVATE_TRADE_MANAGEMENT EVENT 발생 | TRADE_MANAGEMENT |
| G-03 | TRADE_MANAGEMENT | 새 30분봉 하단 BB 접촉 | `position_owner == None` && `current_30m_candle_id != touch_candle_id` && `current_30m_low <= lower_band` && (`case_c_consumed_for_event == False` OR `case_c_recovery_confirmed == True`) | 1) 기존 하단 터치 이벤트 종료, 2) 현재 30분봉을 `touch_candle`로 하여 새 `lower_event_id` 생성, 3) Case B/Case C 상태와 G-02의 플래그 재초기화, 4) ACTIVATE_TRADE_MANAGEMENT EVENT 재발생 | TRADE_MANAGEMENT |
| G-04 | TRADE_MANAGEMENT | TRADE_MANAGEMENT 완료 | `position_owner == None` && `Case B Region == CASE_B_FINAL_STATE` && `Case C Region == CASE_C_FINAL_STATE` | 1) 현재 하단 이벤트 종료, 2) 하단 터치 감시 재개 | LOWER_TOUCH_WATCH |
| G-05 | 임의의 무포지션 상태 | 매매 중지 | `position_owner == None` | 로직을 종료한다. | LOGIC_TERMINATED |
| G-06 | 임의의 포지션 보유 상태 | 매매 중지[포지션 보유] | `position_owner` in `{CASE_B, CASE_C}` | 1) 보유 포지션 강제 매도, 2) 주문 결과와 체결 정보 저장, 3) 로직 중지 | LOGIC_TERMINATED |
| G-07 | TRADE_MANAGEMENT의 임의 상태 | UPPER_BAND_TOUCHED | `realtime_price >= upper_band` | 1) 하단 BB 신규 진입 중지, 2) 현재 `position_owner`와 포지션 관리 책임을 상단 BB 정책에 인계, 3) 하단 이벤트 상태 초기화 | UPPER_BB_STATE_MACHINE |



### 2.2 병렬 진입·포지션 소유권 Event-Action Table(Region 1)

| ID | 현재 상태 | EVENT | 가드 | Action | 다음 상태 |
| ----- | -------------- | ---------------------------- | ---- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------- |
| O-01 | 포지션 영역 시작 | 초기 진입 | None | `position_owner = None` | NO_POSITION |
| O-02 | NO_POSITION | CASE_B_BUY | `position_owner == None` && `case_b_entry_paused == False` && Case B 매수 주문 정상 완료 | 1) Case B 매수 실행 및 정상 체결 확인, 2) `position_owner = CASE_B`, 3) 체결시각, 진입 당시 ETH가격, 체결가, ETH 수량, 주문 금액, 수수료 저장, 4) CASE_B_POSITION_OPENED 및 START_CASE_B_CONDITION_CHECK EVENT 발생 | CASE_B_POSITION_MANAGEMENT |
| O-03 | NO_POSITION | CASE_B_BUY | `position_owner == None` && `case_b_entry_paused == False` && Case B 매수 주문 비정상 완료 | 1) `position_owner = None` 유지, 2) CASE_B_BUY_RETRY EVENT 발생 | NO_POSITION |
| O-04 | NO_POSITION | CASE_B_BUY_RETRY | `position_owner == None` && `case_b_entry_paused == False` && Case B 재매수 주문 정상 완료 | 1) Case B 재매수 실행 및 정상 체결 확인, 2) `position_owner = CASE_B`, 3) 체결 정보 저장, 4) CASE_B_POSITION_OPENED 및 START_CASE_B_CONDITION_CHECK EVENT 발생 | CASE_B_POSITION_MANAGEMENT |
| O-05 | NO_POSITION | CASE_B_BUY_RETRY | `position_owner == None` && `case_b_entry_paused == False` && Case B 재매수 주문 비정상 완료 | 1) `position_owner = None` 유지, 2) CASE_B_BUY_RETRY EVENT 재발생 | NO_POSITION |
| O-06 | NO_POSITION | CASE_C_BUY | `position_owner == None` && Case C 매수 주문 정상 완료 | 1) Case C 매수 실행 및 정상 체결 확인, 2) `position_owner = CASE_C`, 3) 체결시각, 진입 당시 ETH가격, 체결가, ETH 수량, 주문 금액, 수수료 저장, 4) `case_b_entry_paused = True`, 5) CASE_C_POSITION_OPENED 및 START_CASE_C_CONDITION_CHECK EVENT 발생 | CASE_C_POSITION_MANAGEMENT |
| O-07 | NO_POSITION | CASE_C_BUY | `position_owner == None` && Case C 매수 주문 비정상 완료 | 1) `position_owner = None` 유지, 2) CASE_C_BUY_RETRY EVENT 발생 | NO_POSITION |
| O-08 | NO_POSITION | CASE_C_BUY_RETRY | `position_owner == None` && Case C 재매수 주문 정상 완료 | 1) Case C 재매수 실행 및 정상 체결 확인, 2) `position_owner = CASE_C`, 3) 체결 정보 저장, 4) `case_b_entry_paused = True`, 5) CASE_C_POSITION_OPENED 및 START_CASE_C_CONDITION_CHECK EVENT 발생 | CASE_C_POSITION_MANAGEMENT |
| O-09 | NO_POSITION | CASE_C_BUY_RETRY | `position_owner == None` && Case C 재매수 주문 비정상 완료 | 1) `position_owner = None` 유지, 2) CASE_C_BUY_RETRY EVENT 재발생 | NO_POSITION |

동일 평가 주기에 `case_c_buy_signal`과 `case_b_buy_signal`이 함께 참이면 통합 의사코드의 `if Case C ... elif Case B ...` 순서에 따라 Case C가 owner를 선점한다. 어느 경우에도 매수 체결 전에는 `position_owner`를 설정하지 않는다.


#### 2.2.1 CASE_B_POSITION_MANAGEMENT Event-Action Table

| ID | 현재 상태 | EVENT | Guard | Action | 다음 상태 |
| ----- | ----------------------------------- | --------------- | ------------------------------------------------------------------- | ------------------------------------------------------ | --------------------------------------- |
| PB-01 | Initial Pseudo State | 초기 진입 | None | None | CASE_B_HOLDING |
| PB-02 | CASE_B_HOLDING | START_CASE_B_CONDITION_CHECK | `realtime_price > entry_price * 0.99` && NOT (`confirmed_30m_close == True` && `ema_slope_30m_close < -0.08`) && NOT (`realtime_pct_b >= 0.60` 5초 유지 && `realtime_ema_slope <= 0.08`) && NOT (`realtime_pct_b >= 0.60` 5초 유지 && `realtime_ema_slope > 0.08` 5초 유지) && `holding_time < 6시간` | RETRY_CASE_B_CONDITION_CHECK EVENT 발생 | CASE_B_HOLDING |
| PB-03 | CASE_B_HOLDING | START_CASE_B_CONDITION_CHECK | PB-02의 부정, 즉 비상손절·일반손절·Trend Hold 전환·일반익절·시간청산 중 하나 이상이 참 | 우선순위에 따라 1) `realtime_price <= entry_price * 0.99`이면 CASE_B_EMERGENCY_STOP, 2) `confirmed_30m_close == True && ema_slope_30m_close < -0.08`이면 CASE_B_STOP, 3) `%B >= 0.60` 5초 및 slope `> 0.08` 5초이면 CASE_B_UPPER_TREND, 4) `%B >= 0.60` 5초 및 slope `<= 0.08`이면 CASE_B_TAKE_PROFIT, 5) `holding_time >= 6시간`이면 CASE_B_TIME_EXIT EVENT 발생 | CASE_B_HOLDING |
| PB-04 | CASE_B_HOLDING | RETRY_CASE_B_CONDITION_CHECK | PB-02와 동일 | RETRY_CASE_B_CONDITION_CHECK EVENT 재발생 | CASE_B_HOLDING |
| PB-05 | CASE_B_HOLDING | RETRY_CASE_B_CONDITION_CHECK | PB-03과 동일 | PB-03의 우선순위와 조건식을 그대로 적용하여 해당 EVENT 발생 | CASE_B_HOLDING |
| PB-06 | CASE_B_HOLDING | CASE_B_EMERGENCY_STOP(비상 손절) | `realtime_price <= entry_price * 0.99` && Case B 매도 주문 정상 완료 | 1) `exit_reason = EMERGENCY_STOP`, 2) Case B 매도 실행 및 정상 체결 확인, 3) 체결시각, 매도 당시 ETH가격, 체결가, ETH 수량, 주문 금액, 수수료 저장, 4) CASE_B_SELL_FINISHED EVENT 발생 | CASE_B_CLOSED |
| PB-07 | CASE_B_HOLDING | CASE_B_EMERGENCY_STOP(비상 손절) | `realtime_price <= entry_price * 0.99` && Case B 매도 주문 비정상 완료 | 1) `pending_exit_reason = EMERGENCY_STOP`, 2) CASE_B_SELL_RETRY EVENT 발생 | CASE_B_HOLDING |
| PB-08 | CASE_B_HOLDING | CASE_B_STOP(일반 손절) | `confirmed_30m_close == True` && `ema_slope_30m_close < -0.08` && Case B 매도 주문 정상 완료 | 1) `exit_reason = STOP`, 2) Case B 매도 실행 및 정상 체결 확인, 3) 체결 정보 저장, 4) CASE_B_SELL_FINISHED EVENT 발생 | CASE_B_CLOSED |
| PB-09 | CASE_B_HOLDING | CASE_B_STOP(일반 손절) | `confirmed_30m_close == True` && `ema_slope_30m_close < -0.08` && Case B 매도 주문 비정상 완료 | 1) `pending_exit_reason = STOP`, 2) CASE_B_SELL_RETRY EVENT 발생 | CASE_B_HOLDING |
| PB-10 | CASE_B_HOLDING | CASE_B_TIME_EXIT(시간 청산) | `holding_time >= 6시간` && Case B 매도 주문 정상 완료 | 1) `exit_reason = TIME`, 2) Case B 매도 실행 및 정상 체결 확인, 3) 체결 정보 저장, 4) CASE_B_SELL_FINISHED EVENT 발생 | CASE_B_CLOSED |
| PB-11 | CASE_B_HOLDING | CASE_B_TIME_EXIT(시간 청산) | `holding_time >= 6시간` && Case B 매도 주문 비정상 완료 | 1) `pending_exit_reason = TIME`, 2) CASE_B_SELL_RETRY EVENT 발생 | CASE_B_HOLDING |
| PB-12 | CASE_B_HOLDING | CASE_B_TAKE_PROFIT(일반 익절) | `realtime_pct_b >= 0.60` 5초 유지 && `realtime_ema_slope <= 0.08` && Case B 매도 주문 정상 완료 | 1) `exit_reason = TAKE_PROFIT`, 2) Case B 매도 실행 및 정상 체결 확인, 3) 체결 정보 저장, 4) CASE_B_SELL_FINISHED EVENT 발생 | CASE_B_CLOSED |
| PB-13 | CASE_B_HOLDING | CASE_B_TAKE_PROFIT(일반 익절) | `realtime_pct_b >= 0.60` 5초 유지 && `realtime_ema_slope <= 0.08` && Case B 매도 주문 비정상 완료 | 1) `pending_exit_reason = TAKE_PROFIT`, 2) CASE_B_SELL_RETRY EVENT 발생 | CASE_B_HOLDING |
| PB-14 | CASE_B_HOLDING | CASE_B_UPPER_TREND(강한 반등) | `realtime_pct_b >= 0.60` 5초 유지 && `realtime_ema_slope > 0.08` 5초 유지 | CASE_B_TREND_HOLD_CONDITION_CHECK EVENT 발생 | CASE_B_TREND_HOLD |
| PB-15 | CASE_B_TREND_HOLD | CASE_B_TREND_HOLD_CONDITION_CHECK | NOT (`realtime_ema_slope <= 0.04` 5초 유지) && NOT (`realtime_pct_b < 0.60` 5초 유지) | CASE_B_TREND_HOLD_CONDITION_CHECK EVENT 재발생 | CASE_B_TREND_HOLD |
| PB-16 | CASE_B_TREND_HOLD | CASE_B_TREND_HOLD_CONDITION_CHECK | `realtime_ema_slope <= 0.04` 5초 유지 OR `realtime_pct_b < 0.60` 5초 유지 | CASE_B_TREND_HOLD_SELL EVENT 발생 | CASE_B_TREND_HOLD |
| PB-17 | CASE_B_TREND_HOLD | CASE_B_TREND_HOLD_SELL | (`realtime_ema_slope <= 0.04` 5초 유지 OR `realtime_pct_b < 0.60` 5초 유지) && Case B 매도 주문 정상 완료 | 1) `exit_reason = TREND_HOLD`, 2) Case B 매도 실행 및 정상 체결 확인, 3) 체결 정보 저장, 4) CASE_B_SELL_FINISHED EVENT 발생 | CASE_B_CLOSED |
| PB-18 | CASE_B_TREND_HOLD | CASE_B_TREND_HOLD_SELL | (`realtime_ema_slope <= 0.04` 5초 유지 OR `realtime_pct_b < 0.60` 5초 유지) && Case B 매도 주문 비정상 완료 | 1) `pending_exit_reason = TREND_HOLD`, 2) CASE_B_SELL_RETRY EVENT 발생 | CASE_B_TREND_HOLD |
| PB-19 | CASE_B_HOLDING | CASE_B_SELL_RETRY | `pending_exit_reason` in `{EMERGENCY_STOP, STOP, TIME, TAKE_PROFIT}` && 재매도 주문 정상 완료 | 1) `exit_reason = pending_exit_reason`, 2) Case B 재매도 실행 및 정상 체결 확인, 3) 체결 정보 저장, 4) CASE_B_SELL_FINISHED EVENT 발생 | CASE_B_CLOSED |
| PB-20 | CASE_B_HOLDING | CASE_B_SELL_RETRY | `pending_exit_reason` in `{EMERGENCY_STOP, STOP, TIME, TAKE_PROFIT}` && 재매도 주문 비정상 완료 | CASE_B_SELL_RETRY EVENT 재발생 | CASE_B_HOLDING |
| PB-21 | CASE_B_TREND_HOLD | CASE_B_SELL_RETRY | `pending_exit_reason == TREND_HOLD` && 재매도 주문 정상 완료 | 1) `exit_reason = TREND_HOLD`, 2) Case B 재매도 실행 및 정상 체결 확인, 3) 체결 정보 저장, 4) CASE_B_SELL_FINISHED EVENT 발생 | CASE_B_CLOSED |
| PB-22 | CASE_B_TREND_HOLD | CASE_B_SELL_RETRY | `pending_exit_reason == TREND_HOLD` && 재매도 주문 비정상 완료 | CASE_B_SELL_RETRY EVENT 재발생 | CASE_B_TREND_HOLD |
| PB-23 | CASE_B_CLOSED | CASE_B_SELL_FINISHED | `exit_reason` in `{STOP, EMERGENCY_STOP}` && (`realtime_price <= lower_band` OR (`confirmed_30m_close == True` && `current_30m_low <= lower_band`)) | 1) `position_owner = None`, 2) 현재 시점 또는 해당 확정봉을 새 `touch_candle`로 하여 새 하단 이벤트 생성, 3) `case_b_enabled = (touch_candle_bbw < 0.02)`, `case_c_enabled = True`, `allow_new_case_c_setup = True`, 4) `case_c_consumed_for_event = False`, `case_c_recovery_confirmed = False`, `case_c_exit_reason = None`, `case_c_exit_pct_b = None`, 5) `case_b_entry_paused = False`, `case_b_only_until_next_lower_touch = False`, 6) ACTIVATE_TRADE_MANAGEMENT EVENT 발생 | TRADE_MANAGEMENT |
| PB-24 | CASE_B_CLOSED | CASE_B_SELL_FINISHED | NOT (PB-23의 Guard) | 1) `position_owner = None`, 2) Case B 상태 초기화, 3) 현재 하단 이벤트 종료 | LOWER_TOUCH_WATCH |


##### Case B 청산 판정 우선순위

1. 비상 손절
2. 확정 30분봉 일반 손절
3. `realtime_pct_b >= 0.60` 5초 유지 시 `realtime_ema_slope > 0.08`의 5초 유지 여부로 Trend Hold/일반 익절 분기
4. 일반 익절
5. 시간 청산
6. 공통 G-07의 `realtime_price >= upper_band` 상단 BB 인계

`realtime_ema_slope`는 현재가를 임시 30분봉 close로 넣어 계산한다. 동일 평가 주기에 여러 조건이 참이면 위 순서대로 하나의 EVENT만 발생시킨다.

#### 2.2.2 CASE_C_POSITION_MANAGEMENT Event-Action Table

| ID | 현재 상태 | EVENT | Guard | Action | 다음 상태 |
| ----- | ------------------ | --------------------- | --------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ | ------------------------------------------ |
| PC-01 | Initial Pseudo State | 초기 진입 | None | None | CASE_C_HOLDING |
| PC-02 | CASE_C_HOLDING | START_CASE_C_CONDITION_CHECK | `realtime_pct_b < 0.10` && NOT (`ema_slope_30m_realtime <= -0.55` 3분 연속 유지) && `holding_time < 60분` | RETRY_CASE_C_CONDITION_CHECK EVENT 발생 | CASE_C_HOLDING |
| PC-03 | CASE_C_HOLDING | START_CASE_C_CONDITION_CHECK | `realtime_pct_b >= 0.10` OR `ema_slope_30m_realtime <= -0.55` 3분 연속 유지 OR `holding_time >= 60분` | 우선순위에 따라 1) `%B >= 0.10`이면 CASE_C_ENTER_PROFIT_ZONE, 2) slope `<= -0.55` 3분 유지이면 CASE_C_STOP, 3) `holding_time >= 60분`이면 CASE_C_TIME_EXIT EVENT 발생 | CASE_C_HOLDING |
| PC-04 | CASE_C_HOLDING | RETRY_CASE_C_CONDITION_CHECK | PC-02와 동일 | RETRY_CASE_C_CONDITION_CHECK EVENT 재발생 | CASE_C_HOLDING |
| PC-05 | CASE_C_HOLDING | RETRY_CASE_C_CONDITION_CHECK | PC-03과 동일 | PC-03의 우선순위와 조건식을 그대로 적용하여 해당 EVENT 발생 | CASE_C_HOLDING |
| PC-06 | CASE_C_HOLDING | CASE_C_STOP(손절) | `ema_slope_30m_realtime <= -0.55` 3분 연속 유지 && Case C 매도 주문 정상 완료 | 1) `case_c_exit_reason = STOP`, 2) Case C 매도 실행 및 정상 체결 확인, 3) `case_c_exit_pct_b`와 체결시각, 매도 당시 ETH가격, 체결가, ETH 수량, 주문 금액, 수수료 저장, 4) CASE_C_SELL_FINISHED EVENT 발생 | CASE_C_CLOSED |
| PC-07 | CASE_C_HOLDING | CASE_C_STOP(손절) | `ema_slope_30m_realtime <= -0.55` 3분 연속 유지 && Case C 매도 주문 비정상 완료 | 1) `pending_exit_reason = STOP`, `pending_return_state = CASE_C_HOLDING`, 2) CASE_C_SELL_RETRY EVENT 발생 | CASE_C_HOLDING |
| PC-08 | CASE_C_HOLDING 또는 CASE_C_TP_TRAILING | CASE_C_TIME_EXIT(시간 청산) | `holding_time >= 60분` && Case C 매도 주문 정상 완료 | 1) `case_c_exit_reason = TIME`, 2) Case C 매도 실행 및 정상 체결 확인, 3) `case_c_exit_pct_b`와 체결 정보 저장, 4) CASE_C_SELL_FINISHED EVENT 발생 | CASE_C_CLOSED |
| PC-09 | CASE_C_HOLDING 또는 CASE_C_TP_TRAILING | CASE_C_TIME_EXIT(시간 청산) | `holding_time >= 60분` && Case C 매도 주문 비정상 완료 | 1) `pending_exit_reason = TIME`, 2) `pending_return_state`에 청산 시도 전 상태 저장, 3) CASE_C_SELL_RETRY EVENT 발생 | `pending_return_state` |
| PC-10 | CASE_C_HOLDING | CASE_C_ENTER_PROFIT_ZONE(익절권 진입) | `realtime_pct_b >= 0.10` | 1) 익절권 진입 시점의 밴드로 `tp_price = lower_band + 0.10 * (upper_band - lower_band)`, 2) `previous_trail_ema_slope = ema_slope_30m(tp_price)`, 3) START_TP_TRAILING_CONDITION_CHECK EVENT 발생 | CASE_C_TP_TRAILING |
| PC-11 | CASE_C_TP_TRAILING | START_TP_TRAILING_CONDITION_CHECK | `realtime_pct_b >= 0.10` && `holding_time < 60분` && `confirmed_1m_close == False` | RETRY_TP_TRAILING_CONDITION_CHECK EVENT 발생 | CASE_C_TP_TRAILING |
| PC-12 | CASE_C_TP_TRAILING | START_TP_TRAILING_CONDITION_CHECK | `realtime_pct_b < 0.10` OR `confirmed_1m_close == True` OR `holding_time >= 60분` | 우선순위에 따라 1) `%B < 0.10`이면 CASE_C_SELL_AT_TP_PRICE, 2) 확정 1분봉이면 `current_close_ema_slope = ema_slope_30m(close_1m)` 계산 후 `current_close_ema_slope > previous_trail_ema_slope`이면 CASE_C_EMA_INCREASEMENT, 3) `current_close_ema_slope <= previous_trail_ema_slope`이면 CASE_C_EMA_DECREASEMENT, 4) `holding_time >= 60분`이면 CASE_C_TIME_EXIT EVENT 발생 | CASE_C_TP_TRAILING |
| PC-13 | CASE_C_TP_TRAILING | RETRY_TP_TRAILING_CONDITION_CHECK | PC-11과 동일 | RETRY_TP_TRAILING_CONDITION_CHECK EVENT 재발생 | CASE_C_TP_TRAILING |
| PC-14 | CASE_C_TP_TRAILING | RETRY_TP_TRAILING_CONDITION_CHECK | PC-12와 동일 | PC-12의 우선순위와 조건식을 그대로 적용하여 해당 EVENT 발생 | CASE_C_TP_TRAILING |
| PC-15 | CASE_C_TP_TRAILING | CASE_C_EMA_INCREASEMENT(30m EMA slope 증가) | `confirmed_1m_close == True` && `current_close_ema_slope > previous_trail_ema_slope` | 1) `previous_trail_ema_slope = current_close_ema_slope`, 2) RETRY_TP_TRAILING_CONDITION_CHECK EVENT 발생 | CASE_C_TP_TRAILING |
| PC-16 | CASE_C_TP_TRAILING | CASE_C_SELL_AT_TP_PRICE(회복 강도 약화) | `realtime_pct_b < 0.10` && Case C 매도 주문 정상 완료 | 1) `case_c_exit_reason = TP_FALLBACK`, 2) Case C 매도 실행 및 정상 체결 확인, 3) `case_c_exit_pct_b`와 체결 정보 저장, 4) CASE_C_SELL_FINISHED EVENT 발생 | CASE_C_CLOSED |
| PC-17 | CASE_C_TP_TRAILING | CASE_C_SELL_AT_TP_PRICE(회복 강도 약화) | `realtime_pct_b < 0.10` && Case C 매도 주문 비정상 완료 | 1) `pending_exit_reason = TP_FALLBACK`, `pending_return_state = CASE_C_TP_TRAILING`, 2) CASE_C_SELL_RETRY EVENT 발생 | CASE_C_TP_TRAILING |
| PC-18 | CASE_C_TP_TRAILING | CASE_C_EMA_DECREASEMENT(30m EMA slope 비증가) | `confirmed_1m_close == True` && `current_close_ema_slope <= previous_trail_ema_slope` && Case C 매도 주문 정상 완료 | 1) `case_c_exit_reason = TP_TRAIL`, 2) 확정 1분봉 close 시점에 Case C 매도 실행 및 정상 체결 확인, 3) `case_c_exit_pct_b`와 체결 정보 저장, 4) CASE_C_SELL_FINISHED EVENT 발생 | CASE_C_CLOSED |
| PC-19 | CASE_C_TP_TRAILING | CASE_C_EMA_DECREASEMENT(30m EMA slope 비증가) | `confirmed_1m_close == True` && `current_close_ema_slope <= previous_trail_ema_slope` && Case C 매도 주문 비정상 완료 | 1) `pending_exit_reason = TP_TRAIL`, `pending_return_state = CASE_C_TP_TRAILING`, 2) CASE_C_SELL_RETRY EVENT 발생 | CASE_C_TP_TRAILING |
| PC-20 | CASE_C_HOLDING | CASE_C_SELL_RETRY | `pending_return_state == CASE_C_HOLDING` && 재매도 주문 정상 완료 | 1) `case_c_exit_reason = pending_exit_reason`, 2) Case C 재매도 실행 및 정상 체결 확인, 3) `case_c_exit_pct_b`와 체결 정보 저장, 4) CASE_C_SELL_FINISHED EVENT 발생 | CASE_C_CLOSED |
| PC-21 | CASE_C_HOLDING | CASE_C_SELL_RETRY | `pending_return_state == CASE_C_HOLDING` && 재매도 주문 비정상 완료 | CASE_C_SELL_RETRY EVENT 재발생 | CASE_C_HOLDING |
| PC-22 | CASE_C_TP_TRAILING | CASE_C_SELL_RETRY | `pending_return_state == CASE_C_TP_TRAILING` && 재매도 주문 정상 완료 | 1) `case_c_exit_reason = pending_exit_reason`, 2) Case C 재매도 실행 및 정상 체결 확인, 3) `case_c_exit_pct_b`와 체결 정보 저장, 4) CASE_C_SELL_FINISHED EVENT 발생 | CASE_C_CLOSED |
| PC-23 | CASE_C_TP_TRAILING | CASE_C_SELL_RETRY | `pending_return_state == CASE_C_TP_TRAILING` && 재매도 주문 비정상 완료 | CASE_C_SELL_RETRY EVENT 재발생 | CASE_C_TP_TRAILING |
| PC-24 | CASE_C_CLOSED | CASE_C_SELL_FINISHED | None | 1) `position_owner = None`, 2) `case_c_consumed_for_event = True`, `allow_new_case_c_setup = False`, 3) `case_b_entry_paused = True`, 4) CHECK_CASE_C_RECOVERY EVENT 발생 | CASE_C_CLOSED |
| PC-25 | CASE_C_CLOSED | CHECK_CASE_C_RECOVERY | `realtime_pct_b < 0.25` | CHECK_CASE_C_RECOVERY EVENT 재발생 | CASE_C_CLOSED |
| PC-26 | CASE_C_CLOSED | CHECK_CASE_C_RECOVERY | `realtime_pct_b >= 0.25` | 1) `case_c_recovery_confirmed = True`, 2) CHECK_CASE_B_HANDOFF EVENT 발생 | CASE_C_RECOVERY_SUCCEEDED |
| PC-27 | CASE_C_RECOVERY_SUCCEEDED | CHECK_CASE_B_HANDOFF | `case_c_exit_reason == TP_TRAIL` && `case_c_exit_pct_b < 0.40` | 1) `case_b_entry_paused = False`, 2) `case_b_only_until_next_lower_touch = True`, 3) 같은 이벤트의 `allow_new_case_c_setup = False` 유지, 4) CASE_B_ACTIVE_RESUME EVENT 발생 | NO_POSITION |
| PC-28 | CASE_C_RECOVERY_SUCCEEDED | CHECK_CASE_B_HANDOFF | NOT (`case_c_exit_reason == TP_TRAIL` && `case_c_exit_pct_b < 0.40`) | 1) `case_b_entry_paused = True`, 2) `case_b_only_until_next_lower_touch = True`, 3) 같은 이벤트의 `allow_new_case_c_setup = False` 유지, 4) CASE_B_WAIT_ONLY EVENT 발생 | NO_POSITION |


##### Case C 청산 판정 우선순위

익절권 진입 전:

1. `realtime_pct_b >= 0.10` 익절권 진입
2. `ema_slope_30m_realtime <= -0.55` 3분 연속 유지 손절
3. 60분 시간 청산

익절권 진입 후:

1. `realtime_pct_b < 0.10` TP_FALLBACK
2. 1분봉 마감 기준 `ema_slope_30m(close_1m)`와 `previous_trail_ema_slope` 비교
3. 60분 시간 청산

익절권 진입 후에는 Case C 손절 조건을 다시 적용하지 않는다. 1분봉 EMA가 아니라, 1분봉 close를 현재 30분봉의 임시 close로 넣어 계산한 30분봉 EMA slope를 사용한다.

### 2.3 Case B 신호 Event-Action Table(Region 2)

| ID | 현재 상태 | EVENT | Guard | Action | 다음 상태 |
| ---- | ------------------------------- | ----------------- | -------------------------------------------------------------------------------------------------------------- | -------------------------------------------------- | ------------------------- |
| B-01 | Initial Pseudo State | 초기 진입 | None | None | B_WAIT_TOUCH |
| B-02 | B_WAIT_TOUCH | ACTIVATE_TRADE_MANAGEMENT | `touch_candle_low > lower_band_at_touch` OR `touch_candle_bbw >= 0.02` | 1) `case_b_enabled = False`, 2) Case B 상태 변수 초기화 | CASE_B_FINAL_STATE |
| B-03 | B_WAIT_TOUCH | ACTIVATE_TRADE_MANAGEMENT | `touch_candle_low <= lower_band_at_touch` && `touch_candle_bbw < 0.02` | 1) `case_b_enabled = True`, 2) `signal_created = False`, 3) 확정 30분봉 signal 감시 시작 | B_WAIT_SIGNAL |
| B-04 | B_WAIT_SIGNAL | 30M_CANDLE_CLOSED | NOT (`ema_slope_30m_close > -0.03` && `pct_b_close > 0.25` && `current_closed_candle.low >= min(previous_3_closed_candles.low)`) | 다음 30분봉 마감까지 B_WAIT_SIGNAL 유지 | B_WAIT_SIGNAL |
| B-05 | B_WAIT_SIGNAL | 30M_CANDLE_CLOSED | `signal_created == False` && `ema_slope_30m_close > -0.03` && `pct_b_close > 0.25` && `current_closed_candle.low >= min(previous_3_closed_candles.low)` | 1) 현재 확정봉을 최초 `signal_candle`로 저장, 2) `signal_created = True`, `signal_time = candle_close_time`, 3) START_CASE_B_WAIT_PULLBACK_CONDITION_CHECK EVENT 발생 | B_WAIT_PULLBACK |
| B-06 | B_WAIT_PULLBACK | START_CASE_B_WAIT_PULLBACK_CONDITION_CHECK | `position_owner == None` && `case_b_entry_paused == False` && `elapsed_time_from_signal <= 3시간` && `realtime_pct_b <= 0.30` | CASE_B_BUY EVENT 발생 | B_POSITION_OPEN_SIGNALLED |
| B-07 | B_WAIT_PULLBACK | START_CASE_B_WAIT_PULLBACK_CONDITION_CHECK | `elapsed_time_from_signal <= 3시간` && (`realtime_pct_b > 0.30` OR `position_owner != None` OR `case_b_entry_paused == True`) | RETRY_CASE_B_WAIT_PULLBACK_CONDITION_CHECK EVENT 발생 | B_WAIT_PULLBACK |
| B-08 | B_WAIT_PULLBACK | START_CASE_B_WAIT_PULLBACK_CONDITION_CHECK | `elapsed_time_from_signal > 3시간` | 1) 해당 signal 폐기, 2) Case B 상태 변수 초기화 | CASE_B_FINAL_STATE |
| B-09 | B_WAIT_PULLBACK | RETRY_CASE_B_WAIT_PULLBACK_CONDITION_CHECK | `position_owner == None` && `case_b_entry_paused == False` && `elapsed_time_from_signal <= 3시간` && `realtime_pct_b <= 0.30` | CASE_B_BUY EVENT 발생 | B_POSITION_OPEN_SIGNALLED |
| B-10 | B_WAIT_PULLBACK | RETRY_CASE_B_WAIT_PULLBACK_CONDITION_CHECK | `elapsed_time_from_signal <= 3시간` && (`realtime_pct_b > 0.30` OR `position_owner != None` OR `case_b_entry_paused == True`) | RETRY_CASE_B_WAIT_PULLBACK_CONDITION_CHECK EVENT 재발생 | B_WAIT_PULLBACK |
| B-11 | B_WAIT_PULLBACK | RETRY_CASE_B_WAIT_PULLBACK_CONDITION_CHECK | `elapsed_time_from_signal > 3시간` | 1) 해당 signal 폐기, 2) Case B 상태 변수 초기화 | CASE_B_FINAL_STATE |
| B-12 | B_WAIT_SIGNAL | CASE_C_POSITION_OPENED | `position_owner == CASE_C` | `case_b_entry_paused = True`; Case B signal 감시 상태와 변수는 유지 | B_WAIT_SIGNAL |
| B-13 | B_WAIT_PULLBACK | CASE_C_POSITION_OPENED | `position_owner == CASE_C` | `case_b_entry_paused = True`; `signal_time`과 3시간 타이머는 유지 | B_WAIT_PULLBACK |
| B-14 | B_WAIT_SIGNAL | CASE_B_ACTIVE_RESUME | `case_c_recovery_confirmed == True` && `case_c_exit_reason == TP_TRAIL` && `case_c_exit_pct_b < 0.40` | `case_b_entry_paused = False`; 기존 signal 감시를 재개 | B_WAIT_SIGNAL |
| B-15 | B_WAIT_PULLBACK | CASE_B_ACTIVE_RESUME | `case_c_recovery_confirmed == True` && `case_c_exit_reason == TP_TRAIL` && `case_c_exit_pct_b < 0.40` | `case_b_entry_paused = False`; 기존 signal과 3시간 타이머를 유지한 채 pullback 감시 재개 | B_WAIT_PULLBACK |
| B-16 | B_WAIT_SIGNAL | CASE_B_WAIT_ONLY | NOT (`case_c_exit_reason == TP_TRAIL` && `case_c_exit_pct_b < 0.40`) | `case_b_entry_paused = True`; 다음 하단 BB 터치 전까지 signal 판정 대기만 유지 | B_WAIT_SIGNAL |
| B-17 | B_WAIT_PULLBACK | CASE_B_WAIT_ONLY | NOT (`case_c_exit_reason == TP_TRAIL` && `case_c_exit_pct_b < 0.40`) | `case_b_entry_paused = True`; 기존 signal 타이머는 계속 경과하며 신규 매수는 금지 | B_WAIT_PULLBACK |
| B-18 | B_POSITION_OPEN_SIGNALLED | CASE_B_POSITION_OPENED | `position_owner == CASE_B` | Case B 신호 검사 Region 종료 | CASE_B_FINAL_STATE |
| B-19 | B_POSITION_OPEN_SIGNALLED | CASE_C_POSITION_OPENED | `position_owner == CASE_C` | 1) 미체결 Case B 매수·재시도 취소, 2) `case_b_entry_paused = True`, 3) 기존 `signal_time` 유지 | B_WAIT_PULLBACK |


#### Case B 신호 판정 메모

- `touch_candle_bbw < 0.02`는 매수 시점 값이 아니라 **하단 BB 터치봉 값**이다.
- signal은 하단 터치 뒤 `ema_slope_30m_close > -0.03`, `pct_b_close > 0.25`, `signal_candle.low >= min(previous_3_closed_candles.low)`를 최초로 모두 만족한 확정 30분봉 하나만 인정한다.
- `previous_3_closed_candles`는 signal candle 바로 앞의 확정 30분봉 3개이며, 터치봉 기준 저점 비교가 아니다.
- 3시간은 `signal_time`부터 계산하며, 정확히 3시간인 시점은 진입 허용 범위에 포함하고 `> 3시간`이면 signal을 폐기한다.
- Case C 보유 중에도 Case B signal 상태는 유지할 수 있지만 `case_b_entry_paused == True`이면 CASE_B_BUY를 발생시키지 않는다.

### 2.4 Case C 신호 Event-Action Table(Region 3)

| ID | 현재 상태 | EVENT | Guard | Action | 다음 상태 |
| ---- | -------------------- | --------------------- | -------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------- |
| C-01 | Initial Pseudo State | 초기 진입 | None | None | C_WAIT_SETUP  |
| C-02 | C_WAIT_SETUP | ACTIVATE_TRADE_MANAGEMENT | NOT (`allow_new_case_c_setup == True` && `case_c_consumed_for_event == False` && `last_case_c_setup_candle_id != current_30m_candle_id` && `realtime_pct_b <= -0.15` && `cci_30m_realtime <= -140`) | RETRY_C_WAIT_SETUP EVENT 발생 | C_WAIT_SETUP |
| C-03 | C_WAIT_SETUP | ACTIVATE_TRADE_MANAGEMENT | `allow_new_case_c_setup == True` && `case_c_consumed_for_event == False` && `last_case_c_setup_candle_id != current_30m_candle_id` && `realtime_pct_b <= -0.15` && `cci_30m_realtime <= -140` | 1) `last_case_c_setup_candle_id = current_30m_candle_id`, 2) START_CASE_C_SETUP_CONDITION_CHECK EVENT 발생 | C_SETUP |
| C-04 | C_WAIT_SETUP | RETRY_C_WAIT_SETUP | C-02와 동일 | RETRY_C_WAIT_SETUP EVENT 재발생 | C_WAIT_SETUP |
| C-05 | C_WAIT_SETUP | RETRY_C_WAIT_SETUP | C-03과 동일 | 1) `last_case_c_setup_candle_id = current_30m_candle_id`, 2) START_CASE_C_SETUP_CONDITION_CHECK EVENT 발생 | C_SETUP |
| C-06 | C_WAIT_SETUP | CASE_B_POSITION_OPENED | `position_owner == CASE_B` | Case C 신규 진입 권한 종료 | CASE_C_FINAL_STATE |
| C-07 | C_SETUP | START_CASE_C_SETUP_CONDITION_CHECK | None | 1) `flush_low = None`, `flush_low_pct_b = None`, `flush_low_time = None`, 2) `timer_base_pct_b = None`, `timer_base_time = None`, `entry_pct_b = None`, 3) RETRY_CASE_C_SETUP_CONDITION_CHECK EVENT 발생 | C_SETUP |
| C-08 | C_SETUP | RETRY_CASE_C_SETUP_CONDITION_CHECK | `position_owner == None` && `realtime_pct_b >= 0.25` | 1) 매수 없이 Case C setup 종료, 2) `case_c_consumed_for_event = True`, `case_c_recovery_confirmed = True`, `allow_new_case_c_setup = False` | CASE_C_FINAL_STATE |
| C-09 | C_SETUP | RETRY_CASE_C_SETUP_CONDITION_CHECK | `flush_low == None` && `realtime_pct_b <= -0.25` | 1) `flush_low = current_price`, `flush_low_pct_b = realtime_pct_b`, `flush_low_time = now`, 2) `timer_base_pct_b = flush_low_pct_b`, `timer_base_time = now`, 3) `entry_pct_b = timer_base_pct_b + 0.06`, 4) RETRY_CASE_C_SETUP_CONDITION_CHECK EVENT 재발생 | C_SETUP |
| C-10 | C_SETUP | RETRY_CASE_C_SETUP_CONDITION_CHECK | `flush_low != None` && `current_price < flush_low` | 1) `flush_low = current_price`, `flush_low_pct_b = realtime_pct_b`, `flush_low_time = now`, 2) `timer_base_pct_b = flush_low_pct_b`, `timer_base_time = now`, 3) `entry_pct_b = timer_base_pct_b + 0.06`, 4) RETRY_CASE_C_SETUP_CONDITION_CHECK EVENT 재발생 | C_SETUP |
| C-11 | C_SETUP | RETRY_CASE_C_SETUP_CONDITION_CHECK | `flush_low != None` && `current_price >= flush_low` && `now - timer_base_time > 3분` && `realtime_pct_b < 0.25` | 1) `current_open_pct_b = realtime_pct_b`, 2) `timer_base_pct_b = current_open_pct_b`, `timer_base_time = now`, 3) `entry_pct_b = timer_base_pct_b + 0.06`, 4) RETRY_CASE_C_SETUP_CONDITION_CHECK EVENT 재발생 | C_SETUP |
| C-12 | C_SETUP | RETRY_CASE_C_SETUP_CONDITION_CHECK | `position_owner == None` && `flush_low != None` && `now - timer_base_time <= 3분` && `realtime_pct_b >= entry_pct_b` && `entry_pct_b < -0.15` && `realtime_pct_b < 0.25` | 1) CASE_C_BUY EVENT 발생, 2) 매수 명령(판정 확정) 시각 저장 | C_POSITION_OPEN_SIGNALLED |
| C-13 | C_SETUP | RETRY_CASE_C_SETUP_CONDITION_CHECK | `flush_low != None` && `now - timer_base_time <= 3분` && `realtime_pct_b >= entry_pct_b` && `entry_pct_b >= -0.15` && `realtime_pct_b < 0.25` | 1) 추격매수하지 않음, 2) flush 추적과 Case C setup 유지, 3) RETRY_CASE_C_SETUP_CONDITION_CHECK EVENT 재발생 | C_SETUP |
| C-14 | C_SETUP | RETRY_CASE_C_SETUP_CONDITION_CHECK | `realtime_pct_b < 0.25` && ((`flush_low == None` && `realtime_pct_b > -0.25`) OR (`flush_low != None` && `current_price >= flush_low` && `now - timer_base_time <= 3분` && `realtime_pct_b < entry_pct_b`)) | RETRY_CASE_C_SETUP_CONDITION_CHECK EVENT 재발생 | C_SETUP |
| C-15 | C_SETUP | CASE_B_POSITION_OPENED | `position_owner == CASE_B` | Case C 신규 진입 권한 종료; setup 기록은 로그로만 보존 | CASE_C_FINAL_STATE |
| C-16 | C_POSITION_OPEN_SIGNALLED | CASE_C_POSITION_OPENED | `position_owner == CASE_C` | Case C 신호 검사 Region 종료 | CASE_C_FINAL_STATE |
| C-17 | C_POSITION_OPEN_SIGNALLED | CASE_B_POSITION_OPENED | `position_owner == CASE_B` | 1) 미체결 Case C 매수·재시도 취소, 2) Case C 신규 진입 권한 종료 | CASE_C_FINAL_STATE |



#### Case C 신호 판정 메모

- SETUP은 `realtime_pct_b <= -0.15 && cci_30m_realtime <= -140`일 때 시작하고, 첫 flush는 `realtime_pct_b <= -0.25`에서 확정한다.
- `entry_pct_b = timer_base_pct_b + 0.06`이며, `3분 이내 && realtime_pct_b >= entry_pct_b && entry_pct_b < -0.15`를 모두 만족해야 매수한다.
- `entry_pct_b >= -0.15`이면 회복 폭을 만족해도 매수하지 않으며, Case와 flush 추적은 유지한다.
- 더 낮은 가격이 발생한 경우 C-10의 flush 갱신을 C-11의 3분 타이머 재시작보다 먼저 처리한다.
- C_SETUP의 판정 순서는 1) 매수 전 `%B >= 0.25` 종료, 2) 최초 flush 또는 더 낮은 저점 갱신, 3) 3분 초과 시 타이머 재시작, 4) 3분 이내 회복 매수/비매수, 5) 감시 재시도 순이다.
- 한 30분봉에서 Case C setup은 한 번만 인정한다. 매수 전 `realtime_pct_b >= 0.25`이면 해당 setup을 종료한다.
- Case C 매도 후에는 `realtime_pct_b >= 0.25` 회복 전까지 같은 이벤트의 Case C 재진입을 허용하지 않는다. 포지션 보유 중에는 `%B >= 0.25`를 초기화 조건으로 사용하지 않는다.
