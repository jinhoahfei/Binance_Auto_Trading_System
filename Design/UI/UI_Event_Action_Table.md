# UI Event-Action Table
  

  
## 1. 상태 구성  
  
### 1.1 최상위 상태  

| STATE ID | STM 표기 | 의미 |
| ----------------- | -------------- | ------------------------------------------- |
|  | Logic 가동 State | 하단 BB 감시와 진입·매매 관리를 포함하는 최상위 복합 상태 |

  
### 1.2 ==병렬 진입·포지션 소유권== 영역 (Region_1)  

| 상태 ID              | STM 표기                                   |
| ------------------ | ---------------------------------------- |
| NO_POSITION        | 포지션 미보유                                  |
| CASE_B_POSITION_MANAGEMENT | Case B 포지션 관리 복합 상태                |
| CASE_B_HOLDING     | Lower_BB_30M_Pullback (Case_B) 포지션 보유    |
| CASE_B_TREND_HOLD  | 강한 반등 상태 (TREND_HOLD)                    |
| CASE_B_CLOSED      | Lower_BB_30m_Pullback (case_B) 포지션 청산 완료 |
| CASE_C_POSITION_MANAGEMENT | Case C 포지션 관리 복합 상태                |
| CASE_C_HOLDING     | Blade_Catching (Case_C) 포지션 보유           |
| CASE_C_TP_TRAILING | 익절권 진입 (TP_TRAILING)                     |
| CASE_C_CLOSED      | Blade_Catching (case_C) 포지션 청산 완료        |
| CASE_C_RECOVERY_SUCCEEDED | realtime %B >= 0.25 만족               |
  
  
### 1.3 Case B 신호 검사 Region  

| 상태 ID                     | STM 표기                          |
| ------------------------- | ------------------------------- |
| B_WAIT_TOUCH              | case_B 1차 조건 대기                 |
| B_WAIT_SIGNAL             | case_B 1차 조건 만족 (WAIT_SIGNAL)   |
| B_WAIT_PULLBACK           | case_B 2차 조건 만족 (WAIT_PULLBACK) |
| B_POSITION_OPEN_SIGNALLED | case_B 3차 조건 만족 (POSITION_OPEN) |
| CASE_B_FINAL_STATE        | case_B 신호 검사 종료                 |
  
  
### 1.4 Case C 신호 검사 Region  

| 상태 ID                | STM 표기                  |
| -------------------- | ----------------------- |
| C_WAIT_SETUP         | case_C 1차 조건 대기         |
| C_SETUP              | case_C 1차 조건 만족 (SETUP) |
| C_RECOVERY_SUCCEEDED | case_C 회복 성공            |
| CASE_C_FINAL_STATE   | case_C 신호 검사 종료         |
  
  
## 2. Event-Action Table  

### 2.1 Region_1(Upper Status Bar)

#### 2.1.1 Upper Status Bar의 region_1(API Display)
| ID | 현재 상태 | EVENT | 가드·판정 조건 | Action | 다음 상태 |
| ---- | ----------------- | ------------------- | ------------------------- | ------------------------------------- | ----------------- |
| U1-01 | 시작 | None | None | 1) API OFFLINE을 빨간색 글씨로 표시 | API_OFFLINE |
| U1-02 | API_OFFLINE | API_CONNECTED | None | 1) API ONLINE을 초록 글씨로 표시 | API_ONLINE |
| U1-03 | API_ONLINE | API_DISCONNECTED | None | 1) API OFFLINE을 빨간색 글씨로 표시 | API_OFFLINE |
  
#### 2.1.2 Upper Status Bar의 region_2(매매 중지 버튼)
| ID | 현재 상태 | EVENT | 가드·판정 조건 | Action | 다음 상태 |
| ---- | ----------------- | ------------------- | ------------------------- | ------------------------------------- | ----------------- |
| U1-01 | 시작 | None | None | 1) 매매 중지 버튼을 표시 | DISABLE_STOP_TRADING_POPUP |
| U1-02 | DISABLE_STOP_TRADING_POPUP | STOP_TRADING_BUTTON_CLICKED | 포지션 보유 X | 1) API ONLINE을 초록 글씨로 표시 | API_ONLINE |
| U1-03 | API_ONLINE | API_DISCONNECTED | None | 1) API OFFLINE을 빨간색 글씨로 표시 | API_OFFLINE |

  
### 2.2 병렬 진입·포지션 소유권 Event-Action Table(Region 1)  

