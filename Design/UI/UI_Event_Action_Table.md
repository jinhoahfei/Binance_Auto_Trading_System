# UI Event-Action Table
    
## 1. 상태 구성

상태 번호는 composite state와 그 내부 region의 포함관계를 그대로 반영한다. 같은 상태 ID가 서로 다른 region에 반복되는 경우에는 각 region의 독립 상태로 해석한다. 각 region의 `Initial Pseudo State`는 아래 표에서 반복 기재하지 않고, 2절의 해당 Event-Action Table에 정의된 초기 전이를 따른다.

### 1.1 최상위 상태

| STATE ID | STM 표기 | 의미 |
| --- | --- | --- |
| ETIRE_UI_SYSTEM | Etire UI System | `Region_1`(Upper Status Bar), `Region_2`(MAIN_SCREEN & TRADING_DETAILS), `Region_3`(프로그램 종료)를 병렬로 포함하는 최상위 orthogonal composite state |
| UI_FINAL_STATE | UI 종료 Final State | 모든 병렬 region을 종료하고 Etire UI System 전체를 종료하는 최상위 Final State |

### 1.2 Etire UI System의 Region_1 — Upper Status Bar 복합 상태

`UPPER_STATUS_BAR`는 API 상태 표시, 매매 중지 버튼, 자동매매 실행 버튼의 세 region을 병렬로 포함한다.

#### 1.2.1 Upper Status Bar의 Region_1 — API Display

| 상태 ID | STM 표기 |
| --- | --- |
| API_OFFLINE | API 연결 끊김 표시 |
| API_ONLINE | API 연결 정상 표시 |

#### 1.2.2 Upper Status Bar의 Region_2 — 매매 중지 버튼

| 상태 ID | STM 표기 |
| --- | --- |
| DISABLE_STOP_TRADING_POPUP | 매매 중지 팝업 비표시 |
| STOP_POPUP_DISPLAYED | 포지션 미보유 매매 중지 확인 팝업 표시 |
| FORCE_SELL_AND_STOP_POPUP_DISPLAYED | 포지션 보유 강제 매도 후 중지 확인 팝업 표시 |
| FORCE_SELL_AND_STOP_PROCESSING | 포지션 강제 매도 처리 중 |

#### 1.2.3 Upper Status Bar의 Region_3 — 자동매매 실행 버튼

| 상태 ID | STM 표기 |
| --- | --- |
| DISABLE_START_TRADING_POPUP | 자동매매 실행 팝업 비표시 |
| DISPLAY_START_TRADING_POPUP | 자동매매 실행 확인 팝업 표시 |
| DISPLAY_SELECT_REGIME_POPUP | REGIME 선택 안내 팝업 표시 |
| DISPLAY_API_CONNECTION_REQUIRED_POPUP | API 연결 필요 안내 팝업 표시 |
| AUTO_TRADING_RUNNING | 자동매매 실행 중 |

### 1.3 Etire UI System의 Region_2 — MAIN_SCREEN & TRADING_DETAILS

| 상태 ID | STM 표기 | 의미 |
| --- | --- | --- |
| MAIN_SCREEN_WRAPPER | Main Screen 복합 상태 | 메인 화면의 네 region을 병렬로 포함 |
| TRADING_DETAILS | Trading Details 복합 상태 | 거래 내역 상세 화면의 네 region을 병렬로 포함 |
| H* | Deep History Pseudo State | `TRADING_DETAILS`에서 메인 화면으로 복귀할 때 `MAIN_SCREEN_WRAPPER`의 직전 활성 상태 구성을 복원 |

#### 1.3.1 MAIN_SCREEN_WRAPPER 복합 상태

`MAIN_SCREEN_WRAPPER`는 `Region_1`(REGIME Panel), `Region_2`(Display Chart), `Region_3`(Display Account Info), `Region_4`(체결 내역 & 실시간 지표 탭)를 병렬로 포함한다.

##### 1.3.1.1 MAIN_SCREEN의 Region_1 — REGIME Panel 복합 상태

REGIME Panel은 추천 type 표시, type 선택 상태 표시, type 지표 표시의 세 region을 병렬로 포함한다.

###### 1.3.1.1.1 REGIME Panel의 Region_1 — 추천 type 표시

| 상태 ID | STM 표기 |
| --- | --- |
| RECOMMENDED_TYPE_DISPLAYED | 현재 추천 type 표시 |

###### 1.3.1.1.2 REGIME Panel의 Region_2 — type 선택 상태 표시

| 상태 ID | STM 표기 |
| --- | --- |
| TYPE_SELECTION | 적용 REGIME 표시 및 변경 후보 선택 대기 |
| TYPE_CHANGING_POPUP_DISPLAYED | 자동매매 실행 여부와 관계없이 type 변경 확인 팝업 표시 |

###### 1.3.1.1.3 REGIME Panel의 Region_3 — type 지표 표시

| 상태 ID | STM 표기 |
| --- | --- |
| DISPLAY_TYPE_INDICATOR | REGIME 판단 지표의 실시간 값 표시 |

##### 1.3.1.2 MAIN_SCREEN의 Region_2 — Display Chart 복합 상태

Display Chart는 봉 변경, 지표 설정, Active State 표시, 전체 화면, 선 긋기 표시, 그려놓은 선 지우기의 여섯 region을 병렬로 포함한다.

###### 1.3.1.2.1 Display Chart의 Region_1 — 봉 변경

| 상태 ID | STM 표기 |
| --- | --- |
| 30_M_CHART_DISPLAY | 30분봉 차트 표시 |
| 1_M_CHART_DISPLAY | 1분봉 차트 표시 |
| 4_H_CHART_DISPLAY | 4시간봉 차트 표시 |
| 1_DAY_CHART_DISPLAY | 1일봉 차트 표시 |

###### 1.3.1.2.2 Display Chart의 Region_2 — 지표 설정

| 상태 ID | STM 표기 | 의미 |
| --- | --- | --- |
| INDICATOR_SETTINGS_POPUP_CLOSED | 지표 설정 팝업 닫힘 | 지표 설정 팝업이 표시되지 않는 상태 |
| INDICATOR_SETTINGS_POPUP_OPENED | 지표 설정 팝업 열림 복합 상태 | 볼린저밴드, EMA, 거래량 설정 region을 병렬로 포함 |

###### 1.3.1.2.2.1 INDICATOR_SETTINGS_POPUP_OPENED의 Region_1 — 볼린저밴드 설정

| 상태 ID | STM 표기 |
| --- | --- |
| BB_DISPLAY_CHOICE | 저장된 볼린저밴드 표시 상태에 따른 Choice Pseudo State |
| BB_DISPLAY_OFF | 볼린저밴드 표시 OFF |
| BB_DISPLAY_ON | 볼린저밴드 표시 ON |

###### 1.3.1.2.2.2 INDICATOR_SETTINGS_POPUP_OPENED의 Region_2 — EMA 설정

| 상태 ID | STM 표기 |
| --- | --- |
| EMA_DISPLAY_CHOICE | 저장된 EMA 표시 상태에 따른 Choice Pseudo State |
| EMA_DISPLAY_OFF | EMA 표시 OFF |
| EMA_DISPLAY_ON | EMA 표시 ON |

###### 1.3.1.2.2.3 INDICATOR_SETTINGS_POPUP_OPENED의 Region_3 — 거래량 설정

| 상태 ID | STM 표기 |
| --- | --- |
| VOLUME_DISPLAY_CHOICE | 저장된 거래량 표시 상태에 따른 Choice Pseudo State |
| VOLUME_DISPLAY_OFF | 거래량 표시 OFF |
| VOLUME_DISPLAY_ON | 거래량 표시 ON |

###### 1.3.1.2.3 Display Chart의 Region_3 — Active State 표시

| 상태 ID | STM 표기 |
| --- | --- |
| ACTIVE_TRADING_LOGIC_STATE_DISPLAY | 현재 투자 로직 상태 표시 |

###### 1.3.1.2.4 Display Chart의 Region_4 — 전체 화면

| 상태 ID | STM 표기 |
| --- | --- |
| NORMAL_VIEW_DISPLAY | 일반 화면 크기로 차트 표시 |
| FULL_SCREEN_VIEW_DISPLAY | 전체 화면 크기로 차트 표시 |

###### 1.3.1.2.5 Display Chart의 Region_5 — 선 긋기 표시

| 상태 ID | STM 표기 |
| --- | --- |
| DRAWING_DEACTIVATED | 선 그리기 비활성화 |
| DRAWING_WAIT | 선 그리기 입력 대기 |
| USER_DRAWING | 사용자가 선을 그리는 중 |

###### 1.3.1.2.6 Display Chart의 Region_6 — 그려놓은 선 지우기

| 상태 ID | STM 표기 |
| --- | --- |
| AWAITING_SELECTION | 그려진 선 선택 대기 |
| DRAWN_LINE_HIGHLIGHTED | 커서가 올라간 선 강조 표시 |
| CONTEXT_MENU_OPENED | 선택한 선의 컨텍스트 메뉴 표시 |

##### 1.3.1.3 MAIN_SCREEN의 Region_3 — Display Account Info 복합 상태

Display Account Info는 현재 투자 로직 표시, 보유 자산 표시, 분할 매수/매도 관리의 세 region을 병렬로 포함한다.

###### 1.3.1.3.1 Display Account Info의 Region_1 — 현재 투자 로직 표시

