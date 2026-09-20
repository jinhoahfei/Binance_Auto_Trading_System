# 장시간 실행 로그 분석 — 2026-09-19

분석 대상은 가장 최근 실제 거래 모드 실행(run `01246b9353074cc6aec85fd5e5cfa87b`, 프로세스 49325)이다. **2026-09-18 13:18:32부터 2026-09-19 21:53:28까지 약 32시간 35분**, 한국 시간 기준으로 분석했다. 분석 중에도 로그는 계속 추가되므로 수치는 sequence 780092까지 고정했다. 과거 테스트 실행과 다른 실행의 로그는 집계에서 제외했다.

## 결론

**시세 수신과 전략 판단은 계속 동작했고, 기록된 평가에서 첫 매수 진입 조건이 한 번도 충족되지 않아 매수·매도가 없었다.** 거래를 시작한 뒤 매매 상태와 적용 투자 유형의 변경은 없었다. 화면 연결의 짧은 재연결과 한 차례 화면 생존 신호 이상 감지는 있었지만, 같은 거래 프로세스가 계속 실행됐다. 이번 실행에서 주문을 실제 제출하지 않았으므로 주문 제출·체결 기능까지 정상 작동했다고 검증한 것은 아니다.

## 실행과 상태 변화

| 항목 | 확인 결과 |
|---|---|
| 시작 준비 | 9/18 13:18:34 READY, 초기화 단계 3개 모두 성공 |
| 투자 유형 선택 | 9/18 13:18:38 Type 0 · 횡보 선택 |
| 거래 감시 시작 | 9/18 13:18:43 실행 상태 running으로 전환 |
| 유일한 매매 상태 전이 | NOT_STARTED → LOWER_TOUCH_WATCH, G-01 |
| 이후 상태 | 전체 평가에서 LOWER_TOUCH_WATCH, 볼린저밴드 하단 접촉 대기 |
| 적용 투자 유형 | 전체 평가에서 TYPE_0 유지 |
| 시장 기반 전략 평가 | 56,866회; 시작 시 평가 1회 포함 총 56,867회 |
| 시세 입력 처리 | 220,130건 |
| 확정 봉 관측 | 1분 1,955개, 30분 65개, 4시간 8개, 일봉 1개 |
| 하단 접촉 조건 충족 | 0회 |
| 신규 주문 요청·신규 체결 | 0건 |
| 전략 보유 포지션·대기 주문 | 전체 평가에서 0 / 없음 |
| 강제 중지·위험 차단 | manual_kill=false, last_risk_decision=null, command_enabled=true |

근거: [실행 설정](</Users/oscar/Desktop/Binance_Auto/Log_History/2026-09-18_13-18-32-364965_KST_live_49325_01246b9353074cc6aec85fd5e5cfa87b_part0001.log:2>), [유형 선택](</Users/oscar/Desktop/Binance_Auto/Log_History/2026-09-18_13-18-32-364965_KST_live_49325_01246b9353074cc6aec85fd5e5cfa87b_part0001.log:34>), [최초 상태 전이](</Users/oscar/Desktop/Binance_Auto/Log_History/2026-09-18_13-18-32-364965_KST_live_49325_01246b9353074cc6aec85fd5e5cfa87b_part0001.log:52>), [마지막 전략 평가](</Users/oscar/Desktop/Binance_Auto/Log_History/2026-09-19_21-53-10-064855_KST_live_49325_01246b9353074cc6aec85fd5e5cfa87b_part0074.log:123>).

## 매수가 없었던 이유

현재 전략의 첫 조건은 **현재가 ≤ 30분 볼린저밴드 하단**이다. [조건 정의](</Users/oscar/Desktop/Binance_Auto/backend/src/binance_auto_trader/domain/trading/conditions.py:31>)와 [하단 접촉 후 전이 처리](</Users/oscar/Desktop/Binance_Auto/backend/src/binance_auto_trader/domain/trading/transitions/global_transitions.py:135>)를 실제 평가 로그와 대조했다. 56,866개 시장 평가의 가격과 하단 값을 별도로 비교해도 조건 충족은 0회였다. Case B/C 진입 준비 상태에 도달하지 않았고, 구매 예산 검사나 주문 제출 단계도 진행되지 않았다.