| ID | 현재 상태 | EVENT | 가드 | Action | 다음 상태 |
| ----- | -------------- | ---------------------------- | ---- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------- |
| O-01 | 포지션 영역 시작 | 초기 진입 | None | None | NO_POSITION |
| O-02 | NO_POSITION | CASE_B_BUY | None | 1) position_owner =  CASE_B, 2) Case B 매수 실행, 3) 매수 정상 완료 확인, 4) 체결시각, 진입 당시 ETH가격, 체결가, ETH 수량, 주문 금액, 수수료 저장, 5) START_CASE_B_CONDITION_CHECK EVENT 발생 | CASE_B_POSITION_MANAGEMENT |
| O-03 | NO_POSITION | CASE_B_BUY | None | 1) position_owner =  CASE_B, 2) Case B 매수 실행, 3) 비정상 완료 시 ‘CASE_B_BUY_RETRY’EVENT 발생 후 종료  | NO_POSITION |
| O-04 | NO_POSITION | CASE_B_BUY_RETRY | None | 1) Case B 매수 실행, 2) 매수 정상 완료 확인, 3) 체결시각, 진입 당시 ETH가격, 체결가, ETH 수량, 주문 금액, 수수료 저장, 4) START_CASE_B_CONDITION_CHECK EVENT 발생 | CASE_B_POSITION_MANAGEMENT |
| O-05 | NO_POSITION | CASE_B_BUY_RETRY | None | 1) Case B 매수 실행, 2) 비정상 완료 시 ‘CASE_B_BUY_RETRY’EVENT 발생 후 종료  | NO_POSITION |
| O-06 | NO_POSITION | CASE_C_BUY | None | 1) position_owner =  CASE_C, 2) Case C 매수 실행, 3) 매수 정상 완료 확인, 4) 체결시각, 진입 당시 ETH가격, 체결가, ETH 수량, 주문 금액, 수수료 저장, 5) START_CASE_C_CONDITION_CHECK EVENT 발생 | CASE_C_POSITION_MANAGEMENT |
| O-07 | NO_POSITION | CASE_C_BUY | None | 1) position_owner =  CASE_C, 2) Case C 매수 실행, 3) 비정상 완료 시 ‘CASE_C_BUY_RETRY’EVENT 발생 후 종료  | NO_POSITION |
| O-08 | NO_POSITION | CASE_C_BUY_RETRY | None | 1) Case C 매수 실행, 2) 매수 정상 완료 확인, 3) 체결시각, 진입 당시 ETH가격, 체결가, ETH 수량, 주문 금액, 수수료 저장, 4) START_CASE_C_CONDITION_CHECK EVENT 발생 | CASE_C_POSITION_MANAGEMENT |
| O-09 | NO_POSITION | CASE_C_BUY_RETRY | None | 1) Case C 매수 실행, 2) 비정상 완료 시 ‘CASE_C_BUY_RETRY’EVENT 발생 후 종료  | NO_POSITION |

  
#### 2.2.1 CASE_B_POSITION_MANAGEMENT Event-Action Table  