| 상태 ID | STM 표기 |
| --- | --- |
| TRADING_LOGIC_STATUS_DISPLAYED | 현재 투자 로직 상태 및 수익률 표시 |

###### 1.3.1.3.2 Display Account Info의 Region_2 — 보유 자산 표시

| 상태 ID | STM 표기 |
| --- | --- |
| ASSET_SUMMARY_DISPLAYED | KRW 및 ETH 보유 자산 표시 |

###### 1.3.1.3.3 Display Account Info의 Region_3 — 분할 매수/매도 관리 복합 상태

분할 매수/매도 관리는 분할 매수와 분할 매도의 두 region을 병렬로 포함한다.

###### 1.3.1.3.3.1 분할 매수/매도 관리의 Region_1 — 분할 매수

| 상태 ID | STM 표기 |
| --- | --- |
| SCALE_IN_ORDER | 분할 매수 비율 설정 및 표시 |

###### 1.3.1.3.3.2 분할 매수/매도 관리의 Region_2 — 분할 매도

| 상태 ID | STM 표기 |
| --- | --- |
| SCALE_OUT_ORDER | 분할 매도 비율 설정 및 표시 |

##### 1.3.1.4 MAIN_SCREEN의 Region_4 — 체결 내역 & 실시간 지표 탭

| 상태 ID | STM 표기 |
| --- | --- |
| TRADE_HISTORY_DISPLAYED | 체결 내역 탭 표시 |
| REALTIME_INDICATOR_DISPLAYED | 실시간 지표 탭 표시 |

#### 1.3.2 TRADING_DETAILS 복합 상태

`TRADING_DETAILS`는 `Region_1`(계좌 내역 상세), `Region_2`(표시할 기간 선택), `Region_3`(매도/매수 표시 선택), `Region_4`(CSV 내보내기)를 병렬로 포함한다.

##### 1.3.2.1 TRADING_DETAILS의 Region_1 — 계좌 내역 상세 복합 상태

계좌 내역 상세는 수익률 표시, 매도 성과 표시, ETH 보유 수량 표시, 당일 수수료 표시의 네 region을 병렬로 포함한다.

###### 1.3.2.1.1 계좌 내역 상세의 Region_1 — 수익률 표시

| 상태 ID | STM 표기 |
| --- | --- |
| PROFIT_RATE_DISPLAYED | 입출금을 보정한 수익률 표시 |

###### 1.3.2.1.2 계좌 내역 상세의 Region_2 — 매도 성과 표시

| 상태 ID | STM 표기 |
| --- | --- |
| TRADE_PERFORMANCE_DISPLAYED | 누적 매도 성과 표시 |

###### 1.3.2.1.3 계좌 내역 상세의 Region_3 — ETH 보유 수량 표시

| 상태 ID | STM 표기 |
| --- | --- |
| ETH_HOLDINGS_DISPLAYED | ETH 보유 수량 표시 |

###### 1.3.2.1.4 계좌 내역 상세의 Region_4 — 당일 수수료 표시

| 상태 ID | STM 표기 |
| --- | --- |
| DAILY_TRADING_FEE_DISPLAYED | 당일 발생 수수료 표시 |

##### 1.3.2.2 TRADING_DETAILS의 Region_2 — 표시할 기간 선택

| 상태 ID | STM 표기 |
| --- | --- |
| TODAY_TRADE_HISTORY_DISPLAYED | 오늘 거래 내역 표시 |
| WEEKLY_TRADE_HISTORY_DISPLAYED | 최근 7일 거래 내역 표시 |
| MONTHLY_TRADE_HISTORY_DISPLAYED | 최근 30일 거래 내역 표시 |
| ALL_TRADE_HISTORY_DISPLAYED | 전체 기간 거래 내역 표시 |

##### 1.3.2.3 TRADING_DETAILS의 Region_3 — 매도/매수 표시 선택

| 상태 ID | STM 표기 |
| --- | --- |
| ALL_TRADE_HISTORY_DISPLAYED | 매수·매도 전체 거래 내역 표시 |
| BUY_TRADE_HISTORY_DISPLAYED | 매수 거래 내역만 표시 |
| SELL_TRADE_HISTORY_DISPLAYED | 매도 거래 내역만 표시 |

##### 1.3.2.4 TRADING_DETAILS의 Region_4 — CSV 내보내기

| 상태 ID | STM 표기 | 의미 |
| --- | --- | --- |
| AWAITING_CSV_EXPORT_POPUP | CSV 내보내기 팝업 열기 대기 | CSV 내보내기 팝업이 표시되지 않는 상태 |
| CSV_EXPORT_POPUP_DISPLAYED | CSV 내보내기 팝업 표시 복합 상태 | 파일 저장 위치, 불러올 기간, 파일 이름 입력 region을 병렬로 포함 |
| CSV_EXPORT_IN_PROGRESS | CSV 내보내기 처리 중 | 검증된 설정으로 CSV 파일을 생성하는 상태 |
| CSV_EXPORT_COMPLETE | CSV 내보내기 완료 | 내보내기 완료 팝업 표시 |
| CSV_EXPORT_ERROR | CSV 내보내기 실패 | 내보내기 실패 원인을 표시하고 확인을 대기하는 상태 |

###### 1.3.2.4.1 CSV_EXPORT_POPUP_DISPLAYED의 Region_1 — 파일 저장 위치 선택

| 상태 ID | STM 표기 |
| --- | --- |
| FILE_BROWSER_CLOSED | 파일 탐색기 닫힘 |
| FILE_BROWSER_OPENED | 파일 탐색기 열림 |

###### 1.3.2.4.2 CSV_EXPORT_POPUP_DISPLAYED의 Region_2 — 불러올 기간 선택

| 상태 ID | STM 표기 |
| --- | --- |
| CSV_TODAY_TRADE_HISTORY | 내보낼 기간을 오늘로 설정 |
| CSV_WEEKLY_TRADE_HISTORY | 내보낼 기간을 최근 7일로 설정 |
| CSV_MONTHLY_TRADE_HISTORY | 내보낼 기간을 최근 30일로 설정 |
| CSV_SELECT_DATE | 사용자 지정 기간 선택 활성 상태 |
| CSV_START_DATE | 시작일 달력 팝업 표시 |
| CSV_FINISH_DATE | 종료일 달력 팝업 표시 |

###### 1.3.2.4.3 CSV_EXPORT_POPUP_DISPLAYED의 Region_3 — 저장할 파일 이름 입력

| 상태 ID | STM 표기 |
| --- | --- |
| DEFAULT_FILE_NAME | 기본 파일 이름 표시 |
| NEW_FILE_NAME_TYPED | 새 파일 이름 입력 중 |
| FILE_NAME_WRITTEN | 입력한 파일 이름 적용 |

### 1.4 Etire UI System의 Region_3 — 프로그램 종료

| 상태 ID | STM 표기 |
| --- | --- |
| AWAITING_EXIT | 프로그램 종료 요청 대기 |
| FORCE_SELL_EXIT_POPUP_DISPLAYED | 포지션 보유 중 강제 매도 후 종료 확인 팝업 표시 |
| FORCE_SELL_EXIT_PROCESSING | 강제 매도 체결 확인 대기 |
| EXIT_POPUP_DISPLAYED | 일반 종료 확인 또는 종료 준비 실패 후 재확인 팝업 표시 |
  
  
## 2. Event-Action Table  

실행 동작 공통 주석(2026-09-16):

- 표의 조회·저장·거래 로직 초기화는 책임 경계를 포함한다. UI는 backend snapshot/이벤트를 표시하고 `UiCommandPort`에 명령을 요청한다. 체결 영속 저장과 거래 세션 초기화는 backend가 담당하며 UI의 화면 진입이 이를 다시 실행하지 않는다. M4의 체결 저장 표현도 backend에서 저장된 결과를 수신·목록에 반영하는 뜻이다.
- 비동기 요청 수락은 작업 완료가 아니다. 중지·청산 요청이 수락되어도 authoritative lifecycle이 종료를 확인하기 전에는 대기를 유지한다. CSV는 결과 응답 후 완료/실패 팝업을 표시하고, 프로그램 종료는 backend 종료 준비와 네이티브 종료 확인까지 마친 후 최상위 `UI_FINAL_STATE`에 도달한다. API 단절 후에도 중지 완료는 서버 상태로 확인한다.
- REGIME 선택은 실행·정지 모두 `UI_Rule.md` CR-03을 따른다. 적용 성공 전에 후보를 실제 적용값으로 바꾸지 않는다.
- TD2-01/TD3-01의 오늘·전체 설정은 최초 진입에 해당한다. 이후 재진입은 기존 기간·거래 종류를 유지하며 결합 조건으로 다시 조회한다. 조회 중에는 필터 입력을 무시한다. 이탈 시 진행 중 조회를 취소하고, 오래된 응답으로 현재 결과를 덮지 않는다. 필터 조회는 기존 요약 카드를 유지하고, 화면 진입·명시적 새로고침·체결/서버 갱신 등 요약 갱신 경로를 구분한다.
- H*는 실제 하위 활성 상태를 복원한다. 화면 밖에서 갱신된 서버 데이터는 최신값을 유지하며, 이미 제출한 쓰기 명령은 복귀만으로 재제출하지 않는다. CSV 기본 파일명은 창을 새로 열 때 설정하고 기간·날짜 변경만으로 자동 변경하지 않는다.


### 2.1 Etire UI System의 Region_1(Upper Status Bar)

