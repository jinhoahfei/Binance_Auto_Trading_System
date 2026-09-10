# Case C 시세 복구 후 회복 기준 불일치 수정

2026-09-10. `Lower_bb_logic_specification.md` §4.5~4.7과 Event-Action Table C-08~C-14를 기준으로 수정했다.

## 원인

21:32 시작한 live 실행에서 C-10은 21:34:29.994 KST의 저점 2,439.98과 %B `-0.3236635529213372712908670964037950`을 저장했다. 매수선은 기준 %B에 `0.06`을 더한 `-0.2636635529213372712908670964`였다.

시세 장애를 복구한 뒤 `_prepare_market_event_context()`는 첫 시장 평가에서 `timer_base_time`을 21:35:01.996 KST로, `timer_base_pct_b`를 `-0.2920547549500692436220628915022611`로 덮어썼다. `entry_pct_b`는 갱신하지 않아 서로 다른 기준의 값이 섞였다. 장애 시간도 경과시간에서 사라졌고, 21:35:18 매수 판정에 약 48초 대신 약 16초가 기록됐다.

실제 매수 시점의 %B `-0.2618221034394996491882431970337721`은 복구 전 저점 기준으로도 3분 이내 `+0.06` 회복 조건을 만족한다. 다만 같은 복구 경로는 3분이 지난 경우에도 이전 매수선으로 진입할 수 있었다. 이 동작은 별도의 만료 재현 검사에서 확인했다.

## 수정

`TradingController`의 시세 복구 후 Case C 타이머를 덮어쓰는 flag와 patch를 제거했다. 연결 복구는 기존 `timer_base_time`, `timer_base_pct_b`, `entry_pct_b`, flush 기록을 보존한다. 복구 첫 유효 시장 평가에서 장애 시간을 포함한 경과시간과 원래의 판정 우선순위를 적용한다.

| 복구 후 상황 | 처리 |
| --- | --- |
| 매수 전 실시간 %B가 0.25 이상 | C-08로 setup 종료 |
| 최초 flush 없음, 실시간 %B가 -0.25 이하 | C-09로 최초 저점과 세 기준값 함께 저장 |
| 저장된 flush보다 현재 가격이 낮음 | C-10으로 저점과 세 기준값 함께 갱신 |
| 새 저점 없이 실제 경과시간이 180초 초과 | C-11로 현재 %B·현재 시각·새 매수선을 함께 갱신 |
| 실제 경과시간이 180초 이하 | 기존 매수선과 나머지 C-12 조건으로 판정 |

세 값의 관계는 `entry_pct_b = timer_base_pct_b + 0.06`이다. Case C의 회복 제한시간은 5초·3분 연속 유지 지표와 다르므로, 연결 복구를 이유로 새 180초를 부여하지 않는다. 연속 지표의 데이터 단절 처리, 계좌·주문 재조정, 중지 및 worker 실패 차단은 기존 경로를 사용한다.

UI에 전달되는 첫 복구 후 지표도 원래 타이머 ID·남은 시간·매수선을 사용한다. 이번 로그 값을 재생했을 때 복구 후 남은 시간은 148초이며, 16초 뒤 매수 판정의 총 경과시간은 48초이다.

## 재현과 검증

외부 연결을 차단하고 임시 이력·가짜 거래소와 실제 Controller, 계좌·주문 복구, 직렬 event queue, TradingSTM, transport DTO를 사용했다. 실계좌 주문이나 실행 중인 앱의 매매 명령은 호출하지 않았다.

| 검사 | 결과 |
| --- | --- |
| 현재 개발용 실행 파일의 내장 코드로 새 회귀 검사 | 9개 중 6개 실패하여 기존 결함 재현, 오류 0개 |
| 수정 소스의 전용 회귀 검사 | 9개 통과 |
| backend 전체 | 1,140개 실행, 1,130개 통과, opt-in Testnet 10개 건너뜀 |
| UI 전체 | 49개 파일, 501개 통과 |
| 수정 앱의 내장 코드 | 동일 회귀 검사 9개 통과, 외부 연결 차단 |
| 패키징 | TypeScript 검사·UI 및 macOS 앱 빌드·로컬 서명 검증 통과 |
| 보관한 수정 앱 | 빌드와 전체 파일 해시 일치 |