| 시점 | ETH 가격 | 당시 하단 | 하단보다 높은 금액 |
|---|---:|---:|---:|
| 시작 후 첫 평가, 9/18 13:18:44 | 2,476.68 | 2,433.04 | 43.64 USDT |
| 가장 가까운 시점, 9/19 13:39:30 | 2,612.53 | 2,606.75 | **5.78 USDT, 약 0.222%** |
| 분석 마지막 평가, 9/19 21:53:28 | 2,642.61 | 2,615.37 | 27.24 USDT |

가장 가까운 시점의 %B도 약 0.178이었으며 하단 접촉 기준인 0에 도달하지 않았다. 관측된 전략 평가 가격 범위는 2,470.00–2,659.99 USDT였고, 첫 평가에서 마지막 평가까지 약 6.70% 올랐다. 상승했다는 사실만으로 이 전략의 신규 매수가 시작되지는 않는다. 매수가 없어 전략 포지션이 없었으므로 매도도 없었다.

근거: [최소 하단 거리 기록](</Users/oscar/Desktop/Binance_Auto/Log_History/2026-09-19_13-25-30-045991_KST_live_49325_01246b9353074cc6aec85fd5e5cfa87b_part0056.log:5172>), [최초 시장 평가](</Users/oscar/Desktop/Binance_Auto/Log_History/2026-09-18_13-18-32-364965_KST_live_49325_01246b9353074cc6aec85fd5e5cfa87b_part0001.log:62>), [마지막 시장 평가](</Users/oscar/Desktop/Binance_Auto/Log_History/2026-09-19_21-53-10-064855_KST_live_49325_01246b9353074cc6aec85fd5e5cfa87b_part0074.log:123>).

## 연결·오류·데이터 연속성

백엔드 로그 약 78만 줄, 74개 파일, 약 1.21GB를 검사했다. sequence 누락, JSON 해석 실패, 명시적 로그 유실은 모두 0이었다. 실행 재시작이나 거래 세션 중지 기록은 없었다. 정기 상태 기록은 최초 시작 전 1회를 제외한 1,950회 모두 running이었다.

ERROR 8줄은 **화면과 백엔드 사이 연결 종료 4건을 각각 2줄씩 기록한 것**이었다. 각 연결은 아래 시간 안에 새 화면 연결로 인증을 완료했다. 화면 식별자는 달라졌지만 백엔드 프로세스와 거래 세션은 유지됐다. 화면이 다시 시작된 정확한 원인은 이 로그만으로 확정하지 않았다.

| 연결 종료 시각, KST | 재인증까지 | 근거 |
|---|---:|---|
| 2026-09-18 19:48:04 | 0.308초 | [원본 로그](</Users/oscar/Desktop/Binance_Auto/Log_History/2026-09-18_19-26-44-056375_KST_live_49325_01246b9353074cc6aec85fd5e5cfa87b_part0015.log:8618>) |
| 2026-09-19 02:16:35 | 0.279초 | [원본 로그](</Users/oscar/Desktop/Binance_Auto/Log_History/2026-09-19_02-06-56-056888_KST_live_49325_01246b9353074cc6aec85fd5e5cfa87b_part0031.log:4061>) |
| 2026-09-19 08:53:35 | 0.163초 | [원본 로그](</Users/oscar/Desktop/Binance_Auto/Log_History/2026-09-19_08-42-24-067399_KST_live_49325_01246b9353074cc6aec85fd5e5cfa87b_part0046.log:4370>) |
| 2026-09-19 15:59:35 | 0.222초 | [원본 로그](</Users/oscar/Desktop/Binance_Auto/Log_History/2026-09-19_15-46-58-052136_KST_live_49325_01246b9353074cc6aec85fd5e5cfa87b_part0061.log:4446>) |

추가로 9/18 21:36:30 화면 생존 신호가 한 번 비어 이상 감지가 시작됐고, 21:36:35 약 5.052초 후 회복됐다. 같은 시점의 시세 입력과 전략 평가는 계속 최신으로 갱신됐다. OS 진단에서는 정지 원인을 직접 입증하는 증거가 발견되지 않았다. [이상 감지](</Users/oscar/Desktop/Binance_Auto/Log_History/runtime_health/runtime_1789705114623_49192_part0001.log:998>), [회복](</Users/oscar/Desktop/Binance_Auto/Log_History/runtime_health/runtime_1789705114623_49192_part0001.log:1061>).

