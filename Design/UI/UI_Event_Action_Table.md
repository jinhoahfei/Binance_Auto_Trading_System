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

### 2.1 Etire UI System의 Region_1(Upper Status Bar)

#### 2.1.1 Upper Status Bar의 region_1(API Display)
| ID | 현재 상태 | EVENT | 가드·판정 조건 | Action | 다음 상태 |
| ---- | ----------------- | ------------------- | ------------------------- | ------------------------------------- | ----------------- |
| U1-01 | 시작 | None | None | 1) API OFFLINE을 빨간색 글씨로 표시 | API_OFFLINE |
| U1-02 | API_OFFLINE | API_CONNECTED | None | 1) API ONLINE을 초록 글씨로 표시 | API_ONLINE |
| U1-03 | API_ONLINE | API_DISCONNECTED | None | 1) API OFFLINE을 빨간색 글씨로 표시 | API_OFFLINE |
  
#### 2.1.2 Upper Status Bar의 region_2(매매 중지 버튼)
| ID | 현재 상태 | EVENT | 가드·판정 조건 | Action | 다음 상태 |
| ---- | ----------------- | ------------------- | ------------------------- | ------------------------------------- | ----------------- |
| U2-01 | 시작 | None | None | 1) 매매 중지 버튼을 표시 | DISABLE_STOP_TRADING_POPUP |
| U2-02 | DISABLE_STOP_TRADING_POPUP | STOP_BUTTON_CLICKED | 포지션 보유 X | 1) 포지션 보유X에 해당하는 팝업을 출력 | STOP_POPUP_DISPLAYED |
| U2-03 | DISABLE_STOP_TRADING_POPUP | STOP_BUTTON_CLICKED | 포지션 보유 O | 1) 포지션 보유O에 해당하는 팝업을 출력 | FORCE_SELL_AND_STOP_POPUP_DISPLAYED |
| U2-04 | STOP_POPUP_DISPLAYED | STOP_CONFIRMED | None | 1) 자동매매 중단, 2) 팝업 제거 | DISABLE_STOP_TRADING_POPUP |
| U2-05 | STOP_POPUP_DISPLAYED | STOP_CANCELED | None | 1) 팝업 제거 | DISABLE_STOP_TRADING_POPUP |
| U2-06 | FORCE_SELL_AND_STOP_POPUP_DISPLAYED | FORCE_SELL_AND_STOP_CONFIRMED | None | 1) 포지션 강제 매도, 2) 자동매매 중단, 3) 팝업 제거 | DISABLE_STOP_TRADING_POPUP |
| U2-07 | FORCE_SELL_AND_STOP_POPUP_DISPLAYED | FORCE_SELL_AND_STOP_CANCELED | None | 1) 팝업 제거 | DISABLE_STOP_TRADING_POPUP |

#### 2.1.3 Upper Status Bar의 region_3(자동매매 실행 버튼)
| ID | 현재 상태 | EVENT | 가드·판정 조건 | Action | 다음 상태 |
| ---- | ----------------- | ------------------- | ------------------------- | ------------------------------------- | ----------------- |
| U3-01 | 시작 | None | None | 1) '자동매매 실행' 버튼을 표시 | DISABLE_START_TRADING_POPUP |
| U3-02 | DISABLE_START_TRADING_POPUP | START_BUTTON_CLICKED | REGIME 선택 O | 1) REGIME 선택 o에 해당하는 팝업을 출력 | DISPLAY_START_TRADING_POPUP |
| U3-03 | DISABLE_START_TRADING_POPUP | START_BUTTON_CLICKED | REGIME 선택 X | 1) REGIME 선택 x에 해당하는 팝업을 출력 | DISPLAY_SELECT_REGIME_POPUP |
| U3-04 | DISPLAY_START_TRADING_POPUP | START_CONFIRMED | None | 1) 팝업 제거, 2) 거래 시작, 3) '자동매매 실행 중'으로 버튼을 변경 | AUTO_TRADING_RUNNING |
| U3-05 | DISPLAY_START_TRADING_POPUP | START_CANCELED | None | 1) 팝업 제거 | DISABLE_START_TRADING_POPUP |
| U3-06 | DISPLAY_SELECT_REGIME_POPUP | CONFIRMED | None | 1) 팝업 제거, 2) REGIME 영역에 점등 5회 | DISABLE_START_TRADING_POPUP |
| U3-07 | AUTO_TRADING_RUNNING | FORCE_SELL_AND_STOP_CONFIRMED | None | 1) '자동매매 실행'으로 버튼 변경 | DISABLE_START_TRADING_POPUP |
| U3-08 | AUTO_TRADING_RUNNING | STOP_CONFIRMED | None | 1) '자동매매 실행'으로 버튼 변경 | DISABLE_START_TRADING_POPUP |


### 2.2 Etire UI System의 Region_1(Upper Status Bar)

| ID | 현재 상태 | EVENT | 가드 | Action | 다음 상태 |
| ----- | -------------- | ---------------------------- | ---- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------- |