#### 2.1.1 Upper Status Bar의 region_1(API Display)
| ID | 현재 상태 | EVENT | 가드·판정 조건 | Action | 다음 상태 |
| ---- | ----------------- | ------------------- | ------------------------- | ------------------------------------- | ----------------- |
| U1-01 | Initial Pseudo State | None | None | 1) API OFFLINE을 빨간색 글씨로 표시 | API_OFFLINE |
| U1-02 | API_OFFLINE | API_CONNECTED | None | 1) API ONLINE을 초록 글씨로 표시 | API_ONLINE |
| U1-03 | API_ONLINE | API_DISCONNECTED | None | 1) API OFFLINE을 빨간색 글씨로 표시 | API_OFFLINE |
  
#### 2.1.2 Upper Status Bar의 region_2(매매 중지 버튼)
| ID | 현재 상태 | EVENT | 가드·판정 조건 | Action | 다음 상태 |
| ---- | ----------------- | ------------------- | ------------------------- | ------------------------------------- | ----------------- |
| U2-01 | Initial Pseudo State | None | None | 1) 매매 중지 버튼을 표시 | DISABLE_STOP_TRADING_POPUP |
| U2-02 | DISABLE_STOP_TRADING_POPUP | STOP_BUTTON_CLICKED | 자동매매 실행 중 O && 포지션 보유 X | 1) 포지션 미보유에 해당하는 팝업을 출력 | STOP_POPUP_DISPLAYED |
| U2-03 | DISABLE_STOP_TRADING_POPUP | STOP_BUTTON_CLICKED | 자동매매 실행 중 O && 포지션 보유 O | 1) 포지션 보유에 해당하는 팝업을 출력 | FORCE_SELL_AND_STOP_POPUP_DISPLAYED |
| U2-04 | DISABLE_STOP_TRADING_POPUP | STOP_BUTTON_CLICKED | 자동매매 실행 중 X | 1) 자동매매가 실행 중이 아님을 표시 | DISABLE_STOP_TRADING_POPUP |
| U2-05 | STOP_POPUP_DISPLAYED | STOP_CONFIRMED | None | 1) 자동매매 중단, 2) 팝업 제거 | DISABLE_STOP_TRADING_POPUP |
| U2-06 | STOP_POPUP_DISPLAYED | STOP_CANCELED | None | 1) 팝업 제거 | DISABLE_STOP_TRADING_POPUP |
| U2-07 | FORCE_SELL_AND_STOP_POPUP_DISPLAYED | FORCE_SELL_AND_STOP_CONFIRMED | None | 1) 포지션 강제 매도 주문 제출, 2) 강제 매도 처리 중 상태 표시 | FORCE_SELL_AND_STOP_PROCESSING |
| U2-08 | FORCE_SELL_AND_STOP_PROCESSING | FORCE_SELL_AND_STOP_SUCCEEDED | None | 1) 자동매매 중단, 2) 팝업 제거 | DISABLE_STOP_TRADING_POPUP |
| U2-09 | FORCE_SELL_AND_STOP_PROCESSING | FORCE_SELL_AND_STOP_FAILED | None | 1) 강제 매도 실패 원인을 표시, 2) 강제 매도 후 중지 확인 팝업을 다시 표시 | FORCE_SELL_AND_STOP_POPUP_DISPLAYED |
| U2-10 | FORCE_SELL_AND_STOP_POPUP_DISPLAYED | FORCE_SELL_AND_STOP_CANCELED | None | 1) 팝업 제거 | DISABLE_STOP_TRADING_POPUP |

#### 2.1.3 Upper Status Bar의 region_3(자동매매 실행 버튼)
| ID | 현재 상태 | EVENT | 가드·판정 조건 | Action | 다음 상태 |
| ---- | ----------------- | ------------------- | ------------------------- | ------------------------------------- | ----------------- |
| U3-01 | Initial Pseudo State | None | None | 1) '자동매매 실행' 버튼을 표시 | DISABLE_START_TRADING_POPUP |
| U3-02 | DISABLE_START_TRADING_POPUP | START_BUTTON_CLICKED | REGIME 선택 O && API 연결 O | 1) 자동매매 실행 확인 팝업을 출력 | DISPLAY_START_TRADING_POPUP |
| U3-03 | DISABLE_START_TRADING_POPUP | START_BUTTON_CLICKED | REGIME 선택 X | 1) REGIME 선택 안내 팝업을 출력 | DISPLAY_SELECT_REGIME_POPUP |
| U3-04 | DISABLE_START_TRADING_POPUP | START_BUTTON_CLICKED | REGIME 선택 O && API 연결 X | 1) API 연결 필요 안내 팝업을 출력 | DISPLAY_API_CONNECTION_REQUIRED_POPUP |
| U3-05 | DISPLAY_START_TRADING_POPUP | START_CONFIRMED | API 연결 O | 1) 팝업 제거, 2) 거래 로직을 초기화한 뒤 자동매매 시작, 3) '자동매매 실행 중'으로 버튼 변경 | AUTO_TRADING_RUNNING |
| U3-06 | DISPLAY_START_TRADING_POPUP | START_CONFIRMED | API 연결 X | 1) 자동매매 실행 확인 팝업 제거, 2) API 연결 필요 안내 팝업 출력 | DISPLAY_API_CONNECTION_REQUIRED_POPUP |
| U3-07 | DISPLAY_START_TRADING_POPUP | START_CANCELED | None | 1) 팝업 제거 | DISABLE_START_TRADING_POPUP |
| U3-08 | DISPLAY_SELECT_REGIME_POPUP | SELECT_REGIME_NOTICE_CONFIRMED | None | 1) 팝업 제거, 2) REGIME 영역에 점등 5회 | DISABLE_START_TRADING_POPUP |
| U3-09 | DISPLAY_API_CONNECTION_REQUIRED_POPUP | API_CONNECTION_NOTICE_CONFIRMED | None | 1) 팝업 제거 | DISABLE_START_TRADING_POPUP |
| U3-10 | AUTO_TRADING_RUNNING | FORCE_SELL_AND_STOP_SUCCEEDED | None | 1) '자동매매 실행'으로 버튼 변경 | DISABLE_START_TRADING_POPUP |
| U3-11 | AUTO_TRADING_RUNNING | STOP_CONFIRMED | None | 1) '자동매매 실행'으로 버튼 변경 | DISABLE_START_TRADING_POPUP |
| U3-12 | AUTO_TRADING_RUNNING | API_DISCONNECTED | None | 1) 신규 주문 생성 중단, 2) 자동매매 중단, 3) '자동매매 실행'으로 버튼 변경, 4) API 연결 필요 안내 팝업 출력 | DISPLAY_API_CONNECTION_REQUIRED_POPUP |


### 2.2 Etire UI System의 Region_2(MAIN_SCREEN & TRADING_DETAILS)
| ID | 현재 상태 | EVENT | 가드 | Action | 다음 상태 |
| ----- | -------------- | ---------------------------- | ---- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------- |
| ES2-01 | Initial Pseudo State | None | None | None | MAIN_SCREEN_WRAPPER |
| ES2-02 | MAIN_SCREEN_WRAPPER | SHOW_ALL_TRADING_DETAILS | None | 1) 거래내역 상세보기 페이지를 표시 | TRADING_DETAILS |
| ES2-03 | TRADING_DETAILS | BACK_TO_MAIN_SCREEN | None | 1) 메인 페이지의 직전 상태를 표시 | H* |

#### 2.2.1 MAIN_SCREEN_WRAPPER 상태

##### 2.2.1.1 MAIN_SCREEN의 Region1(REGIME Panel)
###### 2.2.1.1.1 REGIME Panel의 Region1(추천 type 표시)
| ID | 현재 상태 | EVENT | 가드 | Action | 다음 상태 |
| ----- | -------------- | ---------------------------- | ---- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------- |
| R1-01 | Initial Pseudo State | None | None | 1) 현재 추천 타입을 표시 | RECOMMENDED_TYPE_DISPLAYED |
| R1-02 | RECOMMENDED_TYPE_DISPLAYED | TYPE_RECOMMENDED | None | 1) 추천 타입을 갱신하여 표시 | RECOMMENDED_TYPE_DISPLAYED |

###### 2.2.1.1.2 REGIME Panel의 Region2(type 선택 상태 표시)
| ID | 현재 상태 | EVENT | 가드 | Action | 다음 상태 |
| ----- | -------------- | ---------------------------- | ---- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------- |
| R2-01 | Initial Pseudo State | None | None | 1) type0 ~ type4 버튼에 색칠되어있지 않은 상태로 표시 | TYPE_SELECTION |
| R2-02 | TYPE_SELECTION | TYPE_CLICKED | 자동매매 실행 중 O | 1) 적용값은 유지하고 클릭한 type을 변경 후보로 표시, 2) 변경 확인 팝업 표시 | TYPE_CHANGING_POPUP_DISPLAYED |
| R2-03 | TYPE_SELECTION | TYPE_CLICKED | 자동매매 실행 중 X | 1) 적용값은 유지하고 클릭한 type을 변경 후보로 표시, 2) 변경 확인 팝업 표시 | TYPE_CHANGING_POPUP_DISPLAYED |
| R2-04 | TYPE_CHANGING_POPUP_DISPLAYED | CONFIRM_TYPE_CHANGE | None | 1) 후보 type의 backend 적용을 요청하고 대기 표시, 2) 성공 시 적용값 확정 및 팝업 제거, 3) 실패 시 기존 적용값을 유지하고 오류 표시 | 성공: TYPE_SELECTION / 실패: TYPE_CHANGING_POPUP_DISPLAYED |
| R2-05 | TYPE_CHANGING_POPUP_DISPLAYED | CANCEL_TYPE_CHANGE | None | 1) 후보 폐기 및 팝업 제거, 2) 기존 적용 type 표시 유지 | TYPE_SELECTION |

