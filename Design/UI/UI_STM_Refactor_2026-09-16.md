# UI STM 계층 복원 구현·검증 및 문서 충돌 기록

기준: 2026-09-16 착수 시 작업 폴더, commit `c5bce4b`. 착수 시 미저장 변경은 없었으며, 이미 반영된 계좌 잔여 자산·평단가·risk 상태와 최신 종료 동작을 그대로 기준으로 삼았다. 구조 기준은 `UI_Event_Action_Table.md`, REGIME 조작 기준은 `UI_Rule.md` CR-03이다.

## 1. 구현 결과와 코드 읽기 경로

UI를 실행하는 XState actor는 하나다. `get_snapshot().value`에서 다음 포함 관계를 직접 확인할 수 있다. 아래 경로의 요소는 이름만 붙인 분류가 아니라 실제 활성 상태 노드다.

| 루트 아래 경로 | 실제 구성 |
|---|---|
| `ETIRE_UI_SYSTEM` | 상단 표시, 화면 선택, 종료의 3개 병렬 Region |
| `UPPER_STATUS_BAR` | `API_DISPLAY`, `STOP_BUTTON`, `START_BUTTON`의 3개 병렬 Region |
| `SCREEN.MAIN_SCREEN_WRAPPER` | REGIME, 차트, 계좌 정보, 트레이더 탭의 4개 병렬 Region |
| `MAIN_SCREEN_WRAPPER.REGIME_PANEL` | 추천·선택·지표의 3개 병렬 Region |
| `MAIN_SCREEN_WRAPPER.DISPLAY_CHART` | 기존 6개 Region 및 지표 설정 내부 3개 Region |
| `MAIN_SCREEN_WRAPPER.DISPLAY_ACCOUNT_INFO` | 투자 상태·자산·분할 설정의 3개 Region, 분할 설정 내부 매수·매도 2개 Region |
| `SCREEN.TRADING_DETAILS` | 상세 요약·기간·거래 종류·CSV의 4개 병렬 Region |
| `TRADING_DETAILS.ACCOUNT_DETAILS` | 수익률·매도 성과·ETH·수수료의 4개 Region |
| `TRADING_DETAILS.PERIOD` / `SIDE` | 각각 실제 선택 상태. 기간 영역의 `query` 하위 상태가 결합 조회의 대기·성공·빈 결과·오류를 관리 |
| `TRADING_DETAILS.CSV_EXPORT.editing` | 경로·기간·파일명 3개 Region. 파일명은 기본값·입력 중·확정 상태 구분 |
| `UI_FINAL_STATE` | 모든 Region과 소유 비동기 작업을 종료하는 최상위 final |

코드 읽기 순서는 다음과 같다.

1. [UiApplicationFacade.ts](/Users/oscar/Desktop/Binance_Auto/UI/src/app/control/UiApplicationFacade.ts): 루트 생성·구독·시작·종료 및 기존 외부 계약.
2. [uiApplicationIntents.ts](/Users/oscar/Desktop/Binance_Auto/UI/src/app/control/uiApplicationIntents.ts): 외부 intent를 한 번의 루트 내부 이벤트 묶음으로 변환. 비활성 화면의 사용자 입력은 거절한다.
3. [uiApplicationMachine.ts](/Users/oscar/Desktop/Binance_Auto/UI/src/app/machines/uiApplicationMachine.ts): 실제 계층, 공통 서버 snapshot, deep history, 최상위 종료.
4. [uiFeatureDefinitions.ts](/Users/oscar/Desktop/Binance_Auto/UI/src/app/machines/uiFeatureDefinitions.ts), [uiRegionComposition.ts](/Users/oscar/Desktop/Binance_Auto/UI/src/app/machines/uiRegionComposition.ts): 기존 기능 정의를 실제 루트 상태 노드로 연결한다. 기능별 UI actor를 만들거나 실행하지 않는다. XState 5.32.5의 기존 가드·Action·assign 정의를 공통 context의 해당 데이터에 연결하며, 전이 판정 자체는 XState가 실행한다.
5. 기능별 새 포함 관계: [tradingRegions.ts](/Users/oscar/Desktop/Binance_Auto/UI/src/features/trading-control/machines/tradingRegions.ts), [regimeRegions.ts](/Users/oscar/Desktop/Binance_Auto/UI/src/features/regime-selection/machines/regimeRegions.ts), [splitOrderRegions.ts](/Users/oscar/Desktop/Binance_Auto/UI/src/features/split-order/machines/splitOrderRegions.ts), [historyFilterRegions.ts](/Users/oscar/Desktop/Binance_Auto/UI/src/features/trade-history/machines/historyFilterRegions.ts), [csvExportRegions.ts](/Users/oscar/Desktop/Binance_Auto/UI/src/features/csv-export/machines/csvExportRegions.ts).
6. [uiCommandExecutor.ts](/Users/oscar/Desktop/Binance_Auto/UI/src/app/machines/uiCommandExecutor.ts): 루트가 소유하는 비동기 명령 실행부. 자식 actor는 HTTP·폴더 선택·CSV·종료·타이머 작업만 실행한다.
7. [selectAppViewModel.ts](/Users/oscar/Desktop/Binance_Auto/UI/src/app/control/selectAppViewModel.ts), [uiApplicationViews.ts](/Users/oscar/Desktop/Binance_Auto/UI/src/app/machines/uiApplicationViews.ts), [uiModalPolicy.ts](/Users/oscar/Desktop/Binance_Auto/UI/src/app/control/uiModalPolicy.ts): 루트 경로와 공통 데이터로 기존 화면 모델·모달 우선순위를 제공한다. 읽기용 영역 투영은 별도 snapshot이나 실행 actor가 아니다.

