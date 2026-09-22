# Case B 주문 차단 독립 검토 — 2026-09-18

작성일: 2026-09-22. 코드·설정·거래 장부는 변경하지 않았고, 저장된 로그와 저장소 이력만 읽었다. 이 문서 및 CSV·과거 코드 사본은 분석 결과물이다. 시각은 한국 시간이다.

## 결론

9월 18일에는 **Case B 진입 신호가 실제 발생했고 앱도 매수를 요청했다.** 그러나 주문 수량을 계산할 때 이미 남아 있던 소액 ETH의 평가액을 총 보유 한도에서 먼저 빼지 않아, 최종 위험 검사에서 총 한도 초과로 차단됐다. 기록된 31회 모두 같은 원인이다. **Case B 가격 조건이 없었던 사건도, Binance가 주문을 거절한 사건도 아니다.**

- 실행 ID: `d50b36bb80314306aacd20a6932b85b0`
- 세션 ID: `d067fe95-ca2f-4552-9542-c25859d5697e`
- Case B 신호 확정 1회: 2026-09-18 10:00:00.075291
- 첫 주문 후보 생성: 10:03:38.055394, 첫 위험 검사 차단: **10:03:38.498876**
- 마지막 위험 검사 차단: **10:06:32.574328**
- 위험 검사 31회, 모두 `RISK_POSITION_NOTIONAL_EXCEEDED`
- 모두 `exchange_order_id=null`, `filled_quantity=0`, 제출 횟수 미소비(`submission_attempt=0`). 각 trace는 RiskPolicy 단계 5.1에서 끝난다.
- 이는 **서로 독립적인 신호 31개가 아니라, 같은 신호를 유지한 채 시세 갱신 때마다 생성한 주문 후보 31개**다.

[31회 전체 목록](/Users/oscar/Desktop/Binance_Auto/artifacts/case-b-audit-20260922/case-b-blocked-orders.csv)

## 최초 진입 후보에서 일어난 일

| 항목 | 값 |
|---|---:|
| 당시 주문 결정 가격 | 2,446.45 USDT |
| 단건 매수 한도 | 10 USDT |
| 총 보유 한도 | 10 USDT |
| 실제 전략 포지션 | 0 ETH |
| 별도 남은 ETH 원금 | 0.000096 ETH |
| 남은 ETH 평가액 | 0.2348592 USDT |
| 예약된 미체결 BUY 평가액 | 0 USDT |
| 코드가 먼저 요청한 수량 | 0.004087555437470620695293179913752580 ETH |
| 거래소 단위 적용 후 주문 후보 수량 | 0.0040 ETH |
| 후보 주문 금액 | 9.7858 USDT |
| 기존 소액 ETH + 후보 주문의 합계 | **10.0206592 USDT** |
| 총 보유 한도 초과분 | **0.0206592 USDT** |

수량 산정은 사실상 `10 / 2446.45`였고, 그 수량을 거래소 수량 단위로 내리면 0.0040 ETH가 됐다. 새 주문 하나만 보면 10 USDT보다 작지만, 최종 위험 검사는 소액 잔여 ETH까지 합산한다. 따라서 `0.2348592 + 9.7858 = 10.0206592 > 10`이라는 이유로 주문을 막았다.

원본 근거:

- [실행 설정: 단건·총 보유 한도 각각 10](/Users/oscar/Desktop/Binance_Auto/Log_History/2026-09-17_23-07-25-146636_KST_live_6481_d50b36bb80314306aacd20a6932b85b0_part0001.log:2)
- [10:00 현재 전략 포지션 없음·잔여 ETH 0.000096](/Users/oscar/Desktop/Binance_Auto/Log_History/2026-09-18_09-48-16-056650_KST_live_6481_d50b36bb80314306aacd20a6932b85b0_part0025.log:4583)
- [첫 주문 준비 시작](/Users/oscar/Desktop/Binance_Auto/Log_History/2026-09-18_09-48-16-056650_KST_live_6481_d50b36bb80314306aacd20a6932b85b0_part0025.log:5814)
- [첫 위험 검사: 금액 계산과 차단 사유](/Users/oscar/Desktop/Binance_Auto/Log_History/2026-09-18_09-48-16-056650_KST_live_6481_d50b36bb80314306aacd20a6932b85b0_part0025.log:5819)
- [첫 실패 trace: 단계 5.1](/Users/oscar/Desktop/Binance_Auto/Log_History/2026-09-18_09-48-16-056650_KST_live_6481_d50b36bb80314306aacd20a6932b85b0_part0025.log:5820)
- [첫 Case B 매수 신호: B-09 / CASE_B / BUY](/Users/oscar/Desktop/Binance_Auto/Log_History/2026-09-18_09-48-16-056650_KST_live_6481_d50b36bb80314306aacd20a6932b85b0_part0025.log:5821)
- [차단 피드백으로 B_WAIT_PULLBACK 복귀](/Users/oscar/Desktop/Binance_Auto/Log_History/2026-09-18_09-48-16-056650_KST_live_6481_d50b36bb80314306aacd20a6932b85b0_part0025.log:5824)
- [마지막 위험 검사: 동일 원인](/Users/oscar/Desktop/Binance_Auto/Log_History/2026-09-18_09-48-16-056650_KST_live_6481_d50b36bb80314306aacd20a6932b85b0_part0025.log:7346)

