# 외부 수동 매도 이후 시작 실패 수정

2026-09-10. 기존 Case B/C 단계·매수 후 보유시간·지표 수정과 REGIME 선택창 복구를 유지하고,
Binance 수동 매도 이후의 새 실행 복구 경로를 보완했다.

## 원인

21:35:18.511 KST의 앱 매수는 gross `0.004 ETH`, 수수료 `0.000004 ETH`였고,
실제 Position은 `0.003996 ETH`였다. 22:32:45.885 KST에 사용자가 Binance에서
`0.0039 ETH`를 매도해 현재 잔고가 `0.000096 ETH`로 줄었다.

기존 실행은 외부 체결을 감지해 reconciliation으로 잠겼다. 22:33:59의 새 실행은 앱 주문만
복구했기 때문에 기존 BUY Position을 그대로 복원했고, `ORDER_RECONCILIATION_FAILED`와
`restored Position exceeds the Binance ETH balance`로 실패했다. 확인한 owner 기록은
`RELEASED`였다. 이번 장애의 원인은 수동 매도를 새 실행의 이력과 Position에 반영하지 못한 것이다.

## 수정

새 실행은 기존 주문 조정 뒤 모든 client의 주문·fill을 읽고, 실제 외부 SELL이 열린 앱 Position의
감소와 정확히 일치하는지 확인한다. 주문·fill과 ETH 잔고를 두 번 조회하고 전체 계좌 미체결 주문도
대조한 뒤에만 기록한다. 원 주문량·체결량·수수료·시각을 보존하고 기존 원가와 성과 계산에 반영한다.

- 전량 매도는 Position을 닫고, 부분 매도는 실제 잔량과 원가를 유지한다.
- 수수료 때문에 남은 매도 단위 미만 ETH는 기존 잔여 장부에 수량·원가를 보존한다.
- 외부 매도는 schema v4, `EXTERNAL_MANUAL`이며 UI의 두 거래 화면에 `외부 수동 매도`로 표시한다.
- 앱이 결정한 시세가 없으므로 판단 시세는 null, CSV에서는 빈 field로 기록한다.
- 같은 주문 ID는 한 번만 저장한다. 저장 직후 중단된 경우도 다음 실행에서 중복 없이 복구한다.
- 실행 중 외부 체결 차단은 유지한다. 새 실행의 복구 완료 상태는 `NOT_STARTED`다.

외부 BUY·입출금·다른 보유량·미체결 주문·불완전한 체결 자료·조회 race를 임의로 매도에 귀속하지
않는다. 상세 범위는 [ADR-007](../Architecture/Decisions/ADR-007-external-manual-sell-recovery.md)에
기록하고 Event-Action Table과 communication specification에도 startup 책임을 반영했다.

## 재현과 검증

| 검사 | 결과 |
| --- | --- |
| backend 전체 | 1,152개 실행, 1,142개 통과, opt-in 외부 주문 검사 10개 건너뜀 |
| UI 회귀 | 총 504개 통과: 기본 검사 502개와 로컬 서버 권한을 부여한 process 검사 2개 |
| UI typecheck·production build | 통과, 산출물은 임시 디렉터리에 격리 |
| 실행 파일 내장 코드 | 외부 복구·조회 8개와 기존 Case C·매수 후 지표 13개, 총 21개 통과 |
| 실제 거래소 읽기 전용 복사본 | 두 번의 독립 startup·정상 종료 모두 통과 |

초기 전체 검사에서 로컬 서버 바인딩이 sandbox에 막힌 항목은 해당 권한을 부여해 재검사했다.
실제 거래소 주문 테스트는 활성화하지 않았다. native Rust 코드와 전략 임계값은 이번 수정 범위가 아니다.

추가 회귀 검사는 전량·부분 매도, 세 번의 반복 startup, 취소된 부분 체결 뒤 이어진 매도,
BNB fee 근거와 원가 보존, 실제 파일 저장 후 중단, CSV의 null 판단 시세를 확인한다.
이체·외부 매수·진행 중 주문·중복·과대 원 요청량·조회 race·잘못된 fill 출처·페이지 누락은 차단한다.

