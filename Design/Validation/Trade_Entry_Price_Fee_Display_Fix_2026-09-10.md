# 체결 내역 진입 가격과 실제 수수료 표시 수정

2026-09-10. 사용자가 `진입당시 ETH가격`을 매수 평균 체결가로 지정한 뒤 표시 경로를 수정했다.

## 원인

backend는 실제 평균 체결가와 주문 판단 시세를 저장하고 있었지만, UI의 `map_trade_record()`가
`entry_price=null`을 고정했다. 매도 상세에는 원래 매수와 연결된 가격도 전달되지 않았다.

수수료는 거래소의 실제 값과 저장값이 일치했다. UI가 원 수수료 asset/amount를 보존하지 않고
`fee_quote_amount`만 금액 공통 formatter에 전달해 두 자리 USDT로 반올림한 것이 문제였다.
schema v2 매수는 별도의 원 수수료 설명도 없어 ETH로 낸 수수료를 USDT로 낸 것처럼 보였다.

## 실제 거래소 대조

23:23:47 KST에 읽기 전용 REST로 해당 두 주문과 fill을 다시 조회했다.

| 항목 | 매수 | 외부 수동 매도 |
| --- | --- | --- |
| 체결 시각 | 21:35:18.511 KST | 22:32:45.885 KST |
| 평균 체결가 | 2,444.04 USDT | 2,421.86 USDT |
| 원 수수료 | 0.00000400 ETH | 0.00944525 USDT |
| USDT 환산 수수료 | 0.00977616 USDT | 0.00944525 USDT |
| 이전 주 표시 | 0.01 USDT | 0.01 USDT |

매수의 주문 판단 시세 2,444.98 USDT는 별도 정보다. 사용자의 선택에 따라 진입 가격에는
2,444.04 USDT를 사용하며, 매도 행도 해당 매수의 2,444.04 USDT를 표시한다.

## 수정

- `TradeHistory.get_entry_prices()`가 필터 전 전체 이력에서 매수 체결가를 연결한다.
- `TradeDetailsResult`는 필터 행과 같은 순서의 가격 tuple을 전달하고, 상세 DTO는 optional
  `entry_price`에 이를 게시한다. 기간 밖 BUY도 SELL 전용 조회에서 참조할 수 있다.
- UI mapper는 가격·원 수수료 자산·원 금액을 보존한다. 이전 backend의 BUY도 저장된 실제
  `average_fill_price`를 사용할 수 있으며, 근거 없는 SELL 가격은 추정하지 않는다.
- 실제 수수료를 유효 자릿수 그대로 주 표시하고 ETH/BNB의 quote 환산은 `≈`로 구분한다.
  USDT 원 수수료의 중복 안내는 없애고 MIXED는 원 자산별 근거와 환산을 구분한다.
- 거래 표의 기존 열·크기·배치를 유지한다. 수수료 셀은 필요한 경우 금액과 단위를 줄바꿈한다.

## 검증

| 검사 | 결과 |
| --- | --- |
| backend 전체 | 1,153개 실행, 1,143개 통과, opt-in 외부 주문 검사 10개 건너뜀 |
| UI 전체 | 48개 파일, 503개 통과 |
| UI typecheck·production build | 통과, 임시 디렉터리에서 빌드 |
| 실제 이력의 로컬 상세 응답 | 전체 2행·오늘 매도 1행 모두 진입 가격 2,444.04 USDT 확인 |
| 원본 보존 | 원본 history SHA-256 동일, repository 저장 0회, 주문 mutation 0회 |

회귀 검사는 40일 전 매수와 오늘 매도의 연결, 이후 새 매수와의 가격 분리, SELL 전용 필터,
실제 ETH/USDT 수수료와 9자리 BNB 수수료, MIXED·0 수수료, 잘못된 가격 입력 거부를 포함한다.

backend가 현재 실행 중이면 새 상세 응답의 매도 진입 가격은 `pnpm desktop:dev` 재실행 후
반영된다. 현재 process를 강제 종료하거나 실거래를 재시작하지 않았다.

## 증거

- [실제 거래소 재조회](/Users/oscar/Desktop/Binance_Auto/Log_History/manual_exit_recovery_2026-09-10/trade_price_fee_exchange_evidence.json)
- [원본 이력을 사용한 상세 응답 검증](/Users/oscar/Desktop/Binance_Auto/Log_History/trade_price_fee_fix_2026-09-10/actual_history_display.json)
- [backend 전체 검사](/Users/oscar/Desktop/Binance_Auto/Log_History/trade_price_fee_fix_2026-09-10/backend.log)
- [UI 전체 검사](/Users/oscar/Desktop/Binance_Auto/Log_History/trade_price_fee_fix_2026-09-10/ui.log)
