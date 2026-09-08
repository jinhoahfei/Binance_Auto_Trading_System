# Lower BB 실거래 전 명세 대조 감사

검토일: 2026-09-09 (KST)

> 후속 코드 수정 및 재검증: [Lower BB · TradingSTM 감사 후 코드 보완](Lower_bb_remediation_2026-09-09.md). 아래 판정과 실패 로그는 수정 전 감사 기록이다.

**판정: 실주문 시작 보류.** 핵심 진입·청산 조건은 구현되어 있으나, 명세와 다른 상태·시간 경계 동작 4건을 확인했다. 이 중 3건은 손절 누락·지연 또는 보유시간 제한 미적용으로 이어질 수 있다. 기존 테스트 통과만으로 명세 준수를 인정할 수 없다.

대상 HEAD: `12c2de12f848b7347a83d259ca2b3db73c6585d4`. 검토 시작 시 작업 트리는 clean이었다. 기준 문서는 `Design/Specification/Lower_bb_logic_specification.md`이며, 이번에는 production 코드·주문 프로필을 수정하지 않았다. Binance 실주문, Testnet 주문, 키 조회, 계좌 변경을 실행하지 않았다.

## 1. 확인된 문제

### F1 / P1 — 새 하단 터치로 분류된 확정봉이 Case B 종가 손절을 누락시킨다

- 명세: §3.8. 확정 30분봉 slope `< -0.08`이면 손절한다.
- 위치: `backend/src/binance_auto_trader/application/trading_controller.py:3718`, `domain/trading/transitions/global_transitions.py:288`, `domain/trading/stm.py:439`.
- Controller는 기존 touch 봉과 다른 확정봉의 low가 lower에 닿으면 포지션 유무와 관계없이 `NEW_30M_LOWER_BAND_TOUCHED`를 반환한다.
- G-03은 `position_owner is None`을 요구하므로 보유 중에는 적용되지 않는다. 이후 포지션 Region은 `MARKET_DATA_UPDATED`만 조건검사 이벤트로 변환한다. 결과적으로 해당 close는 손절 조건검사 없이 소모된다.
- 재현: 매수가 100, 현재가 99.5, lower 99.4, 확정봉 low 99.3, 확정 slope -0.09. 비상손절은 아직 아니지만 종가 손절은 충족한다. 실제 classifier 결과를 STM에 전달하면 transition과 청산 이벤트가 모두 없다. 같은 snapshot을 `MARKET_DATA_UPDATED`로 전달한 대조군에서는 `CASE_B_STOP`이 나온다.
- 다음 진행봉 tick에서는 `confirmed_30m_close=False`가 되므로 이미 누락된 종가 손절을 그대로 복구하지 못한다. production runtime cycle은 주문 재시도 trigger만 release하며 이 close를 일반 시장 이벤트로 다시 보내지 않는다.
- 수정 방향: 새 이벤트를 시작할 수 없는 보유·pending·쿨다운 상태에서도 현재 close의 전략 평가를 보장한다. 새 scope 판정과 포지션/신호 평가가 한 이벤트의 분류 때문에 서로 사라지지 않게 해야 한다.

### F2 / P1 — Case B TREND_HOLD에서 공통 손절·6시간 시간청산이 빠진다

- 명세: §3.8, §3.9, §3.10. 30분 종가 손절, 매수가 대비 -1% 비상손절, 매수 후 6시간 시간청산.
- 위치: `backend/src/binance_auto_trader/domain/trading/transitions/case_b_position_transitions.py:298`.
- `_handle_holding`에는 세 조건이 있지만 `_handle_trend_hold`에는 slope 약화 5초 또는 %B 약화 5초만 있다. 상단 종료는 별도 전역 경로로 유지된다.
- 재현 A: Trend Hold, %B 0.70, slope 0.09, 보유 6시간 → `PB-15`로 계속 보유하며 `CASE_B_TIME_EXIT`가 없다.
- 재현 B: Trend Hold, 매수가 100, 현재가 98.9, 약화 5초가 아직 미완성 → `PB-15`로 계속 보유하며 즉시 비상손절이 없다.
- 결과: 강세가 유지되면 6시간을 초과할 수 있고, 급락 시 즉시 방어 대신 5초 유지조건을 기다린다. 종가 손절도 이 상태에서 별도 검사되지 않는다.
- 수정 방향: Case B 공통 방어조건을 두 보유 상태에 모두 적용하고, 청산 사유별 주문 실패·재시도 경로도 함께 지원한다.

### F3 / P1 — 30분봉 교체가 연속 손절 타이머를 다시 시작한다

