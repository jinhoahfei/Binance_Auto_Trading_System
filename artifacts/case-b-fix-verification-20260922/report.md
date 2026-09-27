# Case B 수정 적용 및 재발 방지 확인

확인일: 2026-09-22, 약 17:27–17:34 KST.

## 결론

**현재 소스와 프로젝트의 배포용 앱에는 잔여 ETH를 먼저 차감하는 수정이 적용되어 있다. 같은 입력으로 재현했을 때 Case B 매수 후보가 정상적으로 주문 단계로 진행했다. 그러나 `/Applications/Binance Auto Trader.app`에는 수정이 없고, 이 앱에서는 같은 한도 초과 차단이 재현됐다.**

확인 시점에 실행 중인 Binance Auto Trader / sidecar 프로세스는 발견되지 않았다. 따라서 다음 실제 실행에 어느 앱이 사용될지는 확인할 수 없다. 앱을 시작하거나 실제 주문을 제출하지 않았다. 프로그램과 운영 장부도 변경하지 않았다.

## 앱별 확인 결과

| 대상 | 잔여 예산 수정 | 검증 |
| --- | --- | --- |
| 현재 Python 소스 | 있음 | 관련 36개 검사 통과 + 당시 가격 재현 통과 |
| 프로젝트의 배포용 `.app` | 있음 | 앱 안의 모듈로 36개 검사 통과 + 당시 가격 재현 통과 |
| 다음 빌드에 쓰는 sidecar 바이너리 | 있음 | 바이너리 안의 모듈로 36개 검사 통과 |
| 응용 프로그램 폴더의 `.app` | 없음 | 당시 가격 재현에서 `RISK_POSITION_NOTIONAL_EXCEEDED`, 주문 0건 |

수정이 확인된 앱:
[Binance Auto Trader.app](</Users/oscar/Desktop/Binance_Auto/UI/apps/desktop/src-tauri/target/release/bundle/macos/Binance Auto Trader.app>)

수정이 없는 앱:
[응용 프로그램 폴더의 기존 앱](</Applications/Binance Auto Trader.app>)

배포용 앱에 들어 있는 백엔드 파일의 수정 시각은 9월 20일 22:21 KST, 기존 설치 앱의 백엔드 파일은 9월 16일 16:44 KST다. 판정은 파일 날짜만 비교한 것이 아니라 각 앱 내부의 Python 모듈을 직접 읽어 수행했다. 파일 해시는 [확인 기록](/Users/oscar/Desktop/Binance_Auto/artifacts/case-b-fix-verification-20260922/binary-evidence.json)에 보관했다.

## 9월 18일 입력 재현

Case B 진입 신호가 이미 발생한 주문 경계에 당시 결정 가격 **2,446.45 USDT**, 잔여 수량 **0.000096 ETH**, 총 보유 한도 **10 USDT**를 입력했다. 이 검사는 과거 봉 전체를 다시 재생한 검사가 아니라, 이전 분석에서 확인한 진입 신호 이후의 주문 차단 원인을 검증한다.

| 항목 | 기존 설치 앱 | 수정된 배포용 앱 |
| --- | ---: | ---: |
| 주문 후보 수량 | 0.004 ETH | 0.0039 ETH |
| 새 주문 금액 | 9.785800 USDT | 9.541155 USDT |
| 기존 잔여 ETH 평가액 | 0.23485920 USDT | 0.23485920 USDT |
| 합산 예상 보유액 | 10.02065920 USDT | 9.77601420 USDT |
| 보유 한도 검사 | 초과로 차단 | 통과 |
| 모의 주문 제출 | 0건 | 1건 |
| 추가 시세 입력 31회 후 | 해당 없음 | 여전히 주문 1건·거래 기록 1건 |

실제 거래소 주문·체결은 수행하지 않았다. 체결 응답과 계정 응답은 테스트용이다. 네트워크 연결을 막은 상태에서 임시 저장소와 모의 거래소를 사용했다. 앱 내부 검사는 소스 코드로의 대체 로드를 금지했다.