전용 검사는 실제 로그의 시장 값과 단일 매수, 반복 복구의 시간 누적, 180초 직전·정확히 180초·1마이크로초 초과 경계, 긴 장애 뒤 새 저점 우선순위, 최초 flush 이전 대기, 충분한 회복 후 setup 종료, 계산 불가 %B의 기준 오염 방지를 포함한다. 복구 직후 실제 transport DTO의 매수선과 타이머 잔여시간도 검사했다.

첫 전체 검사에서는 sandbox가 테스트용 loopback socket을 차단해 실패했다. 로컬 통신이 허용된 실행에서 동일 전체 검사를 다시 수행해 모두 통과했다.

## 적용 범위

이번 수정은 시세 복구 후 Case C 회복 기준 불일치에 한정한다. 같은 live 실행에서 매수 체결 직후 발생한 `Position entered_at` 음수 경과시간 예외는 별도 결함이며 이 수정으로 해결됐다고 보지 않는다.

현재 live 세션은 21:32에 시작한 기존 backend 프로세스다. 그 프로세스의 메모리에 수정 코드를 주입하지 않았고, 매매·중지·강제 종료·재시작 명령을 호출하지 않았다. 실제 적용에는 수정된 실행 파일로 새 프로세스를 시작해야 한다.

패키징 전후 해시를 비교한 결과, release 파일뿐 아니라 개발용 `target/debug/binance-auto-sidecar` 파일도 새 내용으로 변경됐다. 수정 앱 내장 코드와 개발용 파일의 SHA-256은 `996edeff9c85d4e1d5459c3e1c9004117c7b71a13eaff40de9fcf68e28cfcd11`로 동일하다. 파일이 그대로 보존됐다는 최초 검사는 실패했으므로, 실행 중 앱에 영향이 전혀 없었다고 주장하지 않는다.

패키징 후 프로세스 조회에서는 기존 화면 프로세스가 사라지고 backend PID 43044/43052가 남아 있었다. 이 조회와 live 로그만으로 화면 종료 원인은 확정할 수 없어 사용자에게 직접 닫았는지 확인을 요청했다. 확인한 live 로그에는 21:35:18 BUY 한 건만 있고 이후 추가 주문은 없으며, 기존 `EVENT_RUNTIME_FAILED`의 재조정 필요 상태가 유지된다.

검증된 수정 앱은 `Log_History/case_c_recovery_fix_2026-09-10/Binance Auto Trader.app`에 보관했다. `/Applications` 설치본은 이번 작업에서 교체하지 않았고 수정 앱을 실행하지 않았다.

## 증거

- [설계 기준](/Users/oscar/Desktop/Binance_Auto/Design/Specification/Lower_bb_logic_specification.md:310)
- [Event-Action Table](/Users/oscar/Desktop/Binance_Auto/Design/Trading_Logic/Event_Action_Table/Trading_Logic_Event_Action_Table.md:347)
- [실제 기준 불일치 로그](/Users/oscar/Desktop/Binance_Auto/Log_History/2026-09-10_21-32-03-918765_KST_live_43052_c49ae28fea6a490ab25c457b48c7e6f9_part0001.log:1341)
- [기존 실행 파일 재현 결과](/Users/oscar/Desktop/Binance_Auto/Log_History/case_c_recovery_fix_2026-09-10/running-binary-baseline.json)
- [전체 backend 검사](/Users/oscar/Desktop/Binance_Auto/Log_History/case_c_recovery_fix_2026-09-10/backend-verified.log)
- [전체 UI 검사](/Users/oscar/Desktop/Binance_Auto/Log_History/case_c_recovery_fix_2026-09-10/ui-verified.log)
- [수정 앱 내장 코드 검사](/Users/oscar/Desktop/Binance_Auto/Log_History/case_c_recovery_fix_2026-09-10/bundled-verification.json)
- [패키징 결과](/Users/oscar/Desktop/Binance_Auto/Log_History/case_c_recovery_fix_2026-09-10/build.log)
- [수정 앱 보관·해시 검증](/Users/oscar/Desktop/Binance_Auto/Log_History/case_c_recovery_fix_2026-09-10/candidate-verification.json)
- [전용 회귀 검사](/Users/oscar/Desktop/Binance_Auto/backend/tests/integration/test_case_c_market_recovery.py)
