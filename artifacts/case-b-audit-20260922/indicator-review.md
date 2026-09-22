# Case B 지표 계산 및 재대조 방법 검토

검토일: 2026-09-22. 현재 작업 폴더 소스의 읽기 전용 검토이며, 운영 당시 실행본과 완전히 같은 버전이라는 가정은 별도 검증 대상이다.

## 문서 기준과 실제 계산

| 단계 | 명세 | 현재 코드 | 판정 유의사항 |
|---|---|---|---|
| 감시 시작 | 현재가가 실시간 30분 BB 하단 이하, 터치 순간 BBW < 0.02 | 현재 가격과 같은 시점 BB 비교; `OpenLowerEvent`에 BBW 복사 후 runtime 값으로 Case B 활성화 | 과거 확정봉 저가와 최종 BBW만으로 터치 당시 적격 여부를 대신 판정하면 안 됨 |
| 확정봉 %B | `pct_b_close > 0.25` | 신호봉을 포함한 마지막 20개 확정 종가의 BB(모분산, 2σ), `(close-lower)/(upper-lower)` | 직전 20봉 밴드 또는 다음 진행봉 밴드가 아님 |
| 확정봉 slope | `ema_slope > -0.03` | 30분 EMA9, 최근 6개 EMA9의 OLS 기울기 / 신호봉 종가 × 100, 소수 8자리 | Lower 명세 자체는 EMA 기간·OLS 기간·정규화 분모·seed를 명시하지 않음 |
| 저점 유지 | `signal_candle.low >= min(previous_3_candles.low)` | 마지막 확정봉 저가와 직전 세 확정봉(`closed_klines[-4:-1]`) 최저가 비교 | 터치봉 저가와 비교하는 조건도 아니고 신호봉을 min 안에 포함하는 조건도 아님 |
| 진입 | 신호 후 3시간 이내 realtime %B ≤ 0.30 | 즉시 눌림검사 예약, 시간 ≤ 10800초, %B ≤ 0.30 및 소유 포지션/주문/Case B 중지 조건 검사 | 세 신호 조건은 동일한 하나의 확정봉에서 모두 충족해야 함 |

명세 근거: `Design/Specification/Lower_bb_logic_specification.md:35-41,74-108,114-137`.

## 독립 재계산 recipe

1. 해당 실행 세션에서 실제 받아 저장한 ETHUSDT 30분봉을 사용한다. UTC 봉 시작시각에 30분을 더한 시각이 신호 확정 시각이며 KST는 UTC+9다.
2. 하단 터치 시점에는 직전 **확정 종가 19개 + 해당 시점 현재가**로 20개 배열을 만든다. 평균 M, 모표준편차 σ를 계산하고 L=M−2σ, U=M+2σ, BBW=(U−L)/M. `현재가 <= L` 및 `BBW < 0.02`를 체크한다. 이 BBW를 이벤트 종료까지 고정한다.
3. 각 후속 확정 30분봉에 대해 해당 봉을 포함한 마지막 20개 확정 종가로 BB를 계산해 종가 %B를 구한다.
4. EMA는 실제 snapshot의 시간순 확정 종가 전체로 계산한다. 첫 9개 종가 평균을 최초 EMA9로 두고, 이후 `EMA_t = 0.2 × close_t + 0.8 × EMA_(t-1)`를 적용한다. 기본 조회/유지량은 500봉이며, slope만 재현한다고 마지막 20봉에서 seed를 시작하지 않는다.
5. 최근 6개 EMA 값 y0…y5에 x=0…5를 붙인다. `raw_slope = Σ[(x−2.5)(y−mean(y))]/17.5`. `slope = raw_slope / signal_close × 100`. Decimal 유효숫자 34, 마지막에 0.00000001로 HALF_EVEN 반올림한다.
6. 세 조건 `slope > −0.03`, `close_%B > 0.25`, `signal_low >= min(low_(t−1),low_(t−2),low_(t−3))`를 같은 확정봉에서 검사한다.
7. 최초 통과 봉의 마감시각 이후부터 3시간까지 실제 수신된 실시간 30분봉 업데이트에 대해 2번의 동적 밴드 방식으로 `%B <= 0.30` 발생 여부를 검사한다. 신호 확정 직후에도 검사를 하므로 신호 %B가 `(0.25,0.30]`이면 즉시 시장조건이 충족될 수 있다.
8. 가격조건 충족과 실제 주문 가능성은 구분한다. 해당 시점의 root/Case B 상태, lower event 교체·종료, Case C 소유 여부, 주문 보류, Case B entry pause, 사용자의 실행/중지, stream 실패·복구 상태를 함께 대조한다.

