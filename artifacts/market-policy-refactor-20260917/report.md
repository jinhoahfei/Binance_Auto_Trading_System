# 시장 접촉 판단의 STM 통합 — 변경 및 검증 결과

2026-09-17, 기준 커밋 `3bc5f5541b915b1019ec198fb924aab5b866fea0`의 매매 동작을 유지하는 리팩터링을 적용했다.

## 변경 결과

- Controller의 `_select_market_event_type()`과 두 호출을 제거했다. 시장 관측은 원본 스냅샷·발생 시각·시장 version을 가진 `MARKET_DATA_UPDATED`로 전달한다.
- Controller는 검증·큐·경과시간·현재 lower scope 연결과 액션 실행을 유지한다. STM의 `global_transitions`에서 G-02/G-03/G-07의 조건과 액션을 함께 결정한다.
- 상단 관측 시 주문·포지션·준비 중 intent가 있으면 기존 Case 평가를 계속한다. 모두 없으면 이전 하단 신호·timer를 정리하고 하단 감시로 복귀한다. 명시적 상단 event의 무액션 G-07 호환성도 유지했다.
- 스냅샷 없는 내부 timer·retry는 새 밴드 접촉으로 해석하지 않는다.
- 사용되지 않던 `UpperBandPolicy`와 configuration 필드·내보내기를 제거했다. 지원 REGIME, 시작 Guard, 109개 전이 목록은 유지한다.
- Event-Action Table, ADR-001, STM 구현 문서의 책임 구분과 오래된 종료 정책 설명을 정정했다.

접촉 정책의 조건·후속 액션은 이제 전역 전이 구현에서 수정할 수 있다. Controller나 설명용 정책 설정에 같은 결정을 다시 반영할 필요가 없다. STM 엔진 API, UI/API·저장 형식, Case B/C 매매 기준은 변경하지 않았다. 실제 시장 입력 로그의 유형만 계획대로 `MARKET_DATA_UPDATED`가 되고 기존 전이 ID로 판단을 추적한다.

## 변경 전후 동등성

production 수정 전에 기존 상단 연속성 통합 테스트 7개에서 35개 지점을 기록했다. 최종 코드로 같은 입력을 재실행한 결과 아래 항목이 전부 동일했다.

- STM 상태·전이 ID·액션 순서와 인자·판단 ID·Context version
- 전체 시장·포지션·전략 Context
- 예약 평가와 주문 조회 timer
- 가짜 거래소에 제출한 주문
- UI로 전달하는 세션·전략·실시간 지표 데이터

세션 UUID만 고정하고 값의 타입을 JSON으로 직렬화했다. 비교 대상에서 매매 값이나 timer를 제거하지 않았다. 입력 event 유형은 별도 로그·관측 테스트에서 검증했다. 전체 JSON의 변경 전후 SHA-256도 일치했다.

[시나리오·단계 목록과 비교 해시](/Users/oscar/Desktop/Binance_Auto/artifacts/market-policy-refactor-20260917/behavior-comparison.json)

## 추가 검증

- Controller 없이도 실제 관측과 명시적 접촉이 같은 G-02/G-03/G-07 상태·액션을 생성한다.
- 양수 밴드, 현재가 접촉, 새 봉 식별자, 소유권·pending, C 소비·회복 조건을 유지한다.
- 상단과 다음 하단 관측을 미리 큐에 넣어도 처리 시점의 lower scope로 판단하며, 이전 scope의 timer는 새 scope로 연결하지 않는다.
- 상단 관측 적재 후 앞선 시장 입력에서 미결 BUY 또는 체결 포지션이 생겨도 G-07로 초기화하지 않는다. C는 익절 추적을 계속한다.
- 실제 주문 준비 단계에 일시적 실패를 주입했을 때 주문 ID 없는 intent와 BUY 재시도 timer가 상단 접촉 이후에도 유지된다.
- 미결 SELL도 상단 접촉으로 취소·초기화하지 않는다.
- 기존 상단→하단 재진입, 신호·timer 정리, BBW 경계, B/C 보유 관리, 사용자 STOP, 시간·버전·큐 순서 회귀 검사를 유지한다.

## 검사 결과

| 검사 | 결과 |
| --- | --- |
| 전체 백엔드, 프로젝트 가상환경 Python 3.11.14 | 1,233개 실행: 1,223개 통과, 실제 거래소 opt-in 10개 건너뜀 |
| 관련 UI·실제 백엔드 event 재생 | 54개 통과 |
| TypeScript | 통과 |
| 변경 전후 동등성 | 7개 시나리오, 35개 지점 모두 동일 |
| 변경 공백 검사 | 통과 |

첫 전체 실행은 전역 Python 3.14와 샌드박스에서 진행해 로컬 HTTP/WebSocket 포트 제한 및 기존 민감 값 출력 검사의 traceback 형식 차이로 실패했다. 프로젝트 검증 스크립트가 우선 사용하는 가상환경과 로컬 통신이 허용된 실행에서 전체 검사를 다시 통과했다. 해당 실패를 숨기기 위한 production 변경은 하지 않았다.

- [최종 백엔드 검사](/Users/oscar/Desktop/Binance_Auto/artifacts/market-policy-refactor-20260917/backend-tests-verified.log)
- [최초 환경 제약 실패 기록](/Users/oscar/Desktop/Binance_Auto/artifacts/market-policy-refactor-20260917/backend-tests.log)
- [UI 검사](/Users/oscar/Desktop/Binance_Auto/artifacts/market-policy-refactor-20260917/ui-tests.log)
- [타입 검사](/Users/oscar/Desktop/Binance_Auto/artifacts/market-policy-refactor-20260917/typecheck.log)

검증은 가짜 거래소·임시 저장소로 수행했고 실제 거래소 opt-in은 껐다. 설치본 교체, 앱 재빌드, 실거래 재시작은 수행하지 않았다. 변경 사항은 현재 작업 소스에 적용되어 있다.