위 잔여 ETH 0.000096은 위험 검사에 포함된 원금이다. 원본 잔고 조정 정보의 이자 0.00000001 ETH까지 포함한 거래소 spot 잔액은 0.00009601이므로, 두 숫자를 혼동하지 않아야 한다.

## 왜 거래소 주문 전 차단이라고 판단하는가

31개 후보의 주문 trace는 각각 `1 → 2 → 3 → 4 → 5.1 실패`라는 동일한 순서다. 성공 124개와 위험 실패 31개로 정확히 일치한다. 주문 전송·거래소 체결의 후속 trace는 없다.

해당 시기의 저장소 코드에서도 필터 준비 이후 `_evaluate_buy_order_risk()`를 호출하고, 실패하면 `BUY_RISK_BLOCKED` 피드백만 반환하도록 되어 있다. 주문 복구 장부 저장과 실제 주문 전송 코드는 이 반환 다음에 있다. 준비 과정에서 거래소 필터를 조회했을 수는 있지만, 매수 주문 POST는 하지 않은 흐름이다.

- [과거 코드: 필터 준비](/Users/oscar/Desktop/Binance_Auto/artifacts/case-b-audit-20260922/historical-trading-controller-7244cb5.py:6773)
- [과거 코드: 위험 검사와 조기 반환](/Users/oscar/Desktop/Binance_Auto/artifacts/case-b-audit-20260922/historical-trading-controller-7244cb5.py:6853)
- [과거 코드: 실제 전송 전 반환임을 명시](/Users/oscar/Desktop/Binance_Auto/artifacts/case-b-audit-20260922/historical-trading-controller-7244cb5.py:6884)
- [과거 코드: 위험 차단 후 신호 감시로 복귀](/Users/oscar/Desktop/Binance_Auto/artifacts/case-b-audit-20260922/historical-ownership-transitions-7244cb5.py:53)

로그에서 `submitted_quantity`라는 필드명을 사용하지만, 여기서는 **거래소 수량 단위를 적용한 전송 예정 수량**이다. 해당 값이 있다는 것 자체는 실제 전송 증거가 아니다.

## 당시 코드와 현재 코드의 차이

실행 직전 저장소 커밋 `7244cb5`(2026-09-17 22:48:53)의 코드를 별도 사본으로 확인했다. 로그에는 실행 바이너리의 커밋 해시가 없으므로, 이 커밋과 실행 바이너리가 바이트 단위로 같다고 단정하지 않는다. 다만 아래 흐름이 실제 31개 위험 판정의 산식과 일치한다.

1. 기존 `_calculate_order_quantity()`는 가용 USDT와 분할 비율에서 수량을 계산한다.
2. `_apply_order_notional_ceiling()`은 단건 한도 10 USDT만 적용한다.
3. 거래소 단위로 내림한 뒤, 위험 검사가 **전략 포지션 + 잔여 ETH + 예약 BUY + 새 후보**를 총 한도와 비교한다.
4. 수량 계산 단계에는 잔여 ETH를 차감하는 단계가 없으므로, 수량 계산과 최종 검사 사이의 기준이 달랐다.