WARNING 1건은 시작 순간 market_stream_initializing이며 약 0.47초 뒤 초기 동기화가 완료됐다. native 상태 기록에서 초기 샘플 다음부터 시장·계좌 스트림은 online이었다. 마지막 오류 코드 필드에 MARKET_STREAM_UNAVAILABLE가 남아 있어도 현재 스트림 상태와 구별해야 한다.

봉 전환 시 자료를 기다린 기록 87건은 모두 다음 시세 입력으로 이어졌고 가장 긴 간격은 약 2.02초였다. market_evaluation_deferred 163,260건은 진행 중 1분봉 또는 4시간·일봉 등 30분 전략 평가 대상이 아닌 입력에서 발생했으며, 30분봉에 대한 보류는 0건이었다. [평가 대상 구분 코드](</Users/oscar/Desktop/Binance_Auto/backend/src/binance_auto_trader/application/market_evaluation_builder.py:464>).

시세 입력 사이 최대 간격은 약 13.97초, 전략 평가 사이 최대 간격은 약 13.99초였다(9/19 13:23:30–13:23:44). **후속 공개 체결 대조에서 직전 체결과 다음 체결 사이 13.410초 동안 체결이 없었고, 복귀한 봉의 거래량 증가가 새 체결 수량과 정확히 일치함을 확인했다.** 복귀한 봉 이벤트는 생성 후 약 19ms에 입력 로그로 반영됐다. 이번 공백은 무체결 시간과 봉 갱신 주기로 설명되며 14초 처리 지연을 뜻하지 않는다. [상세 원인 분석](</Users/oscar/Desktop/Binance_Auto/artifacts/log-review-20260919/gap-analysis.md>). ‘조건 충족 0회’는 전체 실행의 저장된 평가 기준이며 모든 체결 가격을 전수 비교한 결과는 아니다.

## 투자 유형의 ‘적용’과 ‘추천’ 구분

**실제 적용된 투자 유형은 Type 0 · 횡보로 유지됐다고 확인할 수 있다.** 선택 명령은 시작 전 한 번뿐이었고 이후 모든 전략 평가와 정기 상태 기록에 TYPE_0가 남았다.

화면의 **추천 시장 유형**은 별도 값이다. 추천 변경 결과는 현재 코드에서 메모리의 evaluation_traces에 누적되지만, 검사한 파일 로그에는 과거 추천 유형이 저장되지 않았다. 따라서 추천이 중간에 상승/하락 등으로 바뀌었는지, 계속 같았는지는 이번 파일 로그만으로 확정할 수 없다. 현재 구조에서 추천 변경 자체는 적용 유형을 자동으로 변경하는 명령이 아니다. [추천·선택 분리](</Users/oscar/Desktop/Binance_Auto/backend/src/binance_auto_trader/application/regime_controller.py:595>), [추천 이력 처리](</Users/oscar/Desktop/Binance_Auto/backend/src/binance_auto_trader/application/regime_controller.py:1601>).

## 자산·거래 내역 대조

전략 포지션은 이번 실행 내내 0이었다. 시작 시 잔액 대조는 잔여 원금 0.00009600 ETH와 과거 Earn 보상 0.00000001 ETH를 합친 0.00009601 ETH가 거래소 Spot 잔액과 일치해 verified였다. 이 잔여 자산은 이번 실행의 신규 매수나 열린 전략 포지션이 아니다. 정기 상태에 반복되는 잔액 확인 시각은 실행 시작 시각이므로, 이를 매분 새 잔액 대조가 수행된 것으로 해석하지 않았다. [시작 시 잔액 확인](</Users/oscar/Desktop/Binance_Auto/Log_History/2026-09-18_13-18-32-364965_KST_live_49325_01246b9353074cc6aec85fd5e5cfa87b_part0001.log:11>).

로컬 실제 거래 장부도 대조했다. 마지막 거래 기록은 9/10의 매수와 수동 매도 두 건이고, 이번 9/18–19 실행의 추가 체결은 없다. 미처리 주문 장부의 과거 주문은 마지막 REMOVE로 종료되어 있다. 이번 실행의 신규 실현손익과 신규 거래 수수료는 로컬 기록상 발생하지 않았다. 거래소 계좌를 새로 조회하거나 주문하지 않았으며, 코드·설정·실행 상태는 변경하지 않았다.

[실제 거래 장부](</Users/oscar/Library/Application Support/com.binance-auto.trader/com.binance-auto.trader.live/trade-history.jsonl>), [집계 수치](</Users/oscar/Desktop/Binance_Auto/artifacts/log-review-20260919/metrics.json>).