- 명세: §4.12. realtime 30m EMA slope `<= -0.55`가 3분 연속이면 손절. §3.5~3.7의 5초 연속 조건에도 봉 경계 초기화 규칙은 없다.
- 위치: `backend/src/binance_auto_trader/application/market_evaluation_builder.py:845`.
- `_evaluate_conditions`는 `current_30m_candle_id`가 바뀌면 이전 시작시각 대신 빈 dict를 사용한다. 데이터 단절이 없어도 정상적인 30분 경계에서 모든 연속 조건이 초기화된다.
- 재현: 12:28부터 slope -0.60이 유지되고 12:30 새 봉에서도 동일, 12:31에 총 180초 경과. 실제 builder `__call__`에서 손절 flag가 false다. 초기화가 없다면 true여야 한다. 현재 구현에서는 12:33까지 기다리게 된다.
- 재현은 기존 fixture의 `ControlledConditionBuilder`로 %B/slope 두 입력만 통제하고, 실제 MarketSnapshot provenance·builder 호출·타이머 상태 commit을 사용한다. 타이머 로직은 대체하지 않는다.
- 수정 방향: 정상 봉 교체에서는 조건이 계속 참이면 시작시각을 보존한다. 조건 불충족이나 stream disconnect/gap/rebase 같은 실제 연속성 단절과 구분한다. 표시용 timer도 같은 기준을 사용해야 한다.

### F4 / P2 — Case C 회복 후 실시간 재터치에서 새 이벤트가 열리지 않는다

- 명세: §5.4, §7.4. Case C 회복 확인 후 다시 lower BB에 닿으면 새 하단 이벤트로 B/C를 재판정한다.
- 위치: `backend/src/binance_auto_trader/application/trading_controller.py:3718`, `domain/trading/transitions/global_transitions.py:288`.
- 기존 scope가 있으면 Controller가 새 하단 이벤트를 만드는 시점은 다른 30분봉의 **종가 확정**뿐이다. 다른 봉에서 실시간 재터치해도 일반 market update이고, 이미 Final인 Case C는 그대로 꺼져 있다.
- 재현: owner 없음, C consumed와 recovery confirmed가 true, B signal 대기, C Final. 다음 봉 진행 중 현재가가 lower 아래로 내려가도 G-03이 발생하지 않는다.
- 결과: 다음 봉 마감까지 과이탈·회복이 끝나면 명세상 Case C 기회를 놓칠 수 있다. 이는 §4.4의 한 봉 1회 제한과 별개이며 재현은 서로 다른 봉을 사용한다.
- 수정 방향: 회복 이후 실제 재터치를 감지하되, 한 봉 1회 setup 제한과 회복 전 잠금은 유지한다. 확정봉에서만 재활성화하는 정책을 의도했다면 명세 및 백테스트 조건부터 명시적으로 변경해야 한다.

## 2. 구현이 확인된 항목과 한계

아래는 코드 대조 및 기존 오프라인 테스트에서 확인한 범위다. 전체 전략 동치나 수익률 재현을 의미하지 않는다.

| 항목 | 확인 결과 |
|---|---|
| BB 30m close 20기간 / 2σ / %B | Decimal population 표준편차, `(price-lower)/(upper-lower)` 계산 구현 |
| CCI 20 | typical price와 평균 절대편차를 쓰는 표준 산식 구현 |
| Case B touch BBW < 0.02 | touch 시점 값을 runtime에 저장하며 entry 봉 BBW로 대체하지 않음 |
| B 최초 확정 signal | slope > -0.03, close %B > 0.25, 해당 signal 봉 low와 직전 3봉 low 비교 구현 |
| B 눌림 진입 / 유효시간 | realtime %B <= 0.30, signal age <= 3h, 초과 시 폐기 구현 |
| B 일반 보유 청산 | 익절·Trend 진입·종가 손절·비상손절·6h 조건은 구현. F1~F3 때문에 모든 상황에서 명세대로 작동하지는 않음 |
| C setup / flush / rebound | -0.15, -140, -0.25, 저가 갱신 우선, +0.06, 180초 이내, entry 기준 < -0.15 구현 |
| C 3분 초과 | Case를 버리지 않고 timer 기준을 다시 잡음. 아래 기준가 해석 차이는 남음 |
| C 익절·trailing | %B 0.10 진입, tp 기준가의 30m EMA slope 저장, 1m close를 30m EMA 후보로 계산해 비교 |
| C 청산 우선순위 | 익절권→손절→시간, trailing에서는 fallback→1m slope→시간 순서 구현 |
| C 고정 -1.1% 손절 | 현재 청산 조건에 포함하지 않음 |
| C 포지션 보유 중 %B >= 0.25 | setup/reset 신호로 보유 포지션을 초기화하지 않음 |
| 단일 owner / 진입 충돌 | pending 예약, 실제 체결 뒤 owner 반영, 동일 microstep 동시 진입은 C 우선 |
| C 종료 후 인계 | consumed, 회복 확인, TP_TRAIL + exit %B < 0.40 active 인계 및 나머지 wait-only 구현. 재터치는 F4 |
| B STOP/EMERGENCY 종료 후 재판정 | 매도 완료 후 현재 lower 조건이면 새 이벤트를 여는 경로 구현 |
| 상단 BB 안전 종료 | pending 우선 취소·same-ID 조정, 포지션만 있으면 전량 청산, 둘 다 없으면 runtime 종료 구현 |
| 주문 결과 불명 / 부분 체결 / 중복 결과 | same-ID 조정·durable 이력·잔여 처리 관련 기존 회귀 테스트 통과 |