###### 2.2.1.1.3 REGIME Panel의 Region3(type 지표 표시)
| ID | 현재 상태 | EVENT | 가드 | Action | 다음 상태 |
| ----- | -------------- | ---------------------------- | ---- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------- |
| R3-01 | Initial Pseudo State | None | None | 1) REGIME 판단 지표의 실시간 값을 표시 | DISPLAY_TYPE_INDICATOR |
| R3-02 | DISPLAY_TYPE_INDICATOR | REGIME_INDICATOR_UPDATED | None | 1) 전달받은 지표의 실시간 값을 표시(양수는 초록, 음수는 빨강) | DISPLAY_TYPE_INDICATOR |


##### 2.2.1.2 MAIN_SCREEN의 Region2(Display Chart)
###### 2.2.1.2.1 Display Chart의 Region1(봉 변경)
| ID | 현재 상태 | EVENT | 가드 | Action | 다음 상태 |
| ----- | -------------- | ---------------------------- | ---- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------- |
| DC1-01 | Initial Pseudo State | None | None | 1) 30분봉 차트 표시, 2) 30분봉 차트에 그린 그림이 있다면 불러온다, 3) 설정된 지표를 차트에 표시 | 30_M_CHART_DISPLAY |
| DC1-02 | 30_M_CHART_DISPLAY | 1_M_BUTTON_CLICKED | None | 1) 1분 봉 차트 표시, 2) 1분 봉 차트에 그린 그림이 있다면 불러온다, 3) 설정된 지표를 차트에 표시 | 1_M_CHART_DISPLAY |
| DC1-03 | 30_M_CHART_DISPLAY | 4_H_BUTTON_CLICKED | None | 1) 4시간 봉 차트 표시, 2) 4시간 봉 차트에 그린 그림이 있다면 불러온다, 3) 설정된 지표를 차트에 표시 | 4_H_CHART_DISPLAY |
| DC1-04 | 30_M_CHART_DISPLAY | 1_DAY_BUTTON_CLICKED | None | 1) 1일 봉 차트 표시, 2) 1일 봉 차트에 그린 그림이 있다면 불러온다, 3) 설정된 지표를 차트에 표시 | 1_DAY_CHART_DISPLAY |
| DC1-05 | 1_M_CHART_DISPLAY | 30_M_BUTTON_CLICKED | None | 1) 30분 봉 차트 표시, 2) 30분 봉 차트에 그린 그림이 있다면 불러온다, 3) 설정된 지표를 차트에 표시 | 30_M_CHART_DISPLAY |
| DC1-06 | 1_M_CHART_DISPLAY | 4_H_BUTTON_CLICKED | None | 1) 4시간 봉 차트 표시, 2) 4시간 봉 차트에 그린 그림이 있다면 불러온다, 3) 설정된 지표를 차트에 표시 | 4_H_CHART_DISPLAY |
| DC1-07 | 1_M_CHART_DISPLAY | 1_DAY_BUTTON_CLICKED | None | 1) 1일 봉 차트 표시, 2) 1일 봉 차트에 그린 그림이 있다면 불러온다, 3) 설정된 지표를 차트에 표시 | 1_DAY_CHART_DISPLAY |
| DC1-08 | 4_H_CHART_DISPLAY | 1_M_BUTTON_CLICKED | None | 1) 1분 봉 차트 표시, 2) 1분 봉 차트에 그린 그림이 있다면 불러온다, 3) 설정된 지표를 차트에 표시 | 1_M_CHART_DISPLAY |
| DC1-09 | 4_H_CHART_DISPLAY | 30_M_BUTTON_CLICKED | None | 1) 30분 봉 차트 표시, 2) 30분 봉 차트에 그린 그림이 있다면 불러온다, 3) 설정된 지표를 차트에 표시 | 30_M_CHART_DISPLAY |
| DC1-10 | 4_H_CHART_DISPLAY | 1_DAY_BUTTON_CLICKED | None | 1) 1일 봉 차트 표시, 2) 1일 봉 차트에 그린 그림이 있다면 불러온다, 3) 설정된 지표를 차트에 표시 | 1_DAY_CHART_DISPLAY |
| DC1-11 | 1_DAY_CHART_DISPLAY | 1_M_BUTTON_CLICKED | None | 1) 1분 봉 차트 표시, 2) 1분 봉 차트에 그린 그림이 있다면 불러온다, 3) 설정된 지표를 차트에 표시 | 1_M_CHART_DISPLAY |
| DC1-12 | 1_DAY_CHART_DISPLAY | 30_M_BUTTON_CLICKED | None | 1) 30분 봉 차트 표시, 2) 30분 봉 차트에 그린 그림이 있다면 불러온다, 3) 설정된 지표를 차트에 표시 | 30_M_CHART_DISPLAY |
| DC1-13 | 1_DAY_CHART_DISPLAY | 4_H_BUTTON_CLICKED | None | 1) 4시간 봉 차트 표시, 2) 4시간 봉 차트에 그린 그림이 있다면 불러온다, 3) 설정된 지표를 차트에 표시 | 4_H_CHART_DISPLAY |

###### 2.2.1.2.2 Display Chart의 Region2(지표 설정)
| ID | 현재 상태 | EVENT | 가드 | Action | 다음 상태 |
| ----- | -------------- | ---------------------------- | ---- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------- |
| DC2-01 | Initial Pseudo State | None | None | None | INDICATOR_SETTINGS_POPUP_CLOSED |
| DC2-02 | INDICATOR_SETTINGS_POPUP_CLOSED | INDICATOR_SETTINGS_BUTTON_CLICKED | None | 1) 지표 설정 팝업 표시 | INDICATOR_SETTINGS_POPUP_OPENED |
| DC2-03 | INDICATOR_SETTINGS_POPUP_OPENED | INDICATOR_POPUP_OUTSIDE_CLICKED | None | 1) 지표 설정 팝업 제거 | INDICATOR_SETTINGS_POPUP_CLOSED |
###### 2.2.1.2.2.1 INDICATOR_SETTINGS_POPUP_OPENED의 Region1(볼린저밴드 설정)
| ID | 현재 상태 | EVENT | 가드 | Action | 다음 상태 |
| ----- | -------------- | ---------------------------- | ---- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------- |
| IP1-01 | Initial Pseudo State | None | None | 1) 현재 BB 표시 상태 불러오기 | BB_DISPLAY_CHOICE |
| IP1-02 | BB_DISPLAY_CHOICE | None | 불러온 BB 표시 상태 = off | 1) 차트에서 BB 표시를 제거, 2) BB 스위치를 off로 표시 | BB_DISPLAY_OFF |
| IP1-03 | BB_DISPLAY_CHOICE | None | 불러온 BB 표시 상태 = on | 1) 차트에서 BB를 표시, 2) BB 스위치를 on으로 표시 | BB_DISPLAY_ON |
| IP1-04 | BB_DISPLAY_OFF | BB_DISPLAY_ON_CLICKED | None | 1) 차트에서 BB를 표시, 2) BB 스위치를 on으로 슬라이드, 3) 현재 BB 표시 상태를 on으로 저장 | BB_DISPLAY_ON |
| IP1-05 | BB_DISPLAY_ON | BB_DISPLAY_OFF_CLICKED | None | 1) 차트에서 BB 표시를 제거, 2) BB 스위치를 off로 슬라이드, 3) 현재 BB 표시 상태를 off로 저장 | BB_DISPLAY_OFF |
###### 2.2.1.2.2.2 INDICATOR_SETTINGS_POPUP_OPENED의 Region2(EMA 설정)
| ID | 현재 상태 | EVENT | 가드 | Action | 다음 상태 |
| ----- | -------------- | ---------------------------- | ---- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------- |
| IP2-01 | Initial Pseudo State | None | None | 1) 현재 EMA 표시 상태 불러오기 | EMA_DISPLAY_CHOICE |
| IP2-02 | EMA_DISPLAY_CHOICE | None | 불러온 EMA 표시 상태 = off | 1) 차트에서 EMA 표시를 제거, 2) EMA 스위치를 off로 표시 | EMA_DISPLAY_OFF |
| IP2-03 | EMA_DISPLAY_CHOICE | None | 불러온 EMA 표시 상태 = on | 1) 차트에서 EMA를 표시, 2) EMA 스위치를 on으로 표시 | EMA_DISPLAY_ON |
| IP2-04 | EMA_DISPLAY_OFF | EMA_DISPLAY_ON_CLICKED | None | 1) 차트에서 EMA를 표시, 2) EMA 스위치를 on으로 슬라이드, 3) 현재 EMA 표시 상태를 on으로 저장 | EMA_DISPLAY_ON |
| IP2-05 | EMA_DISPLAY_ON | EMA_DISPLAY_OFF_CLICKED | None | 1) 차트에서 EMA 표시를 제거, 2) EMA 스위치를 off로 슬라이드, 3) 현재 EMA 표시 상태를 off로 저장 | EMA_DISPLAY_OFF |
###### 2.2.1.2.2.3 INDICATOR_SETTINGS_POPUP_OPENED의 Region3(거래량 설정)
| ID | 현재 상태 | EVENT | 가드 | Action | 다음 상태 |
| ----- | -------------- | ---------------------------- | ---- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------- |
| IP3-01 | Initial Pseudo State | None | None | 1) 현재 거래량 표시 상태 불러오기 | VOLUME_DISPLAY_CHOICE |
| IP3-02 | VOLUME_DISPLAY_CHOICE | None | 불러온 거래량 표시 상태 = off | 1) 차트에서 거래량 표시를 제거, 2) 거래량 스위치를 off로 표시 | VOLUME_DISPLAY_OFF |
| IP3-03 | VOLUME_DISPLAY_CHOICE | None | 불러온 거래량 표시 상태 = on | 1) 차트에서 거래량을 표시, 2) 거래량 스위치를 on으로 표시 | VOLUME_DISPLAY_ON |
| IP3-04 | VOLUME_DISPLAY_OFF | VOLUME_DISPLAY_ON_CLICKED | None | 1) 차트에서 거래량을 표시, 2) 거래량 스위치를 on으로 슬라이드, 3) 현재 거래량 표시 상태를 on으로 저장 | VOLUME_DISPLAY_ON |
| IP3-05 | VOLUME_DISPLAY_ON | VOLUME_DISPLAY_OFF_CLICKED | None | 1) 차트에서 거래량 표시를 제거, 2) 거래량 스위치를 off로 슬라이드, 3) 현재 거래량 표시 상태를 off로 저장 | VOLUME_DISPLAY_OFF |