[기존 앱 재현](/Users/oscar/Desktop/Binance_Auto/artifacts/case-b-fix-verification-20260922/installed-replay.log) · [수정 앱 재현](/Users/oscar/Desktop/Binance_Auto/artifacts/case-b-fix-verification-20260922/release-replay.log) · [현재 소스 재현](/Users/oscar/Desktop/Binance_Auto/artifacts/case-b-fix-verification-20260922/source-replay.log)

## 앞으로도 정상적으로 매수가 보류되는 경우

수정은 매수 조건이나 10 USDT 한도를 완화하는 것이 아니라, **기존 보유분과 아직 체결되지 않은 매수 예약분을 먼저 빼고 주문 크기를 줄이는 것**이다.

- 잔여 예산이 최소 주문 금액보다 작으면 정상 대기한다. 같은 조건의 원격 준비 요청은 30초 간격으로 제한하며, 예산이 회복되는 등 조건이 바뀌면 즉시 재검사한다.
- 일부만 체결된 수량과 아직 체결되지 않은 매수 예약분을 함께 계산한다.
- Case B 신호가 3시간을 넘으면, 그 뒤 예산이 회복돼도 지난 신호로 매수하지 않는다.
- 긴급 정지나 정책 불일치 같은 별도 차단은 유지한다.

위 경계 사례를 포함해 잔여 예산 검사 17개와 위험 한도 검사 19개, 합계 36개가 현재 소스와 수정된 앱에서 각각 통과했다.

[수정 앱 36개 검사](/Users/oscar/Desktop/Binance_Auto/artifacts/case-b-fix-verification-20260922/release-tests.log) · [소스 검사](/Users/oscar/Desktop/Binance_Auto/artifacts/case-b-fix-verification-20260922/source-tests.log) · [다음 빌드용 백엔드 검사](/Users/oscar/Desktop/Binance_Auto/artifacts/case-b-fix-verification-20260922/build-input-tests.log)

기존 설치 앱에는 새 검사에 필요한 인터페이스도 없어 36개 중 19개가 오류로 끝났다. 이 숫자만으로 실제 거래 실패를 단정하지 않았으며, 구버전과 호환되는 별도 사건 재현에서 같은 한도 초과 차단을 확인했다.

## 현재 거래소 규칙 확인

9월 22일 **17:30:44 KST**에 읽은 [Binance ETHUSDT 공개 규칙](https://api.binance.com/api/v3/exchangeInfo?symbol=ETHUSDT)은 최소 주문 금액 **5 USDT**, LOT_SIZE 수량 단위 **0.0001 ETH**였다. 따라서 위 수정 후 주문 후보 9.541155 USDT는 이 최소 금액을 충족한다.

정확한 가격 재현에는 이때 조회한 공개 필터 전체를 사용했다. 계정별 필터는 공개 필터와 일치하는 모의 응답, 미체결 주문과 주문 목록은 모두 없는 모의 상태로 두었다. 실제 계정의 현재 잔고·미체결·계정별 한도·주문 직전 기준 가격·거래소 응답은 확인하지 않았다. 현재 규칙이 9월 18일에도 동일했다고 소급해서 단정하지 않는다.

[공개 규칙 원본과 조회 시각](/Users/oscar/Desktop/Binance_Auto/artifacts/case-b-fix-verification-20260922/current-exchange-filters.json)

## 확인 범위

이번에 확인한 것은 **Case B가 유효한 진입 신호를 냈는데 잔여 ETH 때문에 수량이 과대 계산되어 막히던 원인과 그 수정**이다. 모든 향후 매매나 별개의 통신·시세 입력 문제까지 정상임을 보장하는 결과는 아니다. 수정된 앱을 실행해야 이번 검증 결과가 실제 구동에 적용된다.

코드 근거: [주문 수량 제한](/Users/oscar/Desktop/Binance_Auto/backend/src/binance_auto_trader/application/trading_controller.py:7680), [잔여 예산 회귀 검사](/Users/oscar/Desktop/Binance_Auto/backend/tests/integration/test_buy_remaining_budget.py).
