# 14초 시세 입력 공백의 원인 확인

2026-09-19 13:23:30–13:23:44 KST 구간을 프로그램 로그, 별도 차트 연결, Binance 공개 체결 내역으로 대조했다. **이 구간의 공백은 새 체결이 없었던 시간과 다음 봉 갱신 주기로 설명된다. 프로그램에서 시세가 14초 동안 처리 대기한 증거는 없다.**

| 시각, KST | 발생한 일 |
|---|---|
| 13:23:28.624 | 직전 실제 체결: aggregate trade 2084108037, 가격 2622.00, 수량 0.0038 ETH |
| 13:23:30.017 | 이를 반영한 Binance 봉 이벤트 생성 |
| 13:23:30.047 | 앱의 1분봉 입력 로그에 반영 |
| 13:23:42.034 | 다음 실제 체결: aggregate trade 2084108038, 가격 2621.99, 수량 10.0003 ETH |
| 13:23:42.591 | 다음 체결: aggregate trade 2084108039, 가격 2622.00, 수량 0.0130 ETH |
| 13:23:44.020 | 두 체결을 반영한 Binance 봉 이벤트 생성 |
| 13:23:44.039 | 앱의 1분봉 입력 로그에 반영 |
| 13:23:44.056 | 전략 평가 완료 로그 |

직전 체결과 다음 체결은 공개 aggregate trade ID가 연속이고 시간 차는 **13.410초**다. 두 봉 이벤트 사이 체결 수량 합계 **10.0133 ETH**는 로그의 1분봉 누적 거래량 증가 **46.2302 − 36.2169 = 10.0133 ETH**와 정확히 일치한다. 조회한 123개 aggregate trade ID도 모두 연속이다.

복귀한 봉 이벤트 생성 시각에서 앱 입력 기록까지 약 **19.256ms**다. 실제 첫 새 체결에서 앱 기록까지는 약 **2.005초**이며, 이를 20ms로 혼동하면 안 된다. 이 차이는 봉 이벤트 생성 주기를 포함한다. Binance 공식 문서는 현재 사용 중인 1분·30분 등 Kline 스트림 갱신 속도를 2000ms로 안내한다. 무체결 시 동작을 일반적으로 보장한다는 주장은 하지 않으며, 이번 구간의 체결·거래량 대조가 직접 근거다.

[공식 Kline 스트림 문서](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/ws-streams/~#klinecandlestick-streams-for-utc)

별도로 Binance에 연결하는 차트도 13:23:42.133 heartbeat에서 마지막 이벤트 시각이 13:23:30.017이었다. 당시 프로그램은 13:23:34.741에 running 상태를 기록했고, 13:23:36.569에 화면 연결 heartbeat도 보냈다. 중복 시세를 버렸다는 기록이나 연결 종료·재연결도 해당 구간에 없었다. 따라서 백엔드 계산·로깅 병목이나 화면만의 멈춤으로 설명할 근거는 없다.

## 영향과 한계

- 이 구간에서 체결가격은 2621.99–2622.00 USDT이고 당시 전략 하단은 약 2606.72 USDT였다. 이 14초 때문에 하단 매수 조건을 놓쳤다는 증거는 없다.
- 거래소 봉 데이터는 개별 체결 스트림보다 느리며, 이번 새 체결도 약 2초 후 전략에 반영됐다. 초 단위 반응이 필요한 조건은 이 갱신 주기를 별도로 고려해야 한다.
- 이번 공백이 통신장애라는 근거는 없지만, 다른 모든 공백도 같은 원인이라고 일반화하지 않았다.
- 현재 자동 복구는 전략 평가가 60초 이상 멈출 때 시작된다. 연결 online 표시는 최신 시세 수신을 보장하지 않는다. 진단 개선 시 마지막 체결·봉 이벤트 생성·수신·평가 시각을 구분하면 무체결과 통신 지연을 더 명확히 설명할 수 있다.

[공백 전 입력](</Users/oscar/Desktop/Binance_Auto/Log_History/2026-09-19_12-55-42-082283_KST_live_49325_01246b9353074cc6aec85fd5e5cfa87b_part0055.log:10140>), [공백 후 입력](</Users/oscar/Desktop/Binance_Auto/Log_History/2026-09-19_12-55-42-082283_KST_live_49325_01246b9353074cc6aec85fd5e5cfa87b_part0055.log:10156>), [차트의 독립 관측](</Users/oscar/Desktop/Binance_Auto/Log_History/chart/chart_1789705115004_49192_part0002.log:385>), [60초 복구 기준](</Users/oscar/Desktop/Binance_Auto/backend/src/binance_auto_trader/application/trading_controller.py:4416>), [정량 대조 증거](</Users/oscar/Desktop/Binance_Auto/artifacts/log-review-20260919/gap-evidence.json>), [공개 체결 원본](</Users/oscar/Desktop/Binance_Auto/artifacts/log-review-20260919/gap-aggtrades.json>).

공개 시세 조회만 수행했으며 계정 조회·주문·프로그램 설정 변경은 수행하지 않았다.