| ID | 현재 상태 | EVENT | Guard | Action | 다음 상태 |
| ----- | ----------------------------------- | --------------- | ------------------------------------------------------------------- | ------------------------------------------------------ | --------------------------------------- |
| PB-01 | Initial Pseudo State | 초기 진입 | None | None | CASE_B_HOLDING |
| PB-02 | CASE_B_HOLDING | START_CASE_B_CONDITION_CHECK | None | 1) 익절, 강한 반등, 일반 손절, 비상 손절, 시간 청산의 조건 검사, 2) 어떠한 조건도 만족하지 않았을 시 ‘RETRY_CASE_B_CONDITION_CHECK’ EVENT 발생 | CASE_B_HOLDING |
| PB-03 | CASE_B_HOLDING | START_CASE_B_CONDITION_CHECK | None | 1) 익절, 강한 반등, 일반 손절, 비상 손절, 시간 청산의 조건 검사, 2) 5개의 조건 중 만족한 조건 1개에 대한 EVENT를 발생 | CASE_B_HOLDING |
| PB-04 | CASE_B_HOLDING | RETRY_CASE_B_CONDITION_CHECK | None | 1) 익절, 강한 반등, 일반 손절, 비상 손절, 시간 청산의 조건 검사, 2) 어떠한 조건도 만족하지 않았을 시 ‘RETRY_CASE_B_CONDITION_CHECK’ EVENT 발생 | CASE_B_HOLDING |
| PB-05 | CASE_B_HOLDING | RETRY_CASE_B_CONDITION_CHECK | None | 1) 익절, 강한 반등, 일반 손절, 비상 손절, 시간 청산의 조건 검사, 2) 5개의 조건 중 만족한 조건 1개에 대한 EVENT를 발생 | CASE_B_HOLDING |
| PB-06 | CASE_B_HOLDING | CASE_B_EMERGENCY_STOP(비상 손절) | realtime_price <= entry_price * 0.99 | 1) exit_reason = CASE_B_EMERGENCY_STOP 저장 2) Case B 매도 실행, 3) 매도 정상 완료 확인, 4) 체결시각, 매도 당시 ETH가격, 체결가, ETH 수량, 주문 금액, 수수료 저장, 5) CASE_B_SELL_FINISHED EVENT 발생 | CASE_B_CLOSED |
| PB-07 | CASE_B_HOLDING | CASE_B_EMERGENCY_STOP(비상 손절) | realtime_price <= entry_price * 0.99 | 1) exit_reason = CASE_B_EMERGENCY_STOP 저장 2) Case B 매도 실행, 3) 매도 비정상 완료 시 ‘CASE_B_SELL_RETRY’ EVENT 발생 후 종료  | CASE_B_HOLDING |
| PB-08 | CASE_B_HOLDING | CASE_B_STOP(일반 손절) | confirmed_30m_close && ema_slope < -0.08 | 1) exit_reason = CASE_B_STOP 저장 2) Case B 매도 실행, 3) 매도 정상 완료 확인, 4) 체결시각, 매도 당시 ETH가격, 체결가, ETH 수량, 주문 금액, 수수료 저장, 5) CASE_B_SELL_FINISHED EVENT 발생 | CASE_B_CLOSED |
| PB-09 | CASE_B_HOLDING | CASE_B_STOP(일반 손절) | confirmed_30m_close && ema_slope < -0.08 | 1) exit_reason = CASE_B_STOP 저장 2) Case B 매도 실행, 3) 매도 비정상 완료 시 ‘CASE_B_SELL_RETRY’ EVENT 발생 후 종료  | CASE_B_HOLDING |
| PB-10 | CASE_B_HOLDING | CASE_B_TIME_EXIT(시간 청산) | holding_time >= 6시간 | 1) exit_reason = CASE_B_TIME 저장 2) Case B 매도 실행, 3) 매도 정상 완료 확인, 4) 체결시각, 매도 당시 ETH가격, 체결가, ETH 수량, 주문 금액, 수수료 저장, 5) CASE_B_SELL_FINISHED EVENT 발생 | CASE_B_CLOSED |
| PB-11 | CASE_B_HOLDING | CASE_B_TIME_EXIT(시간 청산) | holding_time >= 6시간 | 1) exit_reason = CASE_B_TIME 저장 2) Case B 매도 실행, 3) 매도 비정상 완료 시 ‘CASE_B_SELL_RETRY’ EVENT 발생 후 종료  | CASE_B_HOLDING |
| PB-12 | CASE_B_HOLDING | CASE_B_TAKE_PROFIT(일반 익절) | 실시간 %B >= 0.60 5초 유지 && realtime_ema_slope <= 0.08 | 1) exit_reason = CASE_B_TAKE_PROFIT 저장 2) Case B 매도 실행, 3) 매도 정상 완료 확인, 4) 체결시각, 매도 당시 ETH가격, 체결가, ETH 수량, 주문 금액, 수수료 저장, 5) CASE_B_SELL_FINISHED EVENT 발생 | CASE_B_CLOSED |
| PB-13 | CASE_B_HOLDING | CASE_B_TAKE_PROFIT(일반 익절) | 실시간 %B >= 0.60 5초 유지 && realtime_ema_slope <= 0.08 | 1) exit_reason = CASE_B_TAKE_PROFIT 저장 2) Case B 매도 실행, 3) 매도 비정상 완료 시 ‘CASE_B_SELL_RETRY’ EVENT 발생 후 종료  | CASE_B_HOLDING |
| PB-14 | CASE_B_HOLDING | CASE_B_UPPER_TREND(강한 반등) | 실시간 %B >= 0.60 5초 유지 및 realtime_ema_slope > 0.08 5초 유지 | None  | CASE_B_TREND_HOLD |
| PB-15 | CASE_B_TREND_HOLD | CASE_B_TREND_HOLD_CONDITION_CHECK(강한 반등) | None | 1) Trend Hold 종료 조건 검사, 2) 조건 만족시에만 CASE_B_TREND_HOLD_SELL EVENT를 발생 | CASE_B_TREND_HOLD |
| PB-16 | CASE_B_TREND_HOLD | CASE_B_TREND_HOLD_SELL(강한 반등) | (realtime_ema_slope <= 0.04) 5초간 유지 or (realtime_pct_b < 0.60) 5초간 유지 | 1) exit_reason = CASE_B_TREND_HOLD 저장 2) Case B 매도 실행, 3) 매도 정상 완료 확인, 4) 체결시각, 매도 당시 ETH가격, 체결가, ETH 수량, 주문 금액, 수수료 저장, 5) CASE_B_SELL_FINISHED EVENT 발생 | CASE_B_CLOSED |
| PB-17 | CASE_B_TREND_HOLD | CASE_B_TREND_HOLD_SELL(강한 반등) | (realtime_ema_slope <= 0.04) 5초간 유지 or (realtime_pct_b < 0.60) 5초간 유지 | 1) exit_reason = CASE_B_TREND_HOLD 저장 2) Case B 매도 실행, 3) 매도 비정상 완료 시 ‘CASE_B_SELL_RETRY’ EVENT 발생 후 종료  | CASE_B_HOLDING |
| PB-18 | CASE_B_HOLDING | CASE_B_SELL_RETRY | None | 1) Case B 매도 실행, 2) 매도 정상 완료 확인, 3) 체결시각, 매도 당시 ETH가격, 체결가, ETH 수량, 주문 금액, 수수료 저장, 4) CASE_B_SELL_FINISHED EVENT 발생 | CASE_B_CLOSED |
| PB-19 | CASE_B_HOLDING | CASE_B_SELL_RETRY | None | 1) Case B 매도 실행, 2) 비정상 완료 시 ‘CASE_B_SELL_RETRY' EVENT 발생 후 종료 | CASE_B_HOLDING |
| PB-20 | CASE_B_CLOSED | CASE_B_SELL_FINISHED EVENT | None | 1) position_owner = None | LOWER_TOUCH_WATCH |
  
  
  