## 실제 이력 복사본 결과

원본 `trade-history.jsonl`과 pending journal의 SHA-256을 기록하고 임시 live namespace로
복사했다. 주문 권한이 없는 실제 live application graph로 두 번 시작·종료했다.

| 항목 | 검증 결과 |
| --- | --- |
| 거래 이력 | 기존 BUY 1건 + 외부 SELL 1건, 두 번째 실행에도 2건 |
| 매도 시각 | 2026-09-10 22:32:45.885 KST |
| 실제 매도량·수수료 | `0.00390000 ETH`, `0.00944525 USDT` |
| 매도분 배분 원가 | `9.541297297297297297297297297297297 USDT` |
| 실현손익·수익률 | `-0.105488547297297297297297297297297 USDT`, `-1.10559963%` |
| 전략 Position | 0 |
| 잔여 ETH·취득원가 | `0.00009600 ETH`, `0.234862702702702702702702702702703 USDT` |
| pending·재조정·거래소 미체결 | 모두 없음 |
| lifecycle | `READY` → `CLOSED`, 거래 세션 `NOT_STARTED` |

## 적용 상태

소스 수정과 패키징 검증을 완료했다. `UI`에서 `pnpm desktop:dev`를 다시 실행하면 기존
`beforeDevCommand`가 수정 backend를 재패키징하고, 실제 startup이 체결·잔고를 다시 검증해
복구를 적용한다. 원본 live 이력은 이번 검증에서 수정하지 않았으므로, 현재 저장 상태에 복구가
이미 적용됐다고 보지 않는다. 실제 주문 제출·취소·청산이나 자동매매 시작을 수행하지 않았다.

패키징은 별도 소스 복사본에서 수행했고, 기존 개발용 sidecar들의 SHA-256이 작업 전후 동일했다.
이전 배포 앱에는 이번 소스 수정이 포함되어 있지 않으므로 재빌드된 개발 실행을 사용한다.

## 증거

2026-09-10 UI 후속 수정: 사용자의 화면 배치 요청에 따라 전역 `잔여 ETH` 배너와 전용
component를 제거했다. 외부 매도는 기존 체결 내역과 전체 거래 이력에서 확인한다.
잔여 수량·원가의 backend 장부와 복구 처리는 유지한다.

- [거래소 주문·fill 근거](/Users/oscar/Desktop/Binance_Auto/Log_History/manual_exit_recovery_2026-09-10/exchange_evidence.json)
- [실제 계좌·복사본 두 번 검증](/Users/oscar/Desktop/Binance_Auto/Log_History/manual_exit_recovery_2026-09-10/live_copy_verification.json)
- [backend 전체 검사](/Users/oscar/Desktop/Binance_Auto/Log_History/manual_exit_recovery_2026-09-10/backend.log)
- [UI 전체 검사](/Users/oscar/Desktop/Binance_Auto/Log_History/manual_exit_recovery_2026-09-10/ui.log)
- [UI process 재검사](/Users/oscar/Desktop/Binance_Auto/Log_History/manual_exit_recovery_2026-09-10/ui-process.log)
- [실행 파일 내장 코드 검사](/Users/oscar/Desktop/Binance_Auto/Log_History/manual_exit_recovery_2026-09-10/bundled-verification.json)
- [원래 sidecar 보존과 격리 빌드](/Users/oscar/Desktop/Binance_Auto/Log_History/manual_exit_recovery_2026-09-10/build_manifest.json)
- [복구 통합 검사](/Users/oscar/Desktop/Binance_Auto/backend/tests/integration/test_external_exit_recovery.py)
- [REST 체결 조회 검사](/Users/oscar/Desktop/Binance_Auto/backend/tests/unit/binance/test_external_execution_reader.py)