- [과거 수량 계산](/Users/oscar/Desktop/Binance_Auto/artifacts/case-b-audit-20260922/historical-trading-controller-7244cb5.py:7125)
- [과거 단건 상한만 적용](/Users/oscar/Desktop/Binance_Auto/artifacts/case-b-audit-20260922/historical-trading-controller-7244cb5.py:7195)
- [과거 위험 검사는 잔여 ETH 합산](/Users/oscar/Desktop/Binance_Auto/artifacts/case-b-audit-20260922/historical-trading-controller-7244cb5.py:7075)

현재 작업 폴더에는 **남은 총 예산만큼 수량을 먼저 줄이는 수정**이 있다.

- [현재 코드: 필터 준비 전 `_limit_buy_quantity()` 호출](/Users/oscar/Desktop/Binance_Auto/backend/src/binance_auto_trader/application/trading_controller.py:7273)
- [현재 코드: `총한도 − 전략포지션 − 잔여 ETH − 예약액`으로 수량 제한](/Users/oscar/Desktop/Binance_Auto/backend/src/binance_auto_trader/application/trading_controller.py:7680)
- [현재 코드: 최종 총 보유 한도 차단 규칙](/Users/oscar/Desktop/Binance_Auto/backend/src/binance_auto_trader/domain/trading/risk.py:433)

수정 보고서는 2026-09-18자로 작성되어 있고, 해당 함수와 보고서는 Git 이력상 `e5af533`(2026-09-20 13:53:05)에 처음 포함된다. **수정 작성일·Git 기록일·사용자가 실제 실행한 앱에 반영된 시각은 서로 다르므로, 저장소 이력만으로 실제 배포 시각까지 단정하지 않는다.**

[기존 잔여 예산 수정 보고서](/Users/oscar/Desktop/Binance_Auto/artifacts/buy-remaining-budget-20260918/report.md)

## 수량을 적절히 줄였다면

첫 시점의 남은 총 예산은 `10 − 0.2348592 = 9.7651408 USDT`다. 이 예산을 가격으로 나눈 수량은 약 0.0039915554 ETH이고, 수량 단위 0.0001을 적용하면 0.0039 ETH가 된다.

| 항목 | 실제 과거 후보 | 남은 예산을 먼저 반영한 후보 |
|---|---:|---:|
| 주문 수량 | 0.0040 ETH | 0.0039 ETH |
| 새 주문 금액 | 9.7858000 USDT | 9.5411550 USDT |
| 기존 잔여 ETH 포함 합계 | 10.0206592 USDT | 9.7760142 USDT |
| 총 보유 한도 10 검사 | 초과 | 충족 |

따라서 **같은 10 USDT 한도를 유지하면서도 총 보유 한도 검사를 통과하는 더 작은 주문 후보가 존재했다.** 다만 당시 원본 `exchangeInfo` 최소 주문 금액은 이 로그에 저장되어 있지 않다. 그러므로 이 산식만으로 실제 거래소 체결까지 보장할 수는 없다. 기존 9/18 수정 보고서의 0.0039 ETH 체결 검증은 최소 주문 금액 5 USDT를 설정한 모의 실험이며, 최소 주문 금액 10 USDT 환경에서는 보류되도록 별도로 검증한 결과다.

## 투자 명세와의 관계

명세의 Case B 실제 매수 조건은 신호 확정 후 3시간 이내에 realtime %B가 0.30 이하가 되는 것이다. 첫 후보는 신호 확정 약 218.05초 후, realtime %B 0.2991654024이므로 **명세의 가격·시간 기준에서는 매수할 자리**다. 당시 전략 포지션도 없었고 Case B 진입 일시정지도 아니었다.

[명세 실제 매수 조건](/Users/oscar/Desktop/Binance_Auto/Design/Specification/Lower_bb_logic_specification.md:114)

따라서 설명을 두 층으로 나누는 것이 정확하다.

- 투자 조건 판단: 진입 조건 충족. 앱도 인식하여 매수를 요청했다.
- 실제 실행 판단: 기존 수량 산정이 잔여 ETH 예산을 차감하지 않아 로컬 위험 검사에서 차단. 당시 만들어진 0.0040 ETH 주문을 막은 검사는 설정상 정상이며, 앞 단계의 수량 조정이 부족했다.

9/18 01:44와 09:34의 UI 연결 오류 및 01:01의 평가 정체 기록은 별도로 존재한다. 그러나 10:03~10:06의 31회 차단마다 위험 예산과 정확한 실패 사유가 기록되어 있으므로, 이 매수 불발의 직접 원인을 UI 연결 오류로 설명하면 근거와 맞지 않는다.