###### 2.2.1.2.3 Display Chart의 Region3(Active State 표시)
| ID | 현재 상태 | EVENT | 가드 | Action | 다음 상태 |
| ----- | -------------- | ---------------------------- | ---- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------- |
| DC3-01 | Initial Pseudo State | None | None | 1) 현재 투자 로직 상태 불러오기, 2) 불러온 투자 로직 상태 표시 | ACTIVE_TRADING_LOGIC_STATE_DISPLAY |
| DC3-02 | ACTIVE_TRADING_LOGIC_STATE_DISPLAY | TRADING_LOGIC_STATE_CHANGED | None | 1) 변경된 투자 로직 상태 불러오기, 2) 불러온 투자 로직 상태 표시 | ACTIVE_TRADING_LOGIC_STATE_DISPLAY |

###### 2.2.1.2.4 Display Chart의 Region4(전체 화면)
| ID | 현재 상태 | EVENT | 가드 | Action | 다음 상태 |
| ----- | -------------- | ---------------------------- | ---- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------- |
| DC4-01 | Initial Pseudo State | None | None | 1) 표시할 화면을 일반화면 영역으로 설정 | NORMAL_VIEW_DISPLAY |
| DC4-02 | NORMAL_VIEW_DISPLAY | FULL_SIZE_SELECTED | None | 1) 현재 차트를 유지하며 화면만 전체화면으로 확대 | FULL_SCREEN_VIEW_DISPLAY |
| DC4-03 | FULL_SCREEN_VIEW_DISPLAY | NORMAL_SIZE_SELECTED | None | 1) 현재 차트를 유지하며 화면만 일반화면으로 축소 | NORMAL_VIEW_DISPLAY |

###### 2.2.1.2.5 Display Chart의 Region5(선 긋기 표시)
| ID | 현재 상태 | EVENT | 가드 | Action | 다음 상태 |
| ----- | -------------- | ---------------------------- | ---- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------- |
| DC5-01 | Initial Pseudo State | None | None | None | DRAWING_DEACTIVATED |
| DC5-02 | DRAWING_DEACTIVATED | DRAWING_TOOL_CLICKED | Region6 현재 상태 = AWAITING_SELECTION | 1) 그리기를 시작할 수 있도록 환경을 세팅한다 | DRAWING_WAIT |
| DC5-03 | DRAWING_WAIT | USER_START_DRAWING | None | 1) 그리는 중인 선을 표시 | USER_DRAWING |
| DC5-04 | DRAWING_WAIT | DRAWING_TOOL_CLICKED | None | 1) 그리기 비활성화 | DRAWING_DEACTIVATED |
| DC5-05 | USER_DRAWING | USER_FINISH_DRAWING | None | 1) 그리기 완료된 선을 표시 | DRAWING_WAIT |
| DC5-06 | USER_DRAWING | DRAWING_CANCELED | None | 1) 그리는 중인 선을 제거 | DRAWING_WAIT |
| DC5-07 | USER_DRAWING | DRAWING_TOOL_CLICKED | None | 1) 그리는 중인 선을 제거, 2) 그리기 비활성화 | DRAWING_DEACTIVATED |

###### 2.2.1.2.6 Display Chart의 Region6(그려놓은 선 지우기)
| ID | 현재 상태 | EVENT | 가드 | Action | 다음 상태 |
| ----- | -------------- | ---------------------------- | ---- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------- |
| DC6-01 | Initial Pseudo State | None | None | None | AWAITING_SELECTION |
| DC6-02 | AWAITING_SELECTION | CURSOR_HOVER_ENTER | Region5 현재 상태 != USER_DRAWING | 1) 커서가 올라간 선을 하이라이트 처리한다 | DRAWN_LINE_HIGHLIGHTED |
| DC6-03 | DRAWN_LINE_HIGHLIGHTED | HIGHLIGHTED_LINE_RIGHT_CLICKED | Region5 현재 상태 != USER_DRAWING | 1) 커서의 바로 우측에 붙여서 컨텍스트 메뉴를 표시한다 | CONTEXT_MENU_OPENED |
| DC6-04 | DRAWN_LINE_HIGHLIGHTED | CURSOR_HOVER_EXIT | None | 1) 커서가 올라간 선의 하이라이트를 제거한다 | AWAITING_SELECTION |
| DC6-05 | CONTEXT_MENU_OPENED | DELETE_LINE | None | 1) 선택한 선을 제거, 2) 컨텍스트 메뉴를 제거 | AWAITING_SELECTION |
| DC6-06 | CONTEXT_MENU_OPENED | CONTEXT_MENU_OUTSIDE_CLICKED | None | 1) 컨텍스트 메뉴를 제거한다 | AWAITING_SELECTION |


##### 2.2.1.3 MAIN_SCREEN의 Region3(Display Account Info)
###### 2.2.1.3.1 Display Account Info의 Region1(현재 투자 로직 표시)
| ID | 현재 상태 | EVENT | 가드 | Action | 다음 상태 |
| ----- | -------------- | ---------------------------- | ---- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------- |
| DI1-01 | Initial Pseudo State | None | None | 1) 현재 동작하는 투자 로직상태를 가져온다, 2) 현재 투자 로직상태에서의 수익률을 가져온다, 3) 현재 상태와 현재상태에서의 수익률을 표시한다. | TRADING_LOGIC_STATUS_DISPLAYED |
| DI1-02 | TRADING_LOGIC_STATUS_DISPLAYED | TRADING_STATUS_UPDATED | None | 1) 현재 동작하는 투자 로직상태를 가져온다, 2) 현재 투자 로직상태에서의 수익률을 가져온다, 3) 현재 상태와 현재상태에서의 수익률을 표시한다. | TRADING_LOGIC_STATUS_DISPLAYED |

###### 2.2.1.3.2 Display Account Info의 Region2(보유 자산 표시)
| ID | 현재 상태 | EVENT | 가드 | Action | 다음 상태 |
| ----- | -------------- | ---------------------------- | ---- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------- |
| DI2-01 | Initial Pseudo State | None | None | 1) 현재 자산(KRW)을 가져온다, 2) 현재 자산(ETH)을 가져온다, 3) 가져온 자산을 표시한다 | ASSET_SUMMARY_DISPLAYED |
| DI2-02 | ASSET_SUMMARY_DISPLAYED | ASSET_SUMMARY_UPDATED | None | 1) 현재 자산(KRW)을 가져온다, 2) 현재 자산(ETH)을 가져온다, 3) 가져온 자산을 표시한다 | ASSET_SUMMARY_DISPLAYED |

###### 2.2.1.3.3 Display Account Info의 Region3(분할 매수 / 매도 관리)
###### 2.2.1.3.3.1 분할 매수 / 매도 관리의 Region1(분할 매수)
| ID | 현재 상태 | EVENT | 가드 | Action | 다음 상태 |
| ----- | -------------- | ---------------------------- | ---- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------- |
| SI-01 | Initial Pseudo State | None | None | 1) 초기 세팅 값 가져오기, 2) 가져온 초기값을 화면에 표시 | SCALE_IN_ORDER |
| SI-02 | SCALE_IN_ORDER | SCALE_IN_LEVEL_CHANGED | None | 1) 사용자가 선택한 값을 향후 매수 %에 적용, 2) 변경된 값으로 UI를 표시 | SCALE_IN_ORDER |

###### 2.2.1.3.3.2 분할 매수 / 매도 관리의 Region2(분할 매도)
| ID | 현재 상태 | EVENT | 가드 | Action | 다음 상태 |
| ----- | -------------- | ---------------------------- | ---- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------- |
| SO-01 | Initial Pseudo State | None | None | 1) 초기 세팅 값 가져오기, 2) 가져온 초기값을 화면에 표시 | SCALE_OUT_ORDER |
| SO-02 | SCALE_OUT_ORDER | SCALE_OUT_LEVEL_CHANGED | None | 1) 사용자가 선택한 값을 향후 매도 %에 적용, 2) 변경된 값으로 UI를 표시 | SCALE_OUT_ORDER |