시작·중지는 `context.features.trading` 하나를 공유한다. 분할 매수·매도는 실제 독립 상태를 가지되 기존 최신 요청 처리 방식을 유지한다. 루트의 `server_snapshot`에 전체 수신값을 먼저 보관하고 내부 이벤트로 영역을 동기화하며, 처리 중간 화면 모델을 구독자에게 공개하지 않는다.

메인 내부의 실제 deep history가 차트·지표·선·탭 상태를 복원한다. 상세 화면의 보조 history는 CSV 작업 상태 등을 유지한다. history 복귀는 저장 명령의 시작 조건이 아니다. 명령 시작은 명시적인 입력 또는 작업 결과 전이에 연결되고, 화면 밖의 완료 Action은 한 번 적용한 뒤 복귀 시 상태만 정리한다. 새로운 서버 확정값이 이전 작업을 무효화하면 해당 요청의 오래된 결과를 무시한다. 강조 타이머는 숨겨진 동안에도 원래 만료 시점까지 진행한다. 상세 조회는 이탈 시 실제 AbortSignal로 취소한다.

`UiApplicationIntent`, `AppViewModel`, `UiCommandPort`, Store·bootstrap 계약과 XState 버전은 유지했다. 이전 `.actors` 묶음과 shell actor 실행 경로는 제거했다. 기능별 기존 machine factory는 정의와 단위 테스트에 사용하며 실제 앱이 실행하는 UI 상태 엔진은 루트 하나다.

## 2. 문서 충돌의 최소 수정 기록

공통 근거: [루트 표 행 행동 테스트](/Users/oscar/Desktop/Binance_Auto/UI/src/app/machines/uiEventActionCoverage.test.ts). 아래 ID 묶음은 해당 입력과 결과를 직접 검증하는 시나리오 이름이다. Region 계층과 182개 기존 ID는 삭제·평탄화하지 않았다.

