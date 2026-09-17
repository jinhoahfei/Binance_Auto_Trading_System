# 상단 접촉 후 세션 유지 및 하단 감시 재개 검증

2026-09-17 사용자 요청에 따라 G-07의 상단 접촉 안전 종료 정책을 변경했다.

## 변경된 동작

- 무포지션이며 미결 주문·제출 전 주문 의도도 없으면 상단에서 새 주문 없이 이전 하단 이벤트의 B/C 신호와 타이머를 정리하고 `LOWER_TOUCH_WATCH`로 복귀한다. 세션은 `running`, 거래 단계는 `IDLE`로 유지한다.
- 다음 하단 접촉은 같은 30분봉 안에서도 새 G-02 이벤트로 처리한다. Case B는 새로운 접촉의 BBW < 0.02를 통과해야 하며, Case C도 기존 진입 조건을 그대로 적용한다.
- 포지션·주문 처리 중인 경우 상단 접촉 자체는 강제매도·취소·종료를 요청하지 않는다. 일반 시장 입력을 기존 Case에 전달해 익절·손절·추적과 주문 처리를 계속한다.
- 사용자 중지와 기존 청산·재조정·재시도 경로는 유지한다.
- 지표 화면의 문구는 ‘상단 밴드 안전 종료’에서 ‘상단 밴드 접촉’으로 변경했다. 지표 전송 ID와 구조는 유지한다.

실제 동작 변경은 G-07 전이와 시장 이벤트 분류 두 곳에 한정했다. 정책 이름, 설명 주석, 표시 문구, 관련 테스트 및 설계 문서를 함께 갱신했다. TradingSTM 엔진 구조, B/C 진입 임계값, 주문 실행 구조, 통신 구조는 변경하지 않았다. 사용자가 수정 중이던 Visual Paradigm 파일에는 손대지 않았다.

## 요청한 흐름 재현 결과

실제 2026-09-17 실행 로그의 하단 접촉 및 종료 당시 상단 접촉 수치를 production Controller·TradingSTM·직렬 event queue에 입력했다. 후속 하단 접촉은 재진입을 검증하기 위한 합성 입력이다. 실제 당시 시세 전체를 재생한 백테스트나 거래소 실시간 시험은 아니다.

| 입력 | 전이 | 세션 | 상태·화면 데이터 |
| --- | --- | --- | --- |
| 하단 접촉: 2,378.06 ≤ 2,379.255257… | G-02, C-02, B-03 등 | running | B_WAIT_SIGNAL / C_WAIT_SETUP, B/C 지표 제공 |
| 상단 접촉: 2,419.61 ≥ 2,419.557348… | G-07 | running | LOWER_TOUCH_WATCH, 하단 가격 지표 제공 |
| 상단 위 가격 반복 | 추가 전이 없음 | running | 하단 대기 유지 |
| 같은 30분봉 안의 하단 재접촉 | 새 G-02, C-02, B-03 등 | running | 새 lower_event_id, B/C 감시·지표 재개 |
| 상단 재접촉 | G-07 | running | 하단 대기 복귀 |
| 다음 30분봉의 하단 재접촉 | 새 G-02, C-02, B-03 등 | running | B/C 감시·지표 재개 |

모든 단계에서 세션 ID가 유지되고 polling snapshot과 실시간 event의 데이터가 일치했다. 이 재현 흐름의 가짜 주문 제출도 0건이다.

추가 검증:

- 상단과 가격이 정확히 같은 경계도 복귀하며, 상단 미만에서는 G-07이 실행되지 않는다.
- 이전 B 확정 신호를 지워 다음 하단에서 새 신호 없이 눌림 매수하지 않는다.
- 이전 C setup·반등 타이머를 지워 다음 하단에 이월하지 않는다.
- 다음 하단의 BBW가 0.02이면 기존 계약대로 B는 진입 감시에서 제외하고 C만 감시한다.
- 미결 BUY는 상단에서 취소하지 않으며 같은 주문의 후속 체결을 정상 반영한다.
- 상단 위에서도 B의 TREND_HOLD 및 C의 TP_TRAILING 조건 전이가 실행된다.
- 직접 상단 이벤트를 전달해도 B/C 보유, 미결 주문, 제출 전 주문 의도를 변경하지 않는다.
- 상단 복귀 후에도 사용자 중지는 정상 종료하며, 보유 포지션 청산·미결 주문 조정·실패 재시도 검사도 통과한다.

## 검사 결과

| 검사 | 결과 |
| --- | --- |
| 신규 상단→하단 통합 검사 | 7개 통과 |
| 백엔드 전체 회귀 검사 | 1,223개 실행: 1,213개 통과, 실제 거래소 opt-in 10개 건너뜀 |
| 관련 UI·실제 백엔드 event 재생 검사 | 54개 통과 |
| TypeScript 검사 | 통과 |
| macOS 개발 앱 빌드 | 완료 |
| 생성된 앱의 내장 백엔드 코드 검사 | 41개 통과, production 모듈 108개를 패키지에서 직접 로드, 소스 fallback 금지 |
| 생성된 앱의 로컬 서명 검증 | `codesign --verify --deep --strict` 통과 |
| 변경 내용 공백 검사 | 통과 |

첫 전체 검사에서는 샌드박스의 로컬 포트 열기 제한으로 transport 테스트가 실패했다. 실제 거래소 opt-in을 끄고 API 환경 변수를 제거한 뒤 로컬 통신이 허용된 환경에서 전체 검사를 다시 실행해 통과했다. 최초 실패 로그와 최종 통과 로그를 구분해 보존했다.

실제 계좌·주문은 사용하지 않았다. 신규 통합 검사에서는 외부 socket 연결을 차단하고 가짜 거래소와 임시 이력 파일을 사용했다.

## 결과 파일과 앱

- [재현 단계별 실제 상태 JSON](/Users/oscar/Desktop/Binance_Auto/artifacts/upper-band-continuation-20260917/replay-evidence.json)
- [신규 통합 검사](/Users/oscar/Desktop/Binance_Auto/artifacts/upper-band-continuation-20260917/continuation-tests.log)
- [최종 전체 백엔드 검사](/Users/oscar/Desktop/Binance_Auto/artifacts/upper-band-continuation-20260917/backend-tests-verified.log)
- [UI 검사](/Users/oscar/Desktop/Binance_Auto/artifacts/upper-band-continuation-20260917/ui-tests.log)
- [내장 코드 검사](/Users/oscar/Desktop/Binance_Auto/artifacts/upper-band-continuation-20260917/bundled-tests.log)
- [빌드 로그](/Users/oscar/Desktop/Binance_Auto/artifacts/upper-band-continuation-20260917/app-build.log)
- [개발용 실행 앱](</Users/oscar/Desktop/Binance_Auto/UI/apps/desktop/src-tauri/target/debug/bundle/macos/Binance Auto Trader.app>)

이 앱은 로컬 개발 빌드다. `/Applications`의 기존 설치본은 교체하지 않았으며 새 앱 실행과 자동매매 시작도 하지 않았다. 기존 설치본을 실행하면 이전 종료 정책이 남아 있을 수 있으므로 적용 대상은 위 새 빌드다.