##### 2.2.1.4 MAIN_SCREEN의 Region4(체결 내역 & 실시간 지표 탭)
| ID | 현재 상태 | EVENT | 가드 | Action | 다음 상태 |
| ----- | -------------- | ---------------------------- | ---- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------- |
| M4-01 | Initial Pseudo State | None | None | 1) 프로그램 실행 이후의 '체결 내역을 저장한 파일' 가져오기, 2) 가져온 체결 내역을 화면에 표시 | TRADE_HISTORY_DISPLAYED |
| M4-02 | TRADE_HISTORY_DISPLAYED | BUY_ORDER_EXECUTED | None | 1) 체결된 매수 주문의 정보 가져오기, 2) 가져온 매수 주문의 정보를 '체결 내역을 저장한 파일'에 저장, 3) 가져온 매수 정보를 체결 내역 상단에 표시 | TRADE_HISTORY_DISPLAYED |
| M4-03 | TRADE_HISTORY_DISPLAYED | SELL_ORDER_EXECUTED | None | 1) 체결된 매도 주문의 정보 가져오기, 2) 가져온 매도 주문의 정보를 '체결 내역을 저장한 파일'에 저장, 3) 가져온 매도 정보를 체결 내역 상단에 표시 | TRADE_HISTORY_DISPLAYED |
| M4-04 | TRADE_HISTORY_DISPLAYED | REALTIME_INDICATOR_CLICKED | None | 1) 현재 투자 상태에서 사용하는 지표의 목록을 가져온다, 2) 가져온 목록 속 지표의 값을 가져온다, 3) 가져온 지표 값을 표시, 4) 실시간 지표쪽으로 흰색 사각형이 슬라이드하여 이동한다 | REALTIME_INDICATOR_DISPLAYED |
| M4-05 | REALTIME_INDICATOR_DISPLAYED | REALTIME_INDICATOR_UPDATED | None | 1) 갱신된 실시간 지표 값을 가져온다, 2) 가져온 지표 값을 표시 | REALTIME_INDICATOR_DISPLAYED |
| M4-06 | REALTIME_INDICATOR_DISPLAYED | TRADING_STATUS_UPDATED | None | 1) 변경된 투자 상태에서 사용하는 지표의 목록을 가져온다, 2) 가져온 목록 속 지표의 값을 가져온다, 3) 가져온 지표 값을 표시 | REALTIME_INDICATOR_DISPLAYED |
| M4-07 | REALTIME_INDICATOR_DISPLAYED | BUY_ORDER_EXECUTED | None | 1) 체결된 매수 주문의 정보 가져오기, 2) 가져온 매수 주문의 정보를 '체결 내역을 저장한 파일'에 저장 | REALTIME_INDICATOR_DISPLAYED |
| M4-08 | REALTIME_INDICATOR_DISPLAYED | SELL_ORDER_EXECUTED | None | 1) 체결된 매도 주문의 정보 가져오기, 2) 가져온 매도 주문의 정보를 '체결 내역을 저장한 파일'에 저장 | REALTIME_INDICATOR_DISPLAYED |
| M4-09 | REALTIME_INDICATOR_DISPLAYED | TRADING_HISTORY_CLICKED | None | 1) 프로그램 실행 이후의 '체결 내역을 저장한 파일' 가져오기, 2) 가져온 체결 내역을 화면에 표시 | TRADE_HISTORY_DISPLAYED |

#### 2.2.2 TRADING_DETAILS 상태

##### 2.2.2.1 TRADING_DETAILS의 Region1(계좌 내역 상세)
###### 2.2.2.1.1 계좌 내역 상세의 Region1(수익률 표시)
| ID | 현재 상태 | EVENT | 가드 | Action | 다음 상태 |
| ----- | -------------- | ---------------------------- | ---- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------- |
| D1-01 | Initial Pseudo State | None | None | 1) 입출금을 보정한 수익률 값을 가져온다, 2) 가져온 수익률을 표시한다 | PROFIT_RATE_DISPLAYED |
| D1-02 | PROFIT_RATE_DISPLAYED | PROFIT_RATE_UPDATED | None | 1) 갱신된 수익률 값을 가져온다, 2) 가져온 수익률을 표시한다 | PROFIT_RATE_DISPLAYED |

###### 2.2.2.1.2 계좌 내역 상세의 Region2(매도 성과 표시)
| ID | 현재 상태 | EVENT | 가드 | Action | 다음 상태 |
| ----- | -------------- | ---------------------------- | ---- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------- |
| D2-01 | Initial Pseudo State | None | None | 1) 프로그램 실행 이후에 기록 된 매도 성과(수익 실현 여부, 평균 수익률, 총 수익)을 가져온다, 2) 가져온 정보를 표시한다 | TRADE_PERFORMANCE_DISPLAYED |
| D2-02 | TRADE_PERFORMANCE_DISPLAYED | SELL_ORDER_EXECUTED | None | 1) 갱신된 매도 성과(수익 실현 여부, 평균 수익률, 총 수익)을 가져온다, 2) 가져온 정보를 표시한다 | TRADE_PERFORMANCE_DISPLAYED |

###### 2.2.2.1.3 계좌 내역 상세의 Region3(ETH 보유 수량 표시)
| ID | 현재 상태 | EVENT | 가드 | Action | 다음 상태 |
| ----- | -------------- | ---------------------------- | ---- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------- |
| D3-01 | Initial Pseudo State | None | None | 1) ETH 보유 수량을 가져온다, 2) 가져온 정보를 표시한다 | ETH_HOLDINGS_DISPLAYED |
| D3-02 | ETH_HOLDINGS_DISPLAYED | BUY_ORDER_EXECUTED | None | 1) ETH 보유 수량을 가져온다, 2) 가져온 정보를 표시한다 | ETH_HOLDINGS_DISPLAYED |
| D3-03 | ETH_HOLDINGS_DISPLAYED | SELL_ORDER_EXECUTED | None | 1) ETH 보유 수량을 가져온다, 2) 가져온 정보를 표시한다 | ETH_HOLDINGS_DISPLAYED |

###### 2.2.2.1.4 계좌 내역 상세의 Region4(당일 수수료 표시)
| ID | 현재 상태 | EVENT | 가드 | Action | 다음 상태 |
| ----- | -------------- | ---------------------------- | ---- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------- |
| D4-01 | Initial Pseudo State | None | None | 1) 오늘 발생한 수수료 값을 가져온다, 2) 가져온 정보를 표시한다 | DAILY_TRADING_FEE_DISPLAYED |
| D4-02 | DAILY_TRADING_FEE_DISPLAYED | DAILY_TRADING_FEE_CHANGED | None | 1) 변경된 수수료 값을 가져온다, 2) 가져온 정보를 표시한다 | DAILY_TRADING_FEE_DISPLAYED |

##### 2.2.2.2 TRADING_DETAILS의 Region2(표시할 기간 선택)
| ID | 현재 상태 | EVENT | 가드 | Action | 다음 상태 |
| ----- | -------------- | ---------------------------- | ---- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------- |
| TD2-01 | Initial Pseudo State | None | None | 1) 기간 필터를 오늘로 설정, 2) Region3의 현재 매수/매도 필터와 결합, 3) 결합 조건에 맞는 거래 내역을 표시 | TODAY_TRADE_HISTORY_DISPLAYED |
| TD2-02 | TODAY_TRADE_HISTORY_DISPLAYED | SELECT_DISPLAY_WEEKLY_HISTORY | None | 1) 기간 필터를 최근 7일로 설정, 2) Region3의 현재 매수/매도 필터와 결합, 3) 결합 조건에 맞는 거래 내역을 표시 | WEEKLY_TRADE_HISTORY_DISPLAYED |
| TD2-03 | TODAY_TRADE_HISTORY_DISPLAYED | SELECT_DISPLAY_MONTHLY_HISTORY | None | 1) 기간 필터를 최근 30일로 설정, 2) Region3의 현재 매수/매도 필터와 결합, 3) 결합 조건에 맞는 거래 내역을 표시 | MONTHLY_TRADE_HISTORY_DISPLAYED |
| TD2-04 | TODAY_TRADE_HISTORY_DISPLAYED | SELECT_DISPLAY_ALL_HISTORY | None | 1) 기간 필터를 전체 기간으로 설정, 2) Region3의 현재 매수/매도 필터와 결합, 3) 결합 조건에 맞는 거래 내역을 표시 | ALL_TRADE_HISTORY_DISPLAYED |
| TD2-05 | WEEKLY_TRADE_HISTORY_DISPLAYED | SELECT_DISPLAY_TODAY_HISTORY | None | 1) 기간 필터를 오늘로 설정, 2) Region3의 현재 매수/매도 필터와 결합, 3) 결합 조건에 맞는 거래 내역을 표시 | TODAY_TRADE_HISTORY_DISPLAYED |
| TD2-06 | WEEKLY_TRADE_HISTORY_DISPLAYED | SELECT_DISPLAY_MONTHLY_HISTORY | None | 1) 기간 필터를 최근 30일로 설정, 2) Region3의 현재 매수/매도 필터와 결합, 3) 결합 조건에 맞는 거래 내역을 표시 | MONTHLY_TRADE_HISTORY_DISPLAYED |
| TD2-07 | WEEKLY_TRADE_HISTORY_DISPLAYED | SELECT_DISPLAY_ALL_HISTORY | None | 1) 기간 필터를 전체 기간으로 설정, 2) Region3의 현재 매수/매도 필터와 결합, 3) 결합 조건에 맞는 거래 내역을 표시 | ALL_TRADE_HISTORY_DISPLAYED |
| TD2-08 | MONTHLY_TRADE_HISTORY_DISPLAYED | SELECT_DISPLAY_TODAY_HISTORY | None | 1) 기간 필터를 오늘로 설정, 2) Region3의 현재 매수/매도 필터와 결합, 3) 결합 조건에 맞는 거래 내역을 표시 | TODAY_TRADE_HISTORY_DISPLAYED |
| TD2-09 | MONTHLY_TRADE_HISTORY_DISPLAYED | SELECT_DISPLAY_WEEKLY_HISTORY | None | 1) 기간 필터를 최근 7일로 설정, 2) Region3의 현재 매수/매도 필터와 결합, 3) 결합 조건에 맞는 거래 내역을 표시 | WEEKLY_TRADE_HISTORY_DISPLAYED |
| TD2-10 | MONTHLY_TRADE_HISTORY_DISPLAYED | SELECT_DISPLAY_ALL_HISTORY | None | 1) 기간 필터를 전체 기간으로 설정, 2) Region3의 현재 매수/매도 필터와 결합, 3) 결합 조건에 맞는 거래 내역을 표시 | ALL_TRADE_HISTORY_DISPLAYED |
| TD2-11 | ALL_TRADE_HISTORY_DISPLAYED | SELECT_DISPLAY_TODAY_HISTORY | None | 1) 기간 필터를 오늘로 설정, 2) Region3의 현재 매수/매도 필터와 결합, 3) 결합 조건에 맞는 거래 내역을 표시 | TODAY_TRADE_HISTORY_DISPLAYED |
| TD2-12 | ALL_TRADE_HISTORY_DISPLAYED | SELECT_DISPLAY_WEEKLY_HISTORY | None | 1) 기간 필터를 최근 7일로 설정, 2) Region3의 현재 매수/매도 필터와 결합, 3) 결합 조건에 맞는 거래 내역을 표시 | WEEKLY_TRADE_HISTORY_DISPLAYED |
| TD2-13 | ALL_TRADE_HISTORY_DISPLAYED | SELECT_DISPLAY_MONTHLY_HISTORY | None | 1) 기간 필터를 최근 30일로 설정, 2) Region3의 현재 매수/매도 필터와 결합, 3) 결합 조건에 맞는 거래 내역을 표시 | MONTHLY_TRADE_HISTORY_DISPLAYED |

