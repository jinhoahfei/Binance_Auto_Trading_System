# case_B 종료 강제매도 사유 수정

작성일: 2026-09-27

## 확인된 원인

최근 case_B 매수는 2026-09-26 19:55:42.536 KST, 매도는 같은 날 20:41:29.807 KST다.
매도 주문 ID는 `50138412055`다.

- 20:17:12 일반익절 시도는 주문 준비의 금액 필터에서 `SymbolFilterError`로 실패했다. 진단의 실패 코드는 `SYMBOL_FILTER_REJECTED`다.
- 20:41:29 매도는 종료 준비의 STOP 이벤트에서 생성됐다. 주문 의도는 `force-sell:7ddfd416-00df-49e1-aa1e-b5049c8dbbc0`이며 제출 진단에 `force_sell=true`가 기록되어 있다.
- 당시 `_submit_force_sell()`은 `pending_exit_reason or ExitReason.STOP`을 사용했다. 실패한 일반익절의 `TAKE_PROFIT` 값이 남아 있어 새 강제매도 주문과 체결 내역에 그대로 저장됐다.
- 최근 체결·상세 거래 화면은 외부 수동 매도만 별도로 표시하고 나머지는 소유 전략 `CASE_B`를 표시했다.

선별한 원본 로그 위치와 내용은 [evidence.json](evidence.json)에 보존했다.

## 수정

- 강제매도 주문과 대기 상태에 전용 사유 `FORCE_SELL`을 명시하고 이전 전략의 매도 판단값·복귀 상태를 정리한다.
- 기존 `STOP`은 case_B·case_C의 일반 손절에도 사용되므로 그대로 보존한다. 강제매도와 혼동하지 않는다.
- 최근 체결과 상세 거래 내역은 `FORCE_SELL`을 **강제매도**로 표시한다. 포지션 소유 전략은 기존 case_B·case_C 값을 보존한다.
- 실거래·Testnet 권한 검사와 거래소 필터 준비가 새 사유를 기존 청산 경로와 동일하게 처리한다. 주문 권한·수수료 검사와 일반 주문 금액 제한을 검증했다.
- STOP 전에 이미 제출된 전략 주문의 체결은 원래 사유를 유지하고, 잔량 강제매도만 별도의 사유로 기록한다.

## 기존 기록 교정 및 설치

대상 주문 한 건의 `exit_reason`을 `TAKE_PROFIT`에서 `FORCE_SELL`로 교정했다. 같은 주문의 준비 일지도 일치시켰으며, 잔여 장부에서 체결 6건과 연결된 검증 해시만 재계산했다. 수량·금액·체결가·수수료·원가·손익·전략·시각은 보존했다.

앱의 OS 소유권 잠금을 확보하고 `RELEASED` 상태를 확인한 뒤 적용했다. 교정 전후에 실제 저장소·잔여 장부 복원 로직을 실행해 포지션, 잔여 수량·원가, 수수료, 실현손익과 성과가 모두 동일함을 확인했다. 미결 주문은 0건이다.

수정 앱을 `/Applications/Binance Auto Trader.app`에 설치했다. 설치본 5개 파일은 빌드와 해시가 일치하며 앱 서명 검증을 통과했다. 앱을 다시 실행하면 교정된 기록을 강제매도로 표시한다. 작업 중 실주문이나 실거래 앱 실행은 하지 않았다.

백업은 `data-before/`의 거래 내역·주문 일지·잔여 장부와 `previous-installed-app/Binance Auto Trader.app`이다. 롤백할 경우 앱이 종료된 상태에서 이전 앱과 세 데이터 파일을 함께 복원해야 한다.

## 검증

| 검증 | 결과 |
| --- | --- |
| 백엔드 전체 | 1,424개 실행, 1,414개 통과, 기존 조건부 제외 10개 |
| UI 전체 | 694개 통과 |
| TypeScript 및 프로덕션 UI 빌드 | 통과 |
| 앱 내부 코드로 관련 회귀 실행 | 177개 통과 |
| 설치본 Python 모듈 대조 | 127개 모두 수정 소스와 일치 |
| 설치본 서명·파일 대조 | 통과 |
| 기록 교정 후 회계·미결 주문 검증 | 회계 동일, 미결 주문 0건 |
| 기존 사용자 수정 파일 | `tradingIndicatorPresenter.ts` 보존 |

수정 전 재현 시험에서 종료 매도 사유 오기록과 잔량 강제매도 사유 오기록을 확인했다. 수정 후에는 해당 회귀, 저장 재시도, case_C 청산, 일반 손절 표시 보존, 권한 없는 강제매도 차단도 통과했다.

최종 증거: [final-verification.json](final-verification.json), [repair-applied.json](repair-applied.json), [bundle-verification.json](bundle-verification.json), [installation-verification.json](installation-verification.json).