#### 2.2.2 CASE_C_POSITION_MANAGEMENT Event-Action Table  

| ID | 현재 상태 | EVENT | Guard | Action | 다음 상태 |
| ----- | ------------------ | --------------------- | --------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ | ------------------------------------------ |
| PC-01 | Initial Pseudo State | 초기 진입 | None | None | CASE_C_HOLDING |
| PC-02 | CASE_C_HOLDING | START_CASE_C_CONDITION_CHECK | None | 1) 익절권 진입, 손절, 시간청산에 대한 조건 검사, 2) 어떠한 조건도 만족하지 않았을 시 ‘RETRY_CASE_C_CONDITION_CHECK’ EVENT 발생 | CASE_C_HOLDING |
| PC-03 | CASE_C_HOLDING | START_CASE_C_CONDITION_CHECK | None | 1) 익절권 진입, 손절, 시간청산에 대한 조건 검사, 2) 3개의 조건 중 만족한 조건 1개에 대한 EVENT를 발생 | CASE_C_HOLDING |
| PC-04 | CASE_C_HOLDING | RETRY_CASE_C_CONDITION_CHECK | None | 1) 익절권 진입, 손절, 시간청산에 대한 조건 검사, 2) 어떠한 조건도 만족하지 않았을 시 ‘RETRY_CASE_C_CONDITION_CHECK’ EVENT 발생 | CASE_C_HOLDING |
| PC-05 | CASE_C_HOLDING | RETRY_CASE_C_CONDITION_CHECK | None | 1) 익절권 진입, 손절, 시간청산에 대한 조건 검사, 2) 3개의 조건 중 만족한 조건 1개에 대한 EVENT를 발생 | CASE_C_HOLDING |
| PC-06 | CASE_C_HOLDING | CASE_C_STOP(손절) | ema_slope_30m_realtime <= -0.55가 3분 유지 | 1) exit_reason = CASE_C_STOP 저장 2) Case C 매도 실행, 3) 매도 정상 완료 확인, 4) 매도 당시의 %B, 체결시각, 매도 당시 ETH가격, 체결가, ETH 수량, 주문 금액, 수수료 저장, 5) CASE_C_SELL_FINISHED EVENT 발생 | CASE_C_CLOSED |
| PC-07 | CASE_C_HOLDING | CASE_C_STOP(손절) | ema_slope_30m_realtime <= -0.55가 3분 유지 | 1) exit_reason = CASE_C_STOP 저장 2) Case C 매도 실행, 3) 매도 비정상 완료 시 ‘CASE_C_SELL_RETRY’ EVENT 발생 후 종료  | CASE_C_HOLDING |
| PC-08 | CASE_C_HOLDING | CASE_C_TIME_EXIT(시간 청산) | holding_time >= 1시간 | 1) exit_reason = CASE_C_TIME 저장 2) Case C 매도 실행, 3) 매도 정상 완료 확인, 4) 매도 당시의 %B, 체결시각, 매도 당시 ETH가격, 체결가, ETH 수량, 주문 금액, 수수료 저장, 5) CASE_C_SELL_FINISHED EVENT 발생 | CASE_C_CLOSED |
| PC-09 | CASE_C_HOLDING | CASE_C_TIME_EXIT(시간 청산) | holding_time >= 1시간 | 1) exit_reason = CASE_C_TIME 저장 2) Case C 매도 실행, 3) 매도 비정상 완료 시 ‘CASE_C_SELL_RETRY’ EVENT 발생 후 종료  | CASE_C_HOLDING |
| PC-10 | CASE_C_HOLDING | CASE_C_ENTER_PROFIT_ZONE(익절권 진입) | realtime_pct_b >= 0.10 | 1) 익절권을 진입한 봉을 이용, tp_price = lower + 0.10 * (upper - lower), 2) previous_trail_ema_slope = ema_slope_30m(tp_price), 3) START_TP_TRAILING_CONDITION_CHECK EVENT 발생  | CASE_C_TP_TRAILING |
| PC-11 | CASE_C_TP_TRAILING | START_TP_TRAILING_CONDITION_CHECK | None | 1) 1분봉 마감 시 current_close_ema_slope = ema_slope_30m(close_1m) 실행, 2) 회복 강도 유지에 대한 판단 검사, 2) 어떠한 조건도 만족하지 않았을 시 ‘RETRY_CASE_C_CONDITION_CHECK’ EVENT 발생 | CASE_C_TP_TRAILING |
| PC-12 | CASE_C_TP_TRAILING | START_TP_TRAILING_CONDITION_CHECK | None | 1) 1분봉 마감 시 current_close_ema_slope = ema_slope_30m(close_1m) 실행, 2) 회복 강도 유지에 대한 판단 검사, 2) 3개의 조건 중 만족한 조건 1개에 대한 EVENT를 발생 | CASE_C_TP_TRAILING |
| PC-13 | CASE_C_TP_TRAILING | RETRY_TP_TRAILING_CONDITION_CHECK | None | 1) 1분봉 마감 시 current_close_ema_slope = ema_slope_30m(close_1m) 실행, 2) 회복 강도 유지에 대한 판단 검사, 2) 어떠한 조건도 만족하지 않았을 시 ‘RETRY_CASE_C_CONDITION_CHECK’ EVENT 발생 | CASE_C_TP_TRAILING |
| PC-14 | CASE_C_TP_TRAILING | RETRY_TP_TRAILING_CONDITION_CHECK | None | 1) 1분봉 마감 시 current_close_ema_slope = ema_slope_30m(close_1m) 실행, 2) 회복 강도 유지에 대한 판단 검사, 2) 3개의 조건 중 만족한 조건 1개에 대한 EVENT를 발생 | CASE_C_TP_TRAILING |
| PC-15 | CASE_C_TP_TRAILING | CASE_C_EMA_INCREASEMENT(EMA9 증가) | current_close_ema_slope > previous_trail_ema_slope | 1) previous_trail_ema_slope = current_close_ema_slope 실행 | CASE_C_TP_TRAILING |
| PC-16 | CASE_C_TP_TRAILING | CASE_C_SELL_AT_TP_PRICE(회복 강도 약화) | realtime_pct_b < 0.10 | 1) exit_reason = CASE_C_PROTECTION 저장 2) Case C 매도 실행, 3) 매도 정상 완료 확인, 4) 매도 당시의 %B, 체결시각, 매도 당시 ETH가격, 체결가, ETH 수량, 주문 금액, 수수료 저장, 5) CASE_C_SELL_FINISHED EVENT 발생 | CASE_C_CLOSED |
| PC-17 | CASE_C_TP_TRAILING | CASE_C_SELL_AT_TP_PRICE(회복 강도 약화) | realtime_pct_b < 0.10 | 1) exit_reason = CASE_C_PROTECTION 저장 2) Case C 매도 실행, 3) 매도 비정상 완료 시 ‘CASE_C_SELL_RETRY’ EVENT 발생 후 종료  | CASE_C_TP_TRAILING |
| PC-18 | CASE_C_TP_TRAILING | CASE_C_EMA_DECREASEMENT(EMA9 감소) | current_close_ema_slope <= previous_trail_ema_slope | 1) exit_reason = CASE_C_SUCCESS 저장 2) 현재 진행중인 1분 봉 마감 시점에 Case C 매도 실행, 3) 매도 정상 완료 확인, 4) 매도 당시의 %B, 체결시각, 매도 당시 ETH가격, 체결가, ETH 수량, 주문 금액, 수수료 저장, 5) CASE_C_SELL_FINISHED EVENT 발생 | CASE_C_CLOSED |
| PC-19 | CASE_C_TP_TRAILING | CASE_C_EMA_DECREASEMENT(EMA9 감소) | current_close_ema_slope <= previous_trail_ema_slope | 1) exit_reason = CASE_C_SUCCESS 저장 2) 현재 진행중인 1분 봉 마감 시점에 Case C 매도 실행, 3) 매도 비정상 완료 시 ‘CASE_C_SELL_RETRY’ EVENT 발생 후 종료  | CASE_C_TP_TRAILING |
| PC-20 | CASE_C_HOLDING | CASE_C_SELL_RETRY | None | 1) Case C 매도 실행, 2) 매도 정상 완료 확인, 3) 매도 당시의 %B, 체결시각, 매도 당시 ETH가격, 체결가, ETH 수량, 주문 금액, 수수료 저장, 4) CASE_C_SELL_FINISHED EVENT 발생 | CASE_C_CLOSED |
| PC-21 | CASE_C_HOLDING | CASE_C_SELL_RETRY | None | 1) Case C 매도 실행, 2) 비정상 완료 시 ‘CASE_C_SELL_RETRY' EVENT 발생 후 종료 | CASE_C_HOLDING |
| PC-22 | CASE_C_CLOSED | CASE_C_SELL_FINISHED | None | 1) realtime_pct_b >= 0.25 검사, 2) 조건을 만족한 조건을 만족한 경우 CASE_C_RECOVERY EVENT를 발생 | CASE_C_CLOSED |
| PC-23 | CASE_C_CLOSED | CASE_C_RECOVERY | realtime_pct_b >= 0.25 | 1) CHECK_PCT_B EVENT 발생 | CASE_C_RECOVERY_SUCCEEDED | 
| PC-24 | CASE_C_RECOVERY_SUCCEEDED | CASE_C_RECOVERY | None | 1) 매도 당시의 %B의 값을 비교, 그에 맞는 이벤트를 발생 | CASE_C_RECOVERY_SUCCEEDED |
| PC-25 | CASE_C_RECOVERY_SUCCEEDED | OVER_ZERO_POINT_FOUR | 매도 당시의 %B >= 0.40 | 1) position_owner = None, 2) allow_new_case_c_setup = True | LOWER_TOUCH_WATCH |
| PC-26 | CASE_C_RECOVERY_SUCCEEDED | UNDER_ZERO_POINT_FOUR | 매도 당시의 %B < 0.40 | 1) position_owner = None, 2) allow_new_case_c_setup = True, 3) GO_TO_WAIT_SIGNAL EVENT 발생 | LOWER_TOUCH_WATCH |
  
  

  
### 2.3 Case B 신호 Event-Action Table  