| 문서 위치 | 현재 코드·행동 근거 | 반영한 문장 또는 의미 |
|---|---|---|
| `UI_Event_Action_Table.md` 1.3.1.1.2, R2-02~05 | `regimeMachine.ts`의 후보·확인·적용 Promise, R2-01~05 테스트 | 실행·정지 모두 후보 선택 → 확인창 → backend 적용 성공 후 확정. 취소·실패는 이전 적용값 유지. |
| `UI_Behavior.md` 3절 type0~type4 | 같은 R2 테스트, `RegimePanel.tsx`의 applied/candidate 구분 | 클릭 즉시 실제 전략 적용으로 읽히던 설명을 후보 선택·확인·성공 후 확정으로 수정. |
| `UI_Rule.md` CR-03, VR-05, VR-14 | R2 테스트의 명령 실패 및 성공 전 적용값 유지 | 확인 버튼은 적용 요청이고 실제 확정은 성공 응답 후라는 시점 보충. |
| `UI_Rule.md` CR-15, ER-14, ER-16 | `csvExportMachine.ts`의 날짜 전이는 target 없이 값만 반영, CR2-14~21 | 유효한 날짜 선택 후 달력 유지. 외부 클릭으로 달력만 닫음. 유효하지 않은 입력은 기존값 유지 및 오류 표시. |
| `UI_Rule.md` CR-13·15, `UI_Behavior.md` 12절 오늘 버튼 | CSV `select_today/weekly/monthly`, CR2-02~13 | 기간·날짜 변경은 파일명을 자동 변경하지 않음. 파일명 기본값은 창을 새로 열 때 설정. |
| `UI_Rule.md` ER-11·12, EAT 공통 주석의 TD2·TD3 | `tradeHistoryMachine.ts`의 `has_entered`, `prepare_initial_query`, 루트 복귀·조회 취소 테스트 | 최초 진입 오늘·전체, 재진입 기존 필터 유지 후 재조회. 이탈 시 진행 조회 취소 및 늦은 응답 무시. |
| `UI_Rule.md` CR-10·VR-10, EAT 공통 주석 | 조회 중 필터 상태·요청 수 불변 테스트, `should_publish_summary`, summary revision 경쟁 테스트 | 조회 중 필터 입력 무시. 필터 변경 조회는 기존 요약 유지. 요약 갱신 경로와 오래된 응답 방어 구분. |
| `UI_Rule.md` ER-13 | CSV `reset_draft`, TD4·CR2-01 테스트 | 새 CSV 창은 오늘·경로 미선택·기본 파일명. 상세 화면 필터를 자동 상속한다는 여지를 제거. |
| EAT 2절 공통 주석, M4 체결 저장 표현 | `recentOrdersMachine.ts`는 수신 목록 갱신만 수행; M4-01~09, backend `trade_history_controller.py`의 저장 책임 | UI는 backend에서 저장된 체결을 수신·표시. 영속 저장·거래 세션 초기화는 backend 책임. 화면 복귀는 다시 초기화하는 명령이 아님. |
| EAT 2절 공통 주석, U2·U3·ES3 완료 의미 | 기존 lifecycle 수락/완료 테스트와 루트 final·종료 복구 테스트 | 요청 수락과 완료 구분. 중지·청산은 authoritative 완료, 종료는 backend 준비와 네이티브 종료 확인 뒤 final. |
| EAT 1.4 종료 상태 설명, ES3-07 | 착수 기준의 `appExitMachine.ts` `shutting_down.onError`, ES3-01~09 및 typed 종료 오류 테스트 | 일반 종료 준비 실패는 일반 종료 재확인. 청산 동의 요구는 청산 확인, 결과 불명·프로세스 종료 대기 시간 초과는 해당 복구 상태. 이미 반영되어 있던 종료 정책을 보존. |
| 9월 15일 구현 설명서 | 이번 구조 변경 | 이전 구조 분석임을 첫머리에 표시하고 최신 루트·입력·명령·selector 읽기 경로만 추가. 기존 본문·PDF·이미지는 재작성하지 않음. |

## 3. 검증 결과

- 착수 기준 관련 테스트: 15개 파일, 109개 통과.
- 최종 UI 회귀: **54개 파일, 637개 테스트 통과**. 실제 로컬 Python backend 연동 테스트 포함.
- TypeScript 타입 검사 및 Vite production build 통과.
- [uiEventActionCoverage.test.ts](/Users/oscar/Desktop/Binance_Auto/UI/src/app/machines/uiEventActionCoverage.test.ts): 72개 실행 시나리오/검사에서 **원문 182개 ID 모두**를 실제 입력·화면 값·상태·명령 결과에 연결한다. 마지막 검사는 문서 ID와 행동 시나리오 등록 집합을 대조하며, metadata 존재 여부로 행동 검증을 대체하지 않는다.
- [uiApplicationMachine.test.ts](/Users/oscar/Desktop/Binance_Auto/UI/src/app/machines/uiApplicationMachine.test.ts): 실제 계층, history, 화면 밖 응답, 연속 분할 입력, 오래된 결과 무시, 조회 중 체결 재조회, 자정 갱신, 강조 시간, CSV 복귀, 종료 복구 및 root final 검증.
- 기존 Facade의 원자적 snapshot 발행 테스트와 mock/bootstrap/React 테스트 유지. 비활성 메인 화면에서 REGIME을 누르거나 상세 진입 전에 CSV를 여는 내부 테스트는 실제 화면 순서로 수정했다.

검증은 mock 명령과 기존 로컬 backend fixture를 사용했다. 실제 Binance 주문 제출 및 데스크톱 네이티브 바이너리 패키징을 새로 실행한 결과를 의미하지 않는다. 전체 테스트 중 기존 `main.test.tsx`의 React `act(...)` 경고가 출력되지만 실패한 테스트는 없다.