## 정확한 소스 위치

- BB 20, 2σ 상수: `backend/src/binance_auto_trader/application/market_evaluation_builder.py:26-33`.
- 모분산 BB 계산: 같은 파일 `:44-85`; %B 계산 `:88-108`.
- 확정 EMA 계산 및 마지막 확정 종가 분모: 같은 파일 `:716-737`.
- 실시간 가격은 authoritative 최신 30분봉 close: 같은 파일 `:739-753`.
- 진행봉 BB 19개+현재가 / 확정봉 20개: 같은 파일 `:755-773`.
- 확정 %B 독립 계산: 같은 파일 `:775-788`.
- 현재 확정봉과 직전 세 확정봉 저가 분리: 같은 파일 `:841-870`.
- EMA9 seed/alpha: `backend/src/binance_auto_trader/domain/market/ema_slope.py:7-13,16-53`.
- 6값 OLS 회귀: 같은 파일 `:56-101`.
- 정규화 및 8자리 반올림: 같은 파일 `:104-151`.
- 조건 경계·피연산자: `backend/src/binance_auto_trader/domain/trading/conditions.py:30-38,75-77`.
- 터치 BBW 고정: `backend/src/binance_auto_trader/domain/trading/transitions/helpers.py:70-90`; 활성화 조건 `case_b_signal_transitions.py:51-77`.
- 확정봉만 신호판단, 최초 신호 및 바로 눌림 검사: `case_b_signal_transitions.py:79-109,234-249`.
- 매수 가능성: `case_b_signal_transitions.py:128-188`.
- 기본 500봉 조회·유지: `backend/src/binance_auto_trader/application/market_data_controller.py:24,266,1542`.

## 최종 봉 차트만 보는 경우 놓치는 점

- touch BBW는 터치 시점 값이다. 마지막에 확정된 BBW가 0.02 이상이어도 최초 터치 당시 0.02 미만일 수 있고, 반대도 가능하다.
- Case B 신호는 **종가 확정** 기준이다. 진행 중 `%B > .25` 또는 slope가 회복돼도 마감까지 유지되지 않으면 신호가 아니다.
- 실시간 화면에 확정 slope/%B 수치가 있어도 해당 update가 30분 close trigger라는 뜻은 아니다. builder는 최신 확정봉 지표를 계속 제공하면서 `confirmed_30m_close`는 원본 관측이 실제 30분 확정봉인 경우에만 true로 둔다 (`market_evaluation_builder.py:742-745`).
- REST bootstrap/full resync는 과거 확정봉을 포함하더라도 어떤 close trigger도 만들지 않는다 (`:708-715`). 운영이 중단된 사이의 신호를 과거 차트에서 발견해도 당시 앱이 실행 중 평가했다고 단정할 수 없다.
- 다음 30분봉에서 새 하단 접촉을 받으면 조건에 따라 기존 lower event가 종료되고 새 Case B 후보가 시작된다 (`global_transitions.py:156-182`). 이 경우 이전 후보와 이후 후보의 BBW/신호 대기를 섞지 않아야 한다.
- 회복 중 상단 접촉이 먼저 발생하면 안전 종료가 공통 전략보다 우선한다 (`global_transitions.py:113-123`). 이후의 눌림을 이미 종료된 후보의 진입 기회로 세면 오판이다.
- `%B +0.05` 같은 값은 가격 변화율 5%가 아니다. %B는 해당 순간 밴드 폭에서의 위치이며 진행 중 밴드 자체도 바뀐다.
- Lower 명세의 백테스트 대체체결 규칙은 다음 6봉의 low/high가 %B .30 가격을 포함하는지를 사용한다(`:135-137`). 실시간 수신 업데이트로 산출한 동적 BB 기반 주문과 동일한 체결 경로를 보장하지 않는다.

## 본 서브검토 결론

현재 소스의 Case B **지표 입력 및 경계 비교**는 명세에 적힌 확정봉 `%B`, 직전 3봉 저가, 터치 BBW 고정 방식과 일치한다. 다만 Lower 명세만으로 정확한 slope 수학을 독립 확정할 수 없으므로, 설계자의 백테스트 EMA 기간·기울기 계산법이 위 구현과 같은지는 비교 필요 사항이다. 실제 운영 중 진입 누락 여부는 각 세션의 봉/상태/이벤트 원본 검토 결과에 달려 있다.