| ID | 현재 상태 | EVENT | Guard | Action | 다음 상태 |
| ---- | ------------------------------- | ----------------- | -------------------------------------------------------------------------------------------------------------- | -------------------------------------------------- | ------------------------- |
| B-01 | Initial Pseudo State | 초기 진입 | None | None | B_WAIT_TOUCH |
| B-02 | B_WAIT_TOUCH | ACTIVATE_TRADE_MANAGEMENT | None | 1) 현재 30분봉의 저가, '하단 BB를 최초로 터치한 30분봉' 기준의 BBW에 대한 조건 검사, 2) 2개의 조건 중 하나라도 만족하지 않았을 시 중지 | CASE_B_FINAL_STATE |
| B-03 | B_WAIT_TOUCH | ACTIVATE_TRADE_MANAGEMENT | None | 1) 현재 30분봉의 저가, '하단 BB를 최초로 터치한 30분봉' 기준의 BBW에 대한 조건 검사, 2) 2개의 조건을 모두 만족 시 CASE_B로 진입을 확정  | B_WAIT_SIGNAL |
| B-04 | B_WAIT_TOUCH | GO_TO_WAIT_SIGNAL | None | None | B_WAIT_SIGNAL |
| B-05 | B_WAIT_SIGNAL | 30M_CANDLE_CLOSED | None | 1) 조건 판단을 실행, 2) 조건 만족 시 'START_CASE_B_WAIT_PULLBACK_CONDITION_CHECK' EVENT를 발생 | B_WAIT_PULLBACK |
| B-06 | B_WAIT_PULLBACK | START_CASE_B_WAIT_PULLBACK_CONDITION_CHECK | position_owner = None | 1) 3시간 타이머 확인, 2) 실시간 %b에 대한 조건 검사, 2) (실시간 %b <= 0.30) 만족 시 ‘CASE_B_BUY’ EVENT 발생, 3) position_owner = case_B 설정 | B_POSITION_OPEN_SIGNALLED |
| B-07 | B_WAIT_PULLBACK | START_CASE_B_WAIT_PULLBACK_CONDITION_CHECK | None | 1) 3시간 타이머 확인, 2) 실시간 %b에 대한 조건 검사, 2) (실시간 %b <= 0.30) 불만족 시 ‘RETRY_CASE_B_WAIT_PULLBACK_CONDITION_CHECK’ EVENT 발생 | B_WAIT_PULLBACK |
| B-08 | B_WAIT_PULLBACK | RETRY_CASE_B_WAIT_PULLBACK_CONDITION_CHECK | position_owner = None | 1) 3시간 타이머 확인, 2) 실시간 %b에 대한 조건 검사, 2) (실시간 %b <= 0.30) 만족 시 ‘CASE_B_BUY’ EVENT 발생, 3) position_owner = case_B 설정 | B_POSITION_OPEN_SIGNALLED |
| B-09 | B_WAIT_PULLBACK | RETRY_CASE_B_WAIT_PULLBACK_CONDITION_CHECK | None | 1) 3시간 타이머 확인, 2) 실시간 %b에 대한 조건 검사, 2) (실시간 %b <= 0.30) 불만족 시 ‘RETRY_CASE_B_WAIT_PULLBACK_CONDITION_CHECK’ EVENT 발생 | B_WAIT_PULLBACK |
| B-10 | B_WAIT_PULLBACK | RETRY_CASE_B_WAIT_PULLBACK_CONDITION_CHECK | None | 1) 3시간 타이머의 3시간 초과 확인 | CASE_B_FINAL_STATE   |
| B-11 | B_WAIT_PULLBACK | GO_TO_WAIT_SIGNAL | None | 1) 'START_CASE_B_WAIT_SIGNAL_CONDITION_CHECK' EVENT를 발생. | B_WAIT_SIGNAL |
| B-12 | B_POSITION_OPEN_SIGNALLED | CASE_B_BUY | None | None | CASE_B_FINAL_STATE |
  