##### 2.2.2.3 TRADING_DETAILS의 Region3(매도/매수 표시 선택)
| ID | 현재 상태 | EVENT | 가드 | Action | 다음 상태 |
| ----- | -------------- | ---------------------------- | ---- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------- |
| TD3-01 | Initial Pseudo State | None | None | 1) 거래 종류 필터를 전체로 설정, 2) Region2의 현재 기간 필터와 결합, 3) 결합 조건에 맞는 거래 내역을 표시 | ALL_TRADE_HISTORY_DISPLAYED |
| TD3-02 | ALL_TRADE_HISTORY_DISPLAYED | BUY_TRADE_HISTORY_SELECTED | None | 1) 거래 종류 필터를 매수로 설정, 2) Region2의 현재 기간 필터와 결합, 3) 결합 조건에 맞는 거래 내역을 표시 | BUY_TRADE_HISTORY_DISPLAYED |
| TD3-03 | ALL_TRADE_HISTORY_DISPLAYED | SELL_TRADE_HISTORY_SELECTED | None | 1) 거래 종류 필터를 매도로 설정, 2) Region2의 현재 기간 필터와 결합, 3) 결합 조건에 맞는 거래 내역을 표시 | SELL_TRADE_HISTORY_DISPLAYED |
| TD3-04 | BUY_TRADE_HISTORY_DISPLAYED | ALL_TRADE_HISTORY_SELECTED | None | 1) 거래 종류 필터를 전체로 설정, 2) Region2의 현재 기간 필터와 결합, 3) 결합 조건에 맞는 거래 내역을 표시 | ALL_TRADE_HISTORY_DISPLAYED |
| TD3-05 | BUY_TRADE_HISTORY_DISPLAYED | SELL_TRADE_HISTORY_SELECTED | None | 1) 거래 종류 필터를 매도로 설정, 2) Region2의 현재 기간 필터와 결합, 3) 결합 조건에 맞는 거래 내역을 표시 | SELL_TRADE_HISTORY_DISPLAYED |
| TD3-06 | SELL_TRADE_HISTORY_DISPLAYED | ALL_TRADE_HISTORY_SELECTED | None | 1) 거래 종류 필터를 전체로 설정, 2) Region2의 현재 기간 필터와 결합, 3) 결합 조건에 맞는 거래 내역을 표시 | ALL_TRADE_HISTORY_DISPLAYED |
| TD3-07 | SELL_TRADE_HISTORY_DISPLAYED | BUY_TRADE_HISTORY_SELECTED | None | 1) 거래 종류 필터를 매수로 설정, 2) Region2의 현재 기간 필터와 결합, 3) 결합 조건에 맞는 거래 내역을 표시 | BUY_TRADE_HISTORY_DISPLAYED |

##### 2.2.2.4 TRADING_DETAILS의 Region4(CSV 내보내기)
| ID | 현재 상태 | EVENT | 가드 | Action | 다음 상태 |
| ----- | -------------- | ---------------------------- | ---- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------- |
| TD4-01 | Initial Pseudo State | None | None | None | AWAITING_CSV_EXPORT_POPUP |
| TD4-02 | AWAITING_CSV_EXPORT_POPUP | CSV_EXPORT_CLICKED | None | 1) CSV 내보내기 팝업 화면을 표시한다 | CSV_EXPORT_POPUP_DISPLAYED |
| TD4-03 | CSV_EXPORT_POPUP_DISPLAYED | CLOSE_CSV_EXPORT_POPUP | None | 1) CSV 내보내기 팝업 화면을 제거한다 | AWAITING_CSV_EXPORT_POPUP |
| TD4-04 | CSV_EXPORT_POPUP_DISPLAYED | EXPORT_CSV | 저장 위치 선택 O && 파일명 유효 O && 기간 유효 O | 1) 선택된 설정을 적용하여 CSV 파일 생성 시작, 2) 내보내기 처리 중 상태 표시 | CSV_EXPORT_IN_PROGRESS |
| TD4-05 | CSV_EXPORT_POPUP_DISPLAYED | EXPORT_CSV | 저장 위치 선택 X \|\| 파일명 유효 X \|\| 기간 유효 X | 1) 유효하지 않은 설정 항목을 강조 표시, 2) 오류 원인을 표시 | CSV_EXPORT_POPUP_DISPLAYED |
| TD4-06 | CSV_EXPORT_IN_PROGRESS | CSV_EXPORT_SUCCEEDED | None | 1) 내보내기 처리 중 표시 제거, 2) 내보내기 완료 팝업 출력 | CSV_EXPORT_COMPLETE |
| TD4-07 | CSV_EXPORT_IN_PROGRESS | CSV_EXPORT_FAILED | None | 1) 내보내기 처리 중 표시 제거, 2) 내보내기 실패 원인 표시 | CSV_EXPORT_ERROR |
| TD4-08 | CSV_EXPORT_ERROR | CSV_EXPORT_ERROR_CONFIRMED | None | 1) 내보내기 실패 팝업 제거, 2) 기존 CSV 내보내기 설정 화면 표시 | CSV_EXPORT_POPUP_DISPLAYED |
| TD4-09 | CSV_EXPORT_COMPLETE | ACCEPT_CLOSE_ALL_POPUP | None | 1) 내보내기 완료 팝업 제거, 2) CSV 내보내기 팝업 화면 제거 | AWAITING_CSV_EXPORT_POPUP |

###### 2.2.2.4.1 CSV_EXPORT_POPUP_DISPLAYED의 Region1(파일 저장 위치 선택)
| ID | 현재 상태 | EVENT | 가드 | Action | 다음 상태 |
| ----- | -------------- | ---------------------------- | ---- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------- |
| CR1-01 | Initial Pseudo State | None | None | None | FILE_BROWSER_CLOSED |
| CR1-02 | FILE_BROWSER_CLOSED | SAVE_LOCATION_SELECT_CLICKED | None | 1) 파일탐색기 window를 표시 | FILE_BROWSER_OPENED |
| CR1-03 | FILE_BROWSER_OPENED | SAVE_LOCATION_CONFIRMED | None | 1) 해당 경로를 파일 저장 경로로 설정, 2) 파일탐색기 window를 제거 | FILE_BROWSER_CLOSED |
| CR1-04 | FILE_BROWSER_OPENED | SAVE_LOCATION_CANCELED | None | 1) 기존 파일 저장 경로를 유지, 2) 파일탐색기 window를 제거 | FILE_BROWSER_CLOSED |