## 3. 백테스트와 실주문을 같다고 판단할 수 없는 부분

1. **Case C fallback 가격:** §4.11은 `sell_at_tp_price()`지만 실제 `SpotRESTClient.submit_order()`는 모든 주문을 `MARKET`으로 제출한다(`spot_rest_client.py:1187`). `tp_price`는 계산·기록되지만 주문의 최소 체결가가 아니다. %B 0.10 아래에서 매도 요청하면 저장된 tp 가격 체결을 보장하지 않는다. 이를 단순히 “%B 0.10에서 반드시 수익 확보”로 읽으면 안 된다. 실거래 명세에 시장가 청산과 슬리피지를 반영해야 한다.
2. **실시간 관측 해상도:** 전략은 진행 30분 Kline update로 현재가와 연속 조건을 전진시킨다. Binance 공식 문서상 1s 이외 Kline update 주기는 2,000ms다. tick 사이 왕복이나 주문 지연은 1분 OHLC 백테스트 체결 가정과 같지 않다. [Binance 공식 Kline stream 문서](https://github.com/binance/binance-spot-api-docs/blob/master/web-socket-streams.md#klinecandlestick-streams-for-utc)
3. **EMA slope 정의:** 구현은 EMA9, 최근 6개 EMA의 OLS 기울기, 후보 가격으로 나눈 백분율, 소수점 8자리다. 이 명세는 30분 EMA라는 점만 명시하고 기간·회귀·정규화 방식까지 확정하지 않는다. 원본 백테스트의 정확한 산식과 데이터를 대조하지 않았으므로 문서의 승률/거래 수를 재현했다고 볼 수 없다.
4. **Case C timer 재기준:** §4.7은 `current_open_pct_b`, 구현 C-11은 해당 평가 시점 `market.realtime_pct_b`를 사용한다(`case_c_signal_transitions.py:167`). 이 명세에서 open이 1분봉 시가인지, timer 재시작 시점의 현재가인지 불명확하다. 전자라면 불일치이므로 원본 백테스트와 맞춰 정의해야 한다.
5. **Case B touch BBW:** 진행 중 터치 시점의 BBW를 즉시 고정한다. 명세를 ‘터치봉 최종 종가 BBW’로 해석한 백테스트라면 봉 마감 전후 0.02 경계에서 후보 수가 달라질 수 있다. 현재 문서만으로 어느 시점의 BBW인지 완전히 확정할 수 없다.

## 4. 검증 증거

기존 backend suite:

```sh
cd backend
BINANCE_RUN_TESTNET=0 BINANCE_RUN_TESTNET_ORDERS=0 \
BINANCE_RUN_PHASE13_PUBLIC_CASE2=0 BINANCE_RUN_PHASE13_RECOVERY_ONLY=0 \
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

- 1,075개 실행, 1,065개 통과, 10개 skip, 실패/오류 0. 35.417초.
- 첫 실행은 샌드박스의 loopback socket bind 차단으로 실패했다. 동일 주문 차단 환경에서 loopback 허용 후 재실행하여 통과했다.
- stdout의 `phase13-readiness: GAP` 등은 suite 내부의 검증용 fixture 출력이다. 실제 계좌 readiness를 새로 검사한 결과가 아니다.

명세 독립 재현:

```sh
backend/.venv/bin/python Design/Validation/lower_bb_spec_audit.py
```

- 6개 검사 중 대조군 1개 통과, 명세 기대값 검사 5개 실패: F1 1개, F2 2개, F3 1개, F4 1개.
- 실패를 숨기기 위한 expectedFailure 처리를 하지 않았다. 현 구현에서 exit code 1이 나온다.
- 타이머는 실제 builder 경로를 사용하고, 전략 재현은 production classifier/STM에 통제된 상태 snapshot을 넣는다. 실제 거래소 체결까지의 E2E 증거는 아니다.
- 상세 출력: `lower_bb_spec_audit_result.txt`, `lower_bb_backend_suite_result.txt`.

이번 검토에서는 UI 전체 suite, 설치된 app/DMG 재빌드, 최신 signed 계좌 readiness, 실제 거래소 주문을 실행하지 않았다. 기존 `MACOS_SESSION8_PREPARATION.md`의 기술적 GO는 계좌·runtime 사전조건에 관한 기록이며, 이번에 확인된 전략 불일치를 해소한 증거는 아니다.

## 5. 재개 조건

F1~F3을 수정하고, F4 및 §3의 명세 해석 차이를 실제 운용 기준으로 확정한 뒤 재현 검사를 통과시켜야 한다. 수정 후 기존 backend suite와 public Kline→전략→fake 주문 경로에서 해당 경계 시나리오를 검증하고, 실제 사용할 패키지가 수정 소스를 포함하는지도 확인해야 한다. 그 전에는 현재 명세대로 투자한다고 판정할 수 없다.