### 2.4 Case C 신호 Event-Action Table  

| ID | 현재 상태 | EVENT | Guard | Action | 다음 상태 |
| ---- | -------------------- | --------------------- | -------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------- |
| C-01 | Initial Pseudo State | 초기 진입 | None | None | C_WAIT_SETUP  |
| C-02 | C_WAIT_SETUP | ACTIVATE_TRADE_MANAGEMENT | None | 1) 마지막 case_C의 '판정 확정시간을 검사'하여 직전의 case_C가 판정된 30분 봉과 현재의 봉이 다른 봉인지 확인, 실시간 %b, 실시간 30분 봉 CCI에 대한 조건 검사, 2) 3개의 조건 중 하나라도 만족하지 않았을 시 ‘RETRY_C_WAIT_SETUP’ EVENT 발생 | C_WAIT_SETUP |
| C-03 | C_WAIT_SETUP | ACTIVATE_TRADE_MANAGEMENT | allow_new_case_c_setup = True | 1) 마지막 case_C의 '판정 확정시간을 검사'하여 직전의 case_C가 판정된 30분 봉과 현재의 봉이 다른 봉인지 확인, 실시간 %b, 실시간 30분 봉 CCI에 대한 조건 검사, 2) 3개의 조건을 모두 만족 시 'START_CASE_C_SETUP_CONDITION_CHECK' EVENT를 발생 | C_SETUP |
| C-04 | C_WAIT_SETUP | RETRY_C_WAIT_SETUP | None | 1) 마지막 case_C의 '판정 확정시간을 검사'하여 직전의 case_C가 판정된 30분 봉과 현재의 봉이 다른 봉인지 확인, 실시간 %b, 실시간 30분 봉 CCI에 대한 조건 검사, 2) 3개의 조건 중 하나라도 만족하지 않았을 시 ‘RETRY_C_WAIT_SETUP’ EVENT 발생 | C_WAIT_SETUP |
| C-05 | C_WAIT_SETUP | RETRY_C_WAIT_SETUP | allow_new_case_c_setup = True | 1) 마지막 case_C의 '판정 확정시간을 검사'하여 직전의 case_C가 판정된 30분 봉과 현재의 봉이 다른 봉인지 확인, 실시간 %b, 실시간 30분 봉 CCI에 대한 조건 검사, 2) 3개의 조건을 모두 만족 시 'START_CASE_C_SETUP_CONDITION_CHECK' EVENT를 발생 | C_SETUP |
| C-06 | C_WAIT_SETUP | CASE_B_BUY | None | None | CASE_C_FINAL_STATE |
| C-07 | C_SETUP | START_CASE_C_SETUP_CONDITION_CHECK | None | 1) flush, timer 관련값 초기화, 2) 'RETRY_CASE_C_SETUP_CONDITION_CHECK' EVENT 발생 | C_SETUP |
| C-08 | C_SETUP | RETRY_CASE_C_SETUP_CONDITION_CHECK | None | 1) (실시간 %b, 현재 가격), 3분 타이머, 회복의 정도에 대한 조건 검사, 2) 3개의 조건 중 만족한 조건 1개에 대한 EVENT를 발생 | C_SETUP |
| C-09 | C_SETUP | RETRY_CASE_C_SETUP_CONDITION_CHECK | None | 1) (실시간 %b, 현재 가격), 3분 타이머, 회복의 정도에 대한 조건 검사, 2) 3개의 조건 중 하나라도 해당하지 않았을 시 ‘RETRY_C_WAIT_SETUP’ EVENT 발생 | C_SETUP |
| C-10 | C_SETUP | UPDATE_SETUP_VARIABLES | (realtime_pct_b <= -0.25) or (current_price < flush_low)  | 1) flush_low와 flush_low_pct_b/time을 갱신하고 timer_base_pct_b/time도 현재 시점 기준으로 갱신, 2) 'RETRY_CASE_C_SETUP_CONDITION_CHECK' EVENT 발생 | C_SETUP |
| C-11 | C_SETUP | RECOVERY_FAIL_IN_3_MIN | (now - timer_base_time) > 3min | 1) timer_base_pct_b = current_open_pct_b, timer_base_time = now로 갱신, 2) 'RETRY_CASE_C_SETUP_CONDITION_CHECK' EVENT 발생 | C_SETUP |
| C-12 | C_SETUP | RECOVERY_SUCCESS_IN_3_MIN | (realtime_pct_b >= entry_pct_b) | 1) 'CASE_C_BUY' EVENT 발생, 2) position_owner = case_C, 3) 매수 명령(=판정 확정)이 일어난 시각 저장 | C_RECOVERY_SUCCEEDED |
| C-13 | C_SETUP | OVER_RECOVERY_IN_3_MIN | realtime_pct_b >= 0.25 | None | CASE_C_FINAL_STATE |
| C-14 | C_SETUP | CASE_B_BUY | None | None | CASE_C_FINAL_STATE |
| C-12 | C_RECOVERY_SUCCEEDED | CASE_C_BUY | None | None | CASE_C_FINAL_STATE |