###### 2.2.2.4.2 CSV_EXPORT_POPUP_DISPLAYED의 Region2(불러올 기간 선택)
| ID | 현재 상태 | EVENT | 가드 | Action | 다음 상태 |
| ----- | -------------- | ---------------------------- | ---- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------- |
| CR2-01 | Initial Pseudo State | None | None | 1) 오늘로 기간 설정 | CSV_TODAY_TRADE_HISTORY |
| CR2-02 | CSV_TODAY_TRADE_HISTORY | SELECT_CSV_WEEKLY_HISTORY | None | 1) 7일로 기간 설정 | CSV_WEEKLY_TRADE_HISTORY |
| CR2-03 | CSV_TODAY_TRADE_HISTORY | SELECT_CSV_MONTHLY_HISTORY | None | 1) 30일로 기간 설정 | CSV_MONTHLY_TRADE_HISTORY |
| CR2-04 | CSV_TODAY_TRADE_HISTORY | SELECT_CSV_DATE | None | 1) 시작일, 종료일 칸의 색 변화(회색 -> 파란색), 2) 시작일과 종료일 옆에 달력 아이콘의 등장 | CSV_SELECT_DATE |
| CR2-05 | CSV_WEEKLY_TRADE_HISTORY | SELECT_CSV_TODAY_HISTORY | None | 1) 오늘로 기간 설정 | CSV_TODAY_TRADE_HISTORY |
| CR2-06 | CSV_WEEKLY_TRADE_HISTORY | SELECT_CSV_MONTHLY_HISTORY | None | 1) 30일로 기간 설정 | CSV_MONTHLY_TRADE_HISTORY |
| CR2-07 | CSV_WEEKLY_TRADE_HISTORY | SELECT_CSV_DATE | None | 1) 시작일, 종료일 칸의 색 변화(회색 -> 파란색), 2) 시작일과 종료일 옆에 달력 아이콘의 등장 | CSV_SELECT_DATE |
| CR2-08 | CSV_MONTHLY_TRADE_HISTORY | SELECT_CSV_TODAY_HISTORY | None | 1) 오늘로 기간 설정 | CSV_TODAY_TRADE_HISTORY |
| CR2-09 | CSV_MONTHLY_TRADE_HISTORY | SELECT_CSV_WEEKLY_HISTORY | None | 1) 7일로 기간 설정 | CSV_WEEKLY_TRADE_HISTORY |
| CR2-10 | CSV_MONTHLY_TRADE_HISTORY | SELECT_CSV_DATE | None | 1) 시작일, 종료일 칸의 색 변화(회색 -> 파란색), 2) 시작일과 종료일 옆에 달력 아이콘의 등장 | CSV_SELECT_DATE |
| CR2-11 | CSV_SELECT_DATE | SELECT_CSV_TODAY_HISTORY | None | 1) 오늘로 기간 설정, 2) 시작일, 종료일 칸의 색 변화(파란색 -> 회색), 3) 시작일과 종료일 옆에 달력 아이콘의 삭제 | CSV_TODAY_TRADE_HISTORY |
| CR2-12 | CSV_SELECT_DATE | SELECT_CSV_WEEKLY_HISTORY | None | 1) 7일로 기간 설정, 2) 시작일, 종료일 칸의 색 변화(파란색 -> 회색), 3) 시작일과 종료일 옆에 달력 아이콘의 삭제 | CSV_WEEKLY_TRADE_HISTORY |
| CR2-13 | CSV_SELECT_DATE | SELECT_CSV_MONTHLY_HISTORY | None | 1) 30일로 기간 설정, 2) 시작일, 종료일 칸의 색 변화(파란색 -> 회색), 3) 시작일과 종료일 옆에 달력 아이콘의 삭제 | CSV_MONTHLY_TRADE_HISTORY |
| CR2-14 | CSV_SELECT_DATE | START_CSV_START_DATE_SELECTION | None | 1) 시작일 달력 팝업 open | CSV_START_DATE |
| CR2-15 | CSV_SELECT_DATE | START_CSV_FINISH_DATE_SELECTION | None | 1) 종료일 달력 팝업 open | CSV_FINISH_DATE |
| CR2-16 | CSV_START_DATE | START_DATE_SELECTED | 종료일 미선택 \|\| 선택한 시작일 <= 종료일 | 1) 선택한 날짜를 파란색으로 표시, 2) 시작일 설정 반영 | CSV_START_DATE |
| CR2-17 | CSV_START_DATE | START_DATE_SELECTED | 종료일 선택 O && 선택한 시작일 > 종료일 | 1) 시작일이 종료일보다 늦을 수 없음을 표시 | CSV_START_DATE |
| CR2-18 | CSV_START_DATE | START_DATE_CALENDAR_OUTSIDE_CLICKED | None | 1) 시작일 달력 팝업 close | CSV_SELECT_DATE |
| CR2-19 | CSV_FINISH_DATE | FINISH_DATE_SELECTED | 시작일 미선택 \|\| 시작일 <= 선택한 종료일 | 1) 선택한 날짜를 파란색으로 표시, 2) 종료일 설정 반영 | CSV_FINISH_DATE |
| CR2-20 | CSV_FINISH_DATE | FINISH_DATE_SELECTED | 시작일 선택 O && 시작일 > 선택한 종료일 | 1) 종료일이 시작일보다 이를 수 없음을 표시 | CSV_FINISH_DATE |
| CR2-21 | CSV_FINISH_DATE | FINISH_DATE_CALENDAR_OUTSIDE_CLICKED | None | 1) 종료일 달력 팝업 close | CSV_SELECT_DATE |

###### 2.2.2.4.3 CSV_EXPORT_POPUP_DISPLAYED의 Region3(저장할 파일 이름 입력)
| ID | 현재 상태 | EVENT | 가드 | Action | 다음 상태 |
| ----- | -------------- | ---------------------------- | ---- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------- |
| CR3-01 | Initial Pseudo State | None | None | 1) 파일의 저장 이름 기본값을 표시 | DEFAULT_FILE_NAME |
| CR3-02 | DEFAULT_FILE_NAME | FILE_NAME_CLICKED | None | 1) 타이핑으로 파일 이름 입력 가능 | NEW_FILE_NAME_TYPED |
| CR3-03 | NEW_FILE_NAME_TYPED | ENTER_KEY_TYPED | 파일명 유효 O | 1) 저장할 파일명으로 설정 | FILE_NAME_WRITTEN |
| CR3-04 | NEW_FILE_NAME_TYPED | FILE_NAME_INPUT_FOCUS_LOST | 파일명 유효 O | 1) 저장할 파일명으로 설정 | FILE_NAME_WRITTEN |
| CR3-05 | NEW_FILE_NAME_TYPED | ENTER_KEY_TYPED | 파일명 유효 X | 1) 빈 파일명 또는 사용할 수 없는 문자를 강조 표시 | NEW_FILE_NAME_TYPED |
| CR3-06 | NEW_FILE_NAME_TYPED | FILE_NAME_INPUT_FOCUS_LOST | 파일명 유효 X | 1) 빈 파일명 또는 사용할 수 없는 문자를 강조 표시 | NEW_FILE_NAME_TYPED |
| CR3-07 | FILE_NAME_WRITTEN | FILE_NAME_CLICKED | None | 1) 타이핑으로 파일 이름 입력 가능 | NEW_FILE_NAME_TYPED |

### 2.3 Etire UI System의 Region_3(프로그램 종료)
| ID | 현재 상태 | EVENT | 가드 | Action | 다음 상태 |
| ----- | -------------- | ---------------------------- | ---- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------- |
| ES3-01 | Initial Pseudo State | None | None | None | AWAITING_EXIT |
| ES3-02 | AWAITING_EXIT | EXIT_CLICKED | 포지션 보유 중 O | 1) 종료 시 보유한 포지션이 강제 매도됨을 알리는 팝업 표시 | FORCE_SELL_EXIT_POPUP_DISPLAYED |
| ES3-03 | AWAITING_EXIT | EXIT_CLICKED | 포지션 보유 중 X | 1) 종료 확인 팝업 표시 | EXIT_POPUP_DISPLAYED |
| ES3-04 | FORCE_SELL_EXIT_POPUP_DISPLAYED | FORCE_SELL_EXIT_CANCELED | None | 1) 종료 시 보유한 포지션이 강제 매도됨을 알리는 팝업 제거 | AWAITING_EXIT |
| ES3-05 | FORCE_SELL_EXIT_POPUP_DISPLAYED | FORCE_SELL_EXIT_CONFIRMED | None | 1) 포지션 강제 매도 주문 제출, 2) 강제 매도 처리 중 상태 표시 | FORCE_SELL_EXIT_PROCESSING |
| ES3-06 | FORCE_SELL_EXIT_PROCESSING | FORCE_SELL_EXIT_SUCCEEDED | None | 1) 자동매매 중단, 2) API 및 WebSocket 연결 종료, 3) 저장 데이터 반영, 4) 프로그램 종료 | UI_FINAL_STATE |
| ES3-07 | FORCE_SELL_EXIT_PROCESSING | FORCE_SELL_EXIT_FAILED | None | 1) 종료 실패 원인 표시, 2) 일반 종료 확인에서 안전 조건 재평가; 청산 동의 요구 오류는 청산 확인으로, 종료 결과 불명·프로세스 대기 시간 초과는 해당 복구 상태로 이동 | 일반 실패: EXIT_POPUP_DISPLAYED / 청산 동의 필요: FORCE_SELL_EXIT_POPUP_DISPLAYED / 그 외: 종료 복구 상태 |
| ES3-08 | EXIT_POPUP_DISPLAYED | EXIT_CANCELED | None | 1) 종료 확인 팝업 제거 | AWAITING_EXIT |
| ES3-09 | EXIT_POPUP_DISPLAYED | EXIT_CONFIRMED | None | 1) 자동매매 중단, 2) API 및 WebSocket 연결 종료, 3) 저장 데이터 반영, 4) 프로그램 종료 | UI_FINAL_STATE |
