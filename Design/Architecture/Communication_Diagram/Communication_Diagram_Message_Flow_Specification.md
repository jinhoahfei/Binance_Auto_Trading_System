# Communication Diagram 메시지 호출 명세

## 1. 문서 목적

이 문서는 다음 4개의 Communication Diagram의 메시지 호출 순서, 호출자와 수신자, 매개변수, 반환형, 메시지의 의미 및 실제 동작 과정을 구현 가능한 수준으로 설명한다.

- `Start Trading and Stop Trading.png`
- `Buy and Sell.png`
- `Show Trade History Details.png`
- `CSV Export.png`
- 위 네 이미지를 묶은 `Communication_Diagram.pdf`

추론한 타입은 새로운 기능이나 새로운 collaboration을 제안하는 것이 아니라, 다이어그램의 기존 메시지를 코드 operation으로 표현하기 위한 계약이다.

## 2. UML 표기법

### 2.1 Attribute

Attribute는 다음 형식으로 표기한다.

```text
이름 : 타입 = 초기값 {속성}
```

- `이름`은 attribute 이름이다.
- `타입`은 attribute가 보관하는 값의 타입이다.
- `= 초기값`은 초기값이 있을 때만 적고, 초기값이 없으면 생략할 수 있다.
- `{속성}`은 `{not null}`, `{unique}`, `{readOnly}`처럼 값의 제약이나 특성을 적는다.

예:

```text
context : TradingContext {not null}
filledQuantity : Decimal = 0 {not null}
exchangeOrderId : Long? = null
```

### 2.2 Operation

Operation은 다음 형식으로 표기한다.

```text
이름(parameter list) : return type
```

- parameter는 `parameterName : Type` 형식으로 적는다.
- parameter가 여러 개면 쉼표로 구분한다.
- 기본값이 있으면 `parameterName : Type = defaultValue`로 적는다.
- nullable 값은 `Type?`으로 적는다.
- 반환값이 없으면 `void`를 사용한다.
- 생성 메시지도 이 문서에서는 반환형을 생략하지 않고 `Order(...) : Order`처럼 적는다.

예:

```text
executeTrade(order : Order) : bool
find(query : TradeHistoryQuery) : List<Trade>
stopTrading() : void
```

## 3. 공통 타입과 원본 표기 정합성

### 3.1 공통 타입

아래 이름은 매개변수와 반환형을 명확하게 하기 위한 값 타입 또는 결과 타입이다. 별도의 Communication Diagram 참여 클래스를 새로 제안하는 것이 아니다.

| 타입 | 의미 |
|---|---|
| `Decimal` | 가격, 수량, 금액, 비율처럼 부동소수 오차를 피해야 하는 수치 |
| `Instant` / `LocalDate` | 거래 시각 / UI 날짜 |
| `Path` | 로컬 파일 또는 디렉터리 경로 |
| `Interval` | `1m`, `30m`, `4h`, `1d` Kline 주기 |
| `Kline` | `symbol`, `interval`, `openTime`, OHLCV를 가진 봉 데이터 |
| `RegimeType` | 사용자가 선택하거나 RegimeSTM이 추천하는 REGIME 타입 |
| `HistoryPeriod` | 거래 상세 조회 기간: `TODAY`, `LAST_7_DAYS`, `LAST_30_DAYS`, `ALL` |
| `TradeSide` | 거래 방향 필터: `ALL`, `BUY`, `SELL` |
| `CSVPeriod` | CSV 범위 preset: `TODAY`, `WEEKLY`, `MONTHLY`, `CUSTOM` |
| `CSVExportStatus` | CSV 내보내기 상태: `IDLE`, `VALIDATING`, `EXPORTING`, `SUCCEEDED`, `FAILED` |
| `UIEvent`, `UITransitionResult` | UI STM에 전달되는 이벤트와 전이 결과 |
| `TradingEvent`, `TradingSTMResult` | Trading STM에 전달되는 이벤트와 전이/action 결과 |
| `AccountSnapshot` | REST 또는 user-data stream에서 정규화한 계좌 잔액 snapshot |
| `OrderResult` | 주문 상태와 fill을 정규화한 Gateway 결과 |
| `ExecutionSummary` | 한 주문의 여러 fill을 합친 체결 수량·금액·평균가·수수료 요약 |
| `RealizedResult` | 매도 체결의 실현손익 및 수익률 결과 |
| `TradeDetailsResult` | 거래 목록, 보유량, 성과를 묶은 상세 화면 표시 결과 |
| `ValidationResult` | CSV option 검증 성공 여부와 오류 항목/사유 |
| `CSVExportResult` | CSV 생성 성공 여부, 생성 경로 또는 실패 사유 |
| `Subscription` | WebSocket 구독 handle |

### 3.2 원본 표기 정합성

아래 항목은 기능 차이가 아니라 다이어그램의 철자 또는 이름 정합성 문제다. 메시지 설명과 클래스 목록에서는 구현 가능한 정상 철자를 사용하되, 각 표의 설명에 원본 철자를 남긴다.

| 원본 표기 | 문서의 정규 표기 | 비고 |
|---|---|---|
| `hadle(event)` | `handle(event)` | Buy and Sell 메시지 1 |
| `fetchSelectedTardingLogic()` | `fetchSelectedTradingLogic(...)` | Start/Stop 메시지 6.1.1.1 |
| `get 30mKlines()` | `get30mKlines(...)` | Start/Stop 메시지 1.2.2 |
| VPP 내부 모델명 `recordOrderExcution()` | `recordOrderExecution(...)` | Buy and Sell 메시지 13의 PNG/PDF 표기를 우선함 |
| `tradeHisoryFilterChanged()` | `tradeHistoryFilterChanged(...)` | History 메시지 2.1 |
| `TradeHostoryUI` | `TradeHistoryUI` | History PNG의 lifeline 이름 |
| `TradingHistoryUI` | `TradeHistoryUI` | CSV PNG의 lifeline 이름 |
| `WebsocketGateway` | `WebSocketGateway` | Buy and Sell PNG의 표기이며, Start/Stop PNG의 `WebSocketGateway`와 같은 클래스 |
| `setRegimeType`, `update`, `setSaveLocation` | 각각 `setRegimeType(...)`, `update(...)`, `setSaveLocation(...)` | 원본에는 괄호가 없음 |

`TradingController`와 초안의 `TradingCoordinator`, `UISTM`과 초안의 `UIStateMachine`, `APIGateway`와 초안의 `BinanceAPIGateway`처럼 이름이 충돌하는 경우에는 다이어그램의 `TradingController`, `UISTM`, `APIGateway`를 사용한다. WebSocket Gateway는 Start/Stop의 `WebSocketGateway`를 통합 클래스명으로 사용하되, Buy and Sell 메시지 설명에서는 그 그림의 `WebsocketGateway` 표기를 그대로 남긴다.

## 4. Case 1 - Start Trading and Stop Trading

이 다이어그램의 최상위 순서는 다음과 같다.

1. `1`~`5`: 앱 구동에 필요한 시장 데이터, 계좌, 거래 이력, UI 상태 및 interface 초기화
2. `6`: 사용자의 REGIME 선택
3. `7`: 사용자의 자동매매 시작 확인
4. `8`: 사용자의 자동매매 중지 확인

### 4.1 시장 데이터 초기화 및 REGIME 추천

| 번호 | 호출자 -> 수신자 | Operation | Parameter | Return | 설명 | 동작 과정 |
|---|---|---|---|---|---|---|
| `1` | `UIStateController -> MarketDataController` | `InitializeMarketData(symbol : String = "ETHUSDT") : MarketSnapshot` | `symbol` | `MarketSnapshot` | 시장 데이터 초기화 전체 흐름을 시작한다. | `1.1`에서 실시간 봉 버퍼링을 먼저 시작한 뒤 `1.2`에서 과거 봉을 조회하고, `1.3`에서 두 데이터 집합을 병합한다. 이어서 `1.4`~`1.5`로 4H 지표와 추천 REGIME을 계산한다. |
| `1.1` | `MarketDataController -> WebSocketGateway` | `startAllKlineBuffering(symbol : String, intervals : Set<Interval>) : Subscription` | 거래 symbol, `1m/30m/4h/1d` 주기 집합 | `Subscription` | REST 조회 중 발생하는 최신 봉을 잃지 않도록 WebSocket 버퍼링을 먼저 시작한다. | Gateway는 `1.1.1`의 구독을 만든 뒤 수신 Kline을 정규화해 초기화용 buffer에 쌓는다. 반환되는 구독 handle은 별도 응답 메시지로 그리지 않는다. |
| `1.1.1` | `WebSocketGateway -> Binance WebSocket` | `subscribeAllKlineStreams(symbol : String, intervals : Set<Interval>) : Subscription` | 거래 symbol, 주기 집합 | `Subscription` | Binance의 모든 대상 Kline stream을 구독한다. | 구독 뒤 Binance가 비동기로 보내는 봉을 Gateway가 `symbol`, `interval`, `openTime` 기준의 내부 Kline으로 정규화한다. |
| `1.2` | `MarketDataController -> APIGateway` | `loadAllKlines(symbol : String, limit : int) : Map<Interval, List<Kline>>` | 거래 symbol, 주기별 조회 개수 | 주기별 과거 봉 | 모든 대상 주기의 과거 봉을 한 번에 준비한다. | `1.2.1`~`1.2.4`를 호출하고 그 반환값을 주기별 Map으로 묶는다. |
| `1.2.1` | `APIGateway -> Binance REST API` | `get1mKlines(symbol : String, limit : int) : List<Kline>` | 거래 symbol, 조회 개수 | 1분봉 목록 | 1분봉 과거 데이터를 조회한다. | Binance 응답을 Kline 목록으로 정규화해 `1.2`의 호출 결과에 포함한다. |
| `1.2.2` | `APIGateway -> Binance REST API` | `get30mKlines(symbol : String, limit : int) : List<Kline>` | 거래 symbol, 조회 개수 | 30분봉 목록 | 30분봉 과거 데이터를 조회한다. | 원본 라벨은 공백이 있는 `get 30mKlines()`이며, 구현 시 사용할 수 있도록 `get30mKlines(...)`로 정규화했다. |
| `1.2.3` | `APIGateway -> Binance REST API` | `get4hKlines(symbol : String, limit : int) : List<Kline>` | 거래 symbol, 조회 개수 | 4시간봉 목록 | 4H REGIME 계산에 필요한 4시간봉을 조회한다. | 확정봉과 현재 진행 중인 봉을 함께 받아 이후 RegimeController가 구분한다. |
| `1.2.4` | `APIGateway -> Binance REST API` | `get1dKlines(symbol : String, limit : int) : List<Kline>` | 거래 symbol, 조회 개수 | 일봉 목록 | 일봉 과거 데이터를 조회한다. | 반환값을 다른 주기의 Kline과 함께 `1.2`의 결과에 포함한다. |
| `1.3` | `MarketDataController -> MarketSnapshot` | `update(klines : Map<Interval, List<Kline>>) : void` | REST 봉과 초기 WebSocket buffer를 병합한 결과 | `void` | 최신 시장 snapshot을 갱신한다. | 같은 `symbol`, `interval`, `openTime` 봉은 중복 제거하고 WebSocket 값을 우선한다. 이후 시간순 정렬과 봉 연속성을 검사한다. |
| `1.4` | `MarketDataController -> RegimeController` | `calculate4HIndicators(snapshot : MarketSnapshot) : IndicatorSnapshot` | 최신 시장 snapshot | 계산된 지표 snapshot | REGIME 판정용 4H 지표를 계산한다. | 확정 4H 봉과 진행 중인 4H 봉을 분리한다. 확정봉으로 EMA9 시계열, 최근 6개 EMA9의 LR slope와 HH/HL/LH/LL 구조를 계산하고 진행 중인 봉으로 실시간 EMA9를 계산한다. |
| `1.4.1` | `RegimeController -> IndicatorSnapshot` | `update(ema9Series : List<Decimal>, ema9Slope : Decimal, swingStructure : SwingStructure, liveEma9 : Decimal) : void` | 계산한 4H 지표 | `void` | 계산 결과를 동일 평가 시점의 IndicatorSnapshot에 반영한다. | 원본에는 `update`만 있고 괄호가 없다. 이 snapshot이 `1.5.1`의 STM 입력이 된다. |
| `1.5` | `MarketDataController -> RegimeController` | `recommendRegime(indicators : IndicatorSnapshot) : RegimeType` | 4H IndicatorSnapshot | 추천 REGIME | 현재 4H 데이터에 대한 추천 REGIME을 요청한다. | RegimeController는 `1.5.1`에 판정을 위임하고 그 함수 반환값을 추천값으로 사용한다. 별도 reply 화살표는 필요 없다. |
| `1.5.1` | `RegimeController -> RegimeSTM` | `run(indicators : IndicatorSnapshot) : RegimeType` | 4H 지표 snapshot | 추천 REGIME | Regime STM의 판정 전이를 실행한다. | guard와 다음 상태는 RegimeSTM이 결정한다. 추천값은 사용자 선택값을 자동으로 덮어쓰지 않는다. |

### 4.2 계좌 초기 snapshot 및 account stream

| 번호 | 호출자 -> 수신자 | Operation | Parameter | Return | 설명 | 동작 과정 |
|---|---|---|---|---|---|---|
| `2` | `UIStateController -> TradingController` | `loadAccount(asset : String = "ETH") : Account` | 조회할 기준 자산 | 초기화된 `Account` | 계좌 초기 snapshot을 준비하고 실시간 계좌 stream을 시작한다. | `2.1`의 REST snapshot을 내부 `Account`에 반영한 후 `2.2`로 변경 stream을 구독한다. |
| `2.1` | `TradingController -> APIGateway` | `fetchAccountSnapshot(asset : String = "ETH") : AccountSnapshot` | 기준 자산 | 정규화된 계좌 snapshot | 최초 화면과 거래 context에 사용할 계좌 정보를 조회한다. | `2.1.1`의 Binance 응답을 잔액, ETH 보유량, 평가 정보로 정규화한다. |
| `2.1.1` | `APIGateway -> Binance REST API` | `getAccount() : BinanceAccountResponse` | 없음 | Binance 계좌 응답 | Binance 계좌 정보 API를 호출한다. | 반환 응답은 `fetchAccountSnapshot(...)` 내부에서 소비하며 별도 메시지 답변을 추가하지 않는다. |
| `2.2` | `TradingController -> WebSocketGateway` | `startAccountInfoStream() : Subscription` | 없음 | 계좌 stream 구독 handle | 최초 snapshot 이후의 잔액 변경을 받을 user-data stream을 시작한다. | Gateway가 `2.2.1`을 통해 구독을 만들고 이후 account event를 정규화한다. |
| `2.2.1` | `WebSocketGateway -> Binance WebSocket` | `subscribeAccountInfo() : Subscription` | 없음 | 구독 handle | Binance account/user-data stream을 구독한다. | 구독 자체의 응답은 함수 반환값이다. 이후 비동기 event 처리는 해당 구독 callback의 책임이며 이 초기 구독 메시지에 reply 화살표를 추가하지 않는다. |

### 4.3 거래 이력과 성과 로드

| 번호 | 호출자 -> 수신자 | Operation | Parameter | Return | 설명 | 동작 과정 |
|---|---|---|---|---|---|---|
| `3` | `UIStateController -> TradeHistoryController` | `loadTradeHistory() : TradeHistory` | 없음 | 초기화된 거래 이력 | 저장된 거래 이력과 누적 성과를 메모리에 준비한다. | `3.1`에서 저장 데이터를 가져오고 `3.2`의 TradeHistory와 `3.3`의 Performance를 초기화한다. |
| `3.1` | `TradeHistoryController -> TradeHistoryRepository` | `getTradeHistory() : List<Trade>` | 없음 | 저장된 거래 목록 | repository에 저장된 모든 Trade를 요청한다. | `3.1.1`에서 파일을 읽고 역직렬화한 목록을 반환한다. |
| `3.1.1` | `TradeHistoryRepository -> Local File System` | `read(path : Path) : String` | 거래 이력 저장 경로 | 직렬화된 파일 내용 | 로컬 거래 이력 파일을 읽는다. | 파일 내용의 parsing과 `List<Trade>` 변환은 repository 내부 처리이므로 별도 메시지를 만들지 않는다. |
| `3.2` | `TradeHistoryController -> TradeHistory` | `TradeHistory(trades : List<Trade>) : TradeHistory` | 읽은 거래 목록 | TradeHistory 인스턴스 | 인메모리 거래 이력 entity를 생성 또는 초기화한다. | 이후 주문 기록, 상세 조회 및 필터의 공통 원천으로 사용한다. |
| `3.3` | `TradeHistoryController -> Performance` | `Performance(trades : List<Trade>) : Performance` | 읽은 거래 목록 | Performance 인스턴스 | 기존 거래를 기준으로 누적 성과를 복원한다. | 실현손익, 수수료, 누적 수익률과 매도 성과를 계산해 이후 조회와 신규 Trade 반영에 사용한다. |

### 4.4 UI STM 및 interface 시작

| 번호 | 호출자 -> 수신자 | Operation | Parameter | Return | 설명 | 동작 과정 |
|---|---|---|---|---|---|---|
| `4` | `UIStateController -> UISTM` | `run(initialEvent : UIEvent = APP_STARTED) : UITransitionResult` | 앱 시작 UI event | 초기 UI 전이 결과 | UI STM을 초기 상태에서 실행한다. | 화면, 팝업, 필터 등 UI 상태의 초기 전이를 결정한다. 외부 I/O는 UISTM이 직접 수행하지 않는다. |
| `5` | `UIStateController -> AppShellUI` | `startInterface() : void` | 없음 | `void` | AppShell을 표시하고 사용자 입력 수신을 시작한다. | 앞 단계에서 준비된 추천 REGIME, 계좌, 이력, 성과 및 trading 상태를 controller의 UI binding을 통해 표시한다. |

### 4.5 사용자 REGIME 선택

| 번호 | 호출자 -> 수신자 | Operation | Parameter | Return | 설명 | 동작 과정 |
|---|---|---|---|---|---|---|
| `6` | `User -> AppShellUI` | `selectRegime(regimeType : RegimeType) : void` | 사용자가 선택한 REGIME | `void` | 사용자가 실제 거래에 적용할 REGIME을 선택한다. | 추천 REGIME과 별개의 사용자 선택값이며 Boundary가 `6.1`로 전달한다. |
| `6.1` | `AppShellUI -> UIStateController` | `selectRegime(regimeType : RegimeType) : void` | 선택 REGIME | `void` | UI 입력을 제어 계층에 전달한다. | UIStateController는 `6.1.1`을 통해 RegimeController에 선택을 적용한다. |
| `6.1.1` | `UIStateController -> RegimeController` | `setRegimeType(regimeType : RegimeType) : void` | 선택 REGIME | `void` | 추천값이 아닌 사용자의 적용 REGIME을 설정한다. | 원본 라벨에는 괄호가 없다. RegimeController는 `6.1.1.1`로 선택값에 대응하는 거래 STM을 조회한다. |
| `6.1.1.1` | `RegimeController -> TradingController` | `fetchSelectedTradingLogic(regimeType : RegimeType) : TradingSTM` | 선택 REGIME | 선택된 TradingSTM | 선택 REGIME에 맞는 trading logic을 요청한다. | 원본 철자는 `fetchSelectedTardingLogic()`이다. `6.1.1.1.1`의 반환 인스턴스를 사용하며 별도 reply 메시지는 없다. |
| `6.1.1.1.1` | `TradingController -> TradingSTM` | `getSTMInstance(regimeType : RegimeType) : TradingSTM` | 선택 REGIME | 해당 TradingSTM 인스턴스 | 선택된 logic을 실행할 STM 인스턴스를 가져온다. | TradingController는 이 인스턴스를 이후 시작과 주문 처리에 사용한다. |

### 4.6 자동매매 시작

아래 표는 필수 누락 메시지를 삽입한 최종 numbering이다. 기존 그림의 `7.1.1.1: run()`은 `7.1.1.2`로 이동해야 한다.

| 번호 | 호출자 -> 수신자 | Operation | Parameter | Return | 설명 | 동작 과정 |
|---|---|---|---|---|---|---|
| `7` | `User -> AppShellUI` | `startConfirmed() : void` | 없음 | `void` | 사용자가 시작 확인 팝업에서 거래 시작을 확정한다. | Boundary가 `7.1`로 확인 event를 전달한다. |
| `7.1` | `AppShellUI -> UIStateController` | `startTrading() : void` | 없음 | `void` | 자동매매 시작 UI event를 전달한다. | UI 상태 전이 후 실제 trading 시작을 `7.1.1`에 위임한다. |
| `7.1.1` | `UIStateController -> TradingController` | `startTrading() : void` | 없음 | `void` | TradingController에 자동매매 시작을 요청한다. | Controller는 먼저 `7.1.1.1`에서 runtime context를 초기화하고, 그 context로 `7.1.1.2`의 STM을 실행한다. |
| `7.1.1.1` | `TradingController -> TradingContext` | `initialize(account : Account, selectedRegime : RegimeType, position : Position, scaleInRatio : Decimal, scaleOutRatio : Decimal) : void` | 계좌, 선택 REGIME, 현재 포지션, 분할 비율 | `void` | **이 부분은 diagram에서 추가되어야함.** TradingSTM이 사용할 시작 context를 초기화한다. | Start/Stop 그림에 기존 클래스인 `:TradingContext` lifeline도 함께 추가한다. `positionOwner`, pending 주문 값, `tradingPhase`, `lowerEventId` 등 runtime 값을 일관된 시작값으로 만들고 계좌·포지션·설정 참조를 연결한다. |
| `7.1.1.2` | `TradingController -> TradingSTM` | `run(context : TradingContext) : TradingSTMResult` | 초기화가 끝난 TradingContext | 초기 trading action | TradingSTM을 실행한다. | 원본 그림의 번호는 `7.1.1.1`이다. STM은 시작 가능 조건과 초기 상태를 결정하고, 반환 action은 TradingController가 함수 반환값으로 소비한다. |

### 4.7 자동매매 중지와 전량 매도

아래 표는 필수 누락 메시지를 삽입한 최종 numbering이다. 기존 그림의 `8.1.1.1`과 `8.1.1.1.1`은 각각 `8.1.1.2`와 `8.1.1.2.1`로 이동해야 한다.

| 번호 | 호출자 -> 수신자 | Operation | Parameter | Return | 설명 | 동작 과정 |
|---|---|---|---|---|---|---|
| `8` | `User -> AppShellUI` | `stopConfirmed() : void` | 없음 | `void` | 사용자가 자동매매 중지를 확정한다. | Boundary가 중지 확인 event를 `8.1`로 전달한다. |
| `8.1` | `AppShellUI -> UIStateController` | `stopTrading() : void` | 없음 | `void` | 중지 확인 event를 제어 계층에 전달한다. | UIStateController는 실제 trading 중지를 `8.1.1`에 위임한다. |
| `8.1.1` | `UIStateController -> TradingController` | `stopTrading() : void` | 없음 | `void` | 신규 진입 차단과 전량 매도를 포함한 중지 절차를 시작한다. | `8.1.1.1`에서 STM에 중지 event를 전달하고, 이어서 원본 흐름의 `8.1.1.2`를 실행한다. 포지션 유무에 따른 별도 guard는 다이어그램에 없으므로 추가하지 않는다. |
| `8.1.1.1` | `TradingController -> TradingSTM` | `handle(event : TradingEvent, context : TradingContext) : TradingSTMResult` | `event = STOP_CONFIRMED`, 현재 context | 중지 action/전이 결과 | **이 부분은 diagram에서 추가되어야함.** TradingSTM에 중지를 전달해 신규 진입을 차단하고 중지 절차를 시작한다. | 현재 그림은 매도 API만 호출해 TradingSTM에 중지 의도가 전달되지 않는다. 기존 `handle(...)` 계약을 재사용하며 최종 완료 시점을 새 메시지로 확장하거나 API 응답 메시지를 추가하지 않는다. |
| `8.1.1.2` | `TradingController -> APIGateway` | `sellAllPosition(symbol : String, quantity : Decimal) : OrderResult` | 현재 포지션의 symbol과 전량 수량 | 강제 매도 주문 결과 | 현재 포지션의 전량 매도를 요청한다. | 원본 번호는 `8.1.1.1`이다. 다이어그램에 표시된 순서대로 실행하며 포지션 유무에 따른 별도 조건 분기는 문서에서 추가하지 않는다. |
| `8.1.1.2.1` | `APIGateway -> Binance REST API` | `sellAllPosition(symbol : String, quantity : Decimal) : BinanceOrderResponse` | 보유 symbol과 전량 수량 | Binance 주문 응답 | Binance에 실제 전량 매도 주문을 제출한다. | 원본 번호는 `8.1.1.1.1`이다. 응답은 `8.1.1.2`의 반환값으로 정규화되며 답변 화살표를 별도로 표시하지 않는다. |

## 5. Case 2 - Buy and Sell

### 5.1 흐름 분기

- 매수: `1`~`10` -> `12` -> `13`~`14`; 매도 원가가 필요하지 않으므로 `11`은 생략한다.
- 매도: `1`~`10` -> `11` -> `12` -> `13`~`14`.
- 최초 주문 결과가 `FILLED`이면 `8`, `8.1`, `8.2`, `9`를 건너뛰고 `10`으로 진행한다.
- 최초 결과가 `NEW`, `PARTIALLY_FILLED`, `UNKNOWN`이면 `8` -> `8.1`/`8.2` -> `9` -> `10`으로 진행한다.
- 주문 하나의 여러 fill은 `10`에서 하나의 ExecutionSummary로 집계한다.

### 5.2 메시지 호출

| 번호 | 호출자 -> 수신자 | Operation | Parameter | Return | 설명 | 동작 과정 |
|---|---|---|---|---|---|---|
| `1` | `TradingController -> TradingSTM` | `handle(event : TradingEvent) : TradingSTMResult` | 전략/시장 조건 event | STM action 결과 | 전략 event를 STM에 전달한다. | STM이 매수 또는 매도 action을 결정한다. 원본 라벨은 `hadle(event)`이며 반환 action은 별도 응답 화살표 없이 `2`의 입력으로 사용한다. |
| `2` | `TradingController -> TradingContext` | `applyTradingSTMResult(result : TradingSTMResult) : void` | 메시지 `1`의 결과 | `void` | STM action을 runtime context에 반영한다. | 그림의 매수 예시는 `positionOwner = CASE_B`, `pendingOrderSide = BUY`, `pendingStrategy = CASE_B`, `tradingPhase = ENTRY_ORDER_PENDING`이다. 실제 보유 수량은 체결 뒤 `12`에서 반영한다. |
| `3` | `TradingController -> TradingContext` | `getSplitRatio() : Decimal` | 없음 | 현재 주문 방향의 분할 비율 | 이번 주문에 적용할 분할 비율을 조회한다. | pending side가 BUY면 scale-in 비율, SELL이면 scale-out 비율을 사용한다. |
| `4` | `TradingController -> MarketSnapshot` | `getCurrentETHPrice() : Decimal` | 없음 | 주문 결정 시점 ETH 가격 | 주문 결정을 내린 순간의 시장가격을 얻는다. | `marketPriceAtDecision`으로 Order에 저장하며 실제 `fillPrice`와 구분한다. |
| `5` | `TradingController -> Order` | `Order(symbol : String, side : OrderSide, strategy : StrategyType, requestedQuantity : Decimal, marketPriceAtDecision : Decimal) : Order` | 주문 의도와 결정 가격 | 로컬 Order | 거래소 호출 전에 어떤 주문을 제출하려는지 로컬에 기록한다. | 분할 비율과 가용 잔액/포지션으로 수량을 계산하고 초기 상태의 Order를 만든다. |
| `6` | `TradingController -> APIGateway` | `submitOrder(order : Order) : OrderResult` | 로컬 Order | 정규화된 최초 주문 결과 | 주문 의도를 Gateway에 제출한다. | `6.1`의 Binance 응답을 내부 OrderResult로 정규화한다. |
| `6.1` | `APIGateway -> Binance REST API` | `placeOrder(symbol : String, side : OrderSide, quantity : Decimal) : BinanceOrderResponse` | symbol, side, 수량 | Binance 최초 주문 응답 | Binance에 실제 주문을 생성한다. | 반환 응답은 `6`의 return으로 처리하고 별도 reply 메시지를 표시하지 않는다. |
| `7` | `TradingController -> Order` | `applyOrderResult(result : OrderResult) : void` | 최초 주문 결과 | `void` | 최초 REST 응답을 로컬 Order에 반영한다. | 거래소 주문 ID, 상태, 체결 수량·금액, 처리 시각과 fill 정보를 갱신한다. `FILLED`면 `10`으로, 미완료/불명이면 `8`로 간다. |
| `8` | `TradingController -> APIGateway` | `queryOrderResult(symbol : String, orderId : Long) : OrderResult` | symbol, 거래소 주문 ID | 재조회한 주문 결과 | `[status == NEW or PARTIALLY_FILLED or UNKNOWN]`일 때 최신 주문 결과를 조회한다. | `8.1`의 상태와 `8.2`의 실제 fill 목록을 결합하고, 그 결과를 `9`에서 Order에 다시 적용한다. |
| `8.1` | `APIGateway -> Binance REST API` | `getOrderStatus(symbol : String, orderId : Long) : BinanceOrderStatusResponse` | symbol, 주문 ID | 최신 주문 상태 | 최신 상태, 누적 체결 수량·금액, 갱신 시각을 조회한다. | `8`의 하위 호출이다. |
| `8.2` | `APIGateway -> Binance REST API` | `getAccountTrades(symbol : String, orderId : Long) : List<Fill>` | symbol, 주문 ID | 실제 fill 목록 | 해당 주문에서 발생한 실제 fill과 수수료를 조회한다. | 여러 fill은 `8`의 OrderResult에 포함되고 이후 `10`에서 합산된다. |
| `9` | `TradingController -> Order` | `reapplyOrderResult(result : OrderResult) : void` | 메시지 `8`의 재조회 결과 | `void` | 재조회한 결과를 로컬 Order에 다시 적용한다. | 원본의 `[8 실행 시]` guard에 따라 `8`이 수행된 경우에만 호출한다. |
| `10` | `TradingController -> Order` | `buildExecutionSummary() : ExecutionSummary` | 없음 | 체결 요약 | Order의 여러 fill을 하나의 주문 체결 결과로 집계한다. | 총 체결 수량, 총 체결 금액, 가중평균 `fillPrice`, 총 수수료 및 체결 시각을 계산한다. |
| `11` | `TradingController -> Position` | `getCostBasis(executedQuantity : Decimal) : Decimal` | 실제 매도 체결 수량 | 해당 수량의 취득원가 | `[매도인 경우]` 실현손익 계산에 필요한 원가를 얻는다. | 매수 case에서는 호출하지 않는다. |
| `12` | `TradingController -> Position` | `applyExecution(summary : ExecutionSummary) : void` | 체결 요약 | `void` | 실제 체결을 Position에 반영한다. | 매수면 수량과 평균 진입가를 증가시키고, 매도면 수량을 감소시키며 전량 매도 시 포지션을 닫는다. |
| `13` | `TradingController -> TradeHistoryController` | `recordOrderExecution(order : Order, summary : ExecutionSummary, costBasis : Decimal?) : void` | Order, 체결 요약, 매도 시 원가 | `void` | 완료된 주문 체결을 이력, 성과, 저장소에 기록하도록 위임한다. | `13.1`~`13.5`를 조정한다. PNG/PDF 라벨은 `recordOrderExecution()`이며 VPP 내부 모델명에만 `recordOrderExcution()` 오탈자가 있다. |
| `13.1` | `TradeHistoryController -> Performance` | `calculateRealizedResult(summary : ExecutionSummary, costBasis : Decimal) : RealizedResult` | 매도 체결 요약, 취득원가 | 이번 매도의 실현 결과 | 이번 매도의 실현손익과 수익률을 계산한다. | 노트가 명시한 매도 의미의 하위 호출이다. 매수에서는 실현 결과를 만들지 않는다. |
| `13.2` | `TradeHistoryController -> Trade` | `Trade(order : Order, summary : ExecutionSummary, realizedResult : RealizedResult?) : Trade` | Order, 체결 요약, 선택적 실현 결과 | Trade | 실제 체결 기록 entity를 만든다. | 요청 정보와 실제 fill 정보, 전략, 손익 및 청산 사유를 한 레코드에 보존한다. |
| `13.3` | `TradeHistoryController -> TradeHistory` | `addTrade(trade : Trade) : void` | 새 Trade | `void` | 새 체결을 인메모리 거래 이력에 추가한다. | 이후 최근 체결, 상세 조회, CSV 조회의 원천이 된다. |
| `13.4` | `TradeHistoryController -> Performance` | `applyNewTrade(trade : Trade) : void` | 새 Trade | `void` | 새 거래의 손익과 수수료를 누적 성과에 반영한다. | 노트의 `apply()`보다 메시지 라벨 `applyNewTrade()`를 우선한다. |
| `13.5` | `TradeHistoryController -> TradeHistoryRepository` | `saveThisTradeByOrderID(orderId : Long, trade : Trade) : void` | 주문 ID, 새 Trade | `void` | 주문 ID를 기준으로 거래를 영속화한다. | 저장할 Trade를 직렬화하고 `13.5.1`로 파일에 기록한다. |
| `13.5.1` | `TradeHistoryRepository -> Local File System` | `write(path : Path, data : String) : void` | 저장 경로, 직렬화된 Trade | `void` | 거래 이력 파일에 데이터를 쓴다. | 파일 I/O 결과는 operation 내부에서 처리하며 반환 응답 메시지를 추가하지 않는다. |
| `14` | `TradingController -> TradingSTM` | `orderFinished() : void` | 없음 | `void` | 주문 처리가 끝났음을 STM에 알린다. | TradingContext에 반영된 주문 처리 상태를 사용해 pending 주문 상태를 종료하고 다음 trading 상태로 전이한다. 원본의 빈 parameter list를 유지한다. |

TradingContext 생성/초기화는 주문 실행 이전인 Case 1의 자동매매 시작 흐름에 추가해야 하며, 이 case에 중복 추가하지 않는다.

## 6. Case 3 - Show Trade History Details

이 다이어그램은 두 흐름으로 나뉜다.

1. `1` 계열: 최근 체결 영역에서 전체 보기를 선택해 상세 화면을 처음 표시
2. `2` 계열: 상세 화면에서 기간/매수·매도 필터를 변경해 목록을 다시 표시

### 6.1 전체 거래 상세 화면 표시

| 번호 | 호출자 -> 수신자 | Operation | Parameter | Return | 설명 | 동작 과정 |
|---|---|---|---|---|---|---|
| `1` | `User -> RecentOrderUI` | `showAllTradingDetails() : void` | 없음 | `void` | 사용자가 최근 체결 패널의 `전체 보기`를 선택한다. | RecentOrderUI가 상세 화면 진입 event를 `1.1`로 전달한다. |
| `1.1` | `RecentOrderUI -> UIStateController` | `showAllTradingDetails() : void` | 없음 | `void` | 상세 화면 전환과 초기 조회를 요청한다. | `1.1.1`에서 UI 상태를 전이하고 `1.1.2`에서 표시 데이터를 만든 뒤 `1.1.3`으로 렌더링한다. |
| `1.1.1` | `UIStateController -> UISTM` | `handle(event : UIEvent = SHOW_ALL_TRADING_DETAILS) : UITransitionResult` | 상세 화면 진입 event | UI 전이 결과 | Main 화면에서 Trading Details 화면으로 상태를 전환한다. | UISTM은 화면 상태만 결정하며 조회 I/O는 수행하지 않는다. 반환 결과는 controller가 직접 사용한다. |
| `1.1.2` | `UIStateController -> TradeHistoryController` | `getTradeDetails(period : HistoryPeriod = TODAY, side : TradeSide = ALL) : TradeDetailsResult` | 기본 기간 `TODAY`, 기본 side `ALL` | 상세 표시 결과 | 첫 진입에 필요한 거래 목록, ETH 보유량, 성과를 요청한다. | `1.1.2.1`~`1.1.2.4`의 결과를 하나의 TradeDetailsResult로 묶는다. |
| `1.1.2.1` | `TradeHistoryController -> TradeHistoryQuery` | `TradeHistoryQuery(startDate : LocalDate, endDate : LocalDate, side : TradeSide = ALL) : TradeHistoryQuery` | 오늘 날짜 범위, 전체 side | 조회 조건 객체 | 화면 기본 필터를 결합한 조회 조건을 만든다. | `HistoryPeriod.TODAY`를 실제 시작일/종료일로 변환하고 side를 함께 보존한다. |
| `1.1.2.2` | `TradeHistoryController -> TradeHistory` | `find(query : TradeHistoryQuery) : List<Trade>` | 메시지 `1.1.2.1`의 query | 조건에 맞는 Trade 목록 | 인메모리 거래 이력에서 조건에 맞는 체결을 찾는다. | 조건과 일치하는 목록을 `1.1.2`의 TradeDetailsResult에 포함한다. |
| `1.1.2.3` | `TradeHistoryController -> Account` | `getHoldings(asset : String = "ETH") : Decimal` | 자산 symbol | 현재 ETH 보유 수량 | 상세 화면 요약 카드에 표시할 보유량을 조회한다. | Account의 최신 REST/WebSocket 반영값을 반환한다. 별도 reply 메시지는 없다. |
| `1.1.2.4` | `TradeHistoryController -> Performance` | `getPerformance() : Performance` | 없음 | 현재 Performance snapshot | 수익률, 매도 성과, 실현손익 및 수수료를 조회한다. | UI Rule의 당일 전체 수익률, 매도 성과, 당일 수수료 표시에 필요한 값을 제공한다. |
| `1.1.3` | `UIStateController -> TradeHistoryUI` | `displayTradeDetails(details : TradeDetailsResult) : void` | 결합된 상세 표시 결과 | `void` | 상세 화면을 렌더링한다. | 거래 테이블, 기간/side 기본 선택, ETH 보유량, 수익률, 매도 성과, 수수료를 한 번에 표시한다. 원본 수신자 이름은 `TradeHostoryUI`이다. |

### 6.2 기간 및 매수·매도 필터 변경

| 번호 | 호출자 -> 수신자 | Operation | Parameter | Return | 설명 | 동작 과정 |
|---|---|---|---|---|---|---|
| `2` | `User -> TradeHistoryUI` | `selectFilter(period : HistoryPeriod, side : TradeSide) : void` | 선택 기간과 거래 side | `void` | 사용자가 오늘/7일/30일/전체 기간 또는 전체/매수/매도 필터를 선택한다. | 두 필터의 현재 선택값을 결합해 `2.1`로 전달한다. 원본 수신자는 `TradeHostoryUI`이다. |
| `2.1` | `TradeHistoryUI -> UIStateController` | `tradeHistoryFilterChanged(period : HistoryPeriod, side : TradeSide) : void` | 결합된 기간/side | `void` | 필터 변경 event를 controller에 전달한다. | 원본 operation은 `tradeHisoryFilterChanged()`이다. `2.1.1`에서 UI 선택 상태를 갱신하고 `2.1.2`에서 데이터를 다시 조회한다. |
| `2.1.1` | `UIStateController -> UISTM` | `handle(event : UIEvent) : UITransitionResult` | 선택된 필터를 담은 UI event | UI 전이 결과 | 기간/side 필터의 UI 상태를 전이한다. | 같은 그룹에서 하나의 선택만 활성화하고 조회 조건을 확정한다. |
| `2.1.2` | `UIStateController -> TradeHistoryController` | `getTradeDetails(period : HistoryPeriod, side : TradeSide) : TradeDetailsResult` | 현재 기간과 side | 필터 결과 | 결합 조건에 맞는 거래 상세를 다시 요청한다. | `2.1.2.1`과 `2.1.2.2`의 조회 목록을 TradeDetailsResult의 `trades`로 반환하며, 호출자는 그 목록만 `2.1.3`에 전달한다. 기존 요약 정보의 불필요한 재조회는 새 메시지로 추가하지 않는다. |
| `2.1.2.1` | `TradeHistoryController -> TradeHistoryQuery` | `TradeHistoryQuery(startDate : LocalDate, endDate : LocalDate, side : TradeSide) : TradeHistoryQuery` | 선택 기간의 실제 날짜 범위와 side | 조회 조건 객체 | 기간과 side를 하나의 조회 조건으로 만든다. | `TODAY`, 최근 7일, 최근 30일, 전체 기간을 날짜 범위로 변환한다. |
| `2.1.2.2` | `TradeHistoryController -> TradeHistory` | `find(query : TradeHistoryQuery) : List<Trade>` | 필터 query | 조건에 맞는 Trade 목록 | 거래 이력에서 필터 조건과 일치하는 체결을 찾는다. | 결과가 없으면 빈 목록을 반환해 UI가 빈 상태를 표시한다. |
| `2.1.3` | `UIStateController -> TradeHistoryUI` | `displayFilteredTrades(trades : List<Trade>) : void` | `2.1.2` 반환값의 `trades` | `void` | 필터링된 거래 행을 화면에 반영한다. | UIStateController가 `TradeDetailsResult.trades`를 추출해 기존 테이블을 결과 목록으로 교체하고, 빈 목록이면 empty state를 표시한다. 원본 수신자 이름은 `TradeHostoryUI`이다. |

조회 결과, Account 보유량 및 Performance는 동기 operation의 반환값으로 결합할 수 있으므로 반환용 reply 메시지를 추가하지 않는다.

## 7. Case 4 - CSV Export

이 다이어그램의 순서는 다음과 같다.

1. `1` 계열: CSV 팝업 열기와 기본 option 생성
2. `2` 계열: 저장 위치 선택
3. `3` 계열: 기간, 날짜, 파일명 option 변경
4. `4` 계열: 검증, 거래 조회, CSV 생성, 성공/실패 표시

CSV PNG의 진입 Boundary 이름은 `TradingHistoryUI`이지만 History PNG의 `TradeHostoryUI`와 같은 거래 내역 상세 Boundary이므로 구현 클래스 목록에서는 `TradeHistoryUI`로 통합한다.

### 7.1 CSV 팝업 열기

| 번호 | 호출자 -> 수신자 | Operation | Parameter | Return | 설명 | 동작 과정 |
|---|---|---|---|---|---|---|
| `1` | `User -> TradeHistoryUI` | `clickCSVExport() : void` | 없음 | `void` | 사용자가 거래 내역 상세 화면의 CSV 내보내기 버튼을 누른다. | 원본 수신자 이름은 `TradingHistoryUI`이다. Boundary가 `1.1`로 팝업 열기 event를 전달한다. |
| `1.1` | `TradeHistoryUI -> UIStateController` | `openCSVExport() : void` | 없음 | `void` | CSV option 팝업을 열도록 요청한다. | `1.1.1`에서 UI 상태를 전이하고, `1.1.2`로 임시 option을 만든 뒤 `1.1.3`으로 표시한다. |
| `1.1.1` | `UIStateController -> UISTM` | `handle(event : UIEvent = CSV_EXPORT_CLICKED) : UITransitionResult` | CSV 팝업 열기 event | UI 전이 결과 | UI 상태를 `CSV_EXPORT_POPUP_DISPLAYED`로 전이한다. | UISTM은 팝업 표시 action만 결정하고 파일 I/O는 수행하지 않는다. |
| `1.1.2` | `UIStateController -> CSVExportOptions` | `CSVExportOptions(period : CSVPeriod = TODAY, startDate : LocalDate = today, endDate : LocalDate = today, fileName : String = defaultFileName) : CSVExportOptions` | 기본 기간, 날짜, 파일명 | 임시 option 객체 | 팝업에서 수정할 CSV option을 생성한다. | 저장 위치는 아직 미선택이며, UI 문서의 기본 기간/기본 파일명 규칙을 적용한다. |
| `1.1.3` | `UIStateController -> csvPopup : PopupUI` | `showCSVExportOptions(options : CSVExportOptions) : void` | 기본 option | `void` | CSV option 팝업을 표시한다. | 저장 위치, 기간 preset, 시작일/종료일, 파일명, 취소/내보내기 controls를 렌더링한다. |

### 7.2 저장 위치 선택

| 번호 | 호출자 -> 수신자 | Operation | Parameter | Return | 설명 | 동작 과정 |
|---|---|---|---|---|---|---|
| `2` | `User -> csvPopup : PopupUI` | `selectSaveLocation() : void` | 없음 | `void` | 사용자가 `위치 선택`을 누른다. | 노트에 따라 이 operation은 저장 경로 선택만 담당하며 기간/파일명은 변경하지 않는다. |
| `2.1` | `csvPopup : PopupUI -> UIStateController` | `saveLocationSelectionRequested() : void` | 없음 | `void` | OS 디렉터리 선택 창 열기를 요청한다. | `2.1.1`에서 UI 상태를 바꾸고 `2.1.2`에서 picker를 실행한다. |
| `2.1.1` | `UIStateController -> UISTM` | `handle(event : UIEvent = SAVE_LOCATION_SELECT_CLICKED) : UITransitionResult` | 위치 선택 event | UI 전이 결과 | 파일 browser open 상태를 처리한다. | 선택 완료와 취소는 `Path?` 반환값으로 구분한다. |
| `2.1.2` | `UIStateController -> CSVFileGateway` | `chooseDirectory() : Path?` | 없음 | 선택 경로 또는 취소 시 `null` | OS 디렉터리 선택을 Gateway에 위임한다. | `2.1.2.1`의 반환 경로를 그대로 사용한다. 반환 화살표는 추가하지 않는다. |
| `2.1.2.1` | `CSVFileGateway -> File System` | `openDirectoryPicker() : Path?` | 없음 | 선택 경로 또는 `null` | OS 폴더 선택 창을 연다. | 사용자가 취소하면 `null`을 반환하고 기존 저장 경로를 유지한다. |
| `2.1.3` | `UIStateController -> CSVExportOptions` | `setSaveLocation(path : Path) : void` | 선택된 경로 | `void` | option의 저장 위치를 갱신한다. | 원본 라벨은 괄호 없는 `setSaveLocation`이다. picker 취소 시 이 메시지를 실행하지 않는다. |
| `2.1.4` | `UIStateController -> csvPopup : PopupUI` | `displaySaveLocation(path : Path) : void` | 선택된 non-null 경로 | `void` | 선택 경로를 팝업에 표시한다. | `2.1.2`가 non-null Path를 반환한 경우에만 `선택된 폴더` 영역을 갱신한다. picker 취소 시 `2.1.3`과 함께 생략하고 기존 표시를 유지한다. |

### 7.3 기간, 날짜 및 파일명 변경

| 번호 | 호출자 -> 수신자 | Operation | Parameter | Return | 설명 | 동작 과정 |
|---|---|---|---|---|---|---|
| `3` | `User -> csvPopup : PopupUI` | `changeCSVOptions(period : CSVPeriod, startDate : LocalDate?, endDate : LocalDate?, fileName : String) : void` | 변경된 기간/날짜/파일명 | `void` | 사용자가 내보낼 범위 또는 파일명을 변경한다. | nullable 날짜는 이번 UI event에서 해당 날짜가 변경되지 않았음을 뜻한다. 다이어그램 노트에 따라 저장 경로는 이 operation의 책임이 아니다. |
| `3.1` | `csvPopup : PopupUI -> UIStateController` | `csvOptionChanged(period : CSVPeriod, startDate : LocalDate?, endDate : LocalDate?, fileName : String) : void` | 현재 입력값 | `void` | option 변경 event를 controller에 전달한다. | nullable 날짜는 기존 값을 유지하는 patch 의미다. `3.1.1`에서 UI 상태/guard를 처리하고 `3.1.2`에서 임시 option에 적용한다. |
| `3.1.1` | `UIStateController -> UISTM` | `handle(event : UIEvent) : UITransitionResult` | 기간/날짜/파일명 변경 event | UI 전이 결과 | 기간 preset, 직접 날짜 선택 및 파일명 입력 상태를 전이한다. | 시작일이 종료일보다 늦은 경우 등 UI guard 결과를 반환한다. |
| `3.1.2` | `UIStateController -> CSVExportOptions` | `apply(period : CSVPeriod, startDate : LocalDate?, endDate : LocalDate?, fileName : String) : void` | 검증 가능한 현재 입력값 | `void` | 임시 CSV option을 갱신한다. | `TODAY`는 오늘, `WEEKLY`는 오늘 포함 7일, `MONTHLY`는 오늘 포함 30일로 non-null 날짜를 맞춘다. `CUSTOM`에서는 전달된 날짜만 바꾸고 null인 날짜는 기존 non-null Attribute 값을 유지한다. |
| `3.1.3` | `UIStateController -> csvPopup : PopupUI` | `refresh(options : CSVExportOptions) : void` | 갱신된 option | `void` | 팝업 표시를 option과 동기화한다. | 선택된 preset, 날짜 field 활성 상태, 파일명 preview와 validation 표시를 갱신한다. |

### 7.4 검증, 조회 및 파일 생성

| 번호 | 호출자 -> 수신자 | Operation | Parameter | Return | 설명 | 동작 과정 |
|---|---|---|---|---|---|---|
| `4` | `User -> csvPopup : PopupUI` | `exportConfirmed() : void` | 없음 | `void` | 사용자가 `내보내기`를 확정한다. | PopupUI가 `4.1`로 현재 option의 내보내기를 요청한다. |
| `4.1` | `csvPopup : PopupUI -> UIStateController` | `exportCSV() : void` | 없음 | `void` | CSV 검증 및 생성을 시작한다. | `4.1.1` 검증 결과에 따라 실패면 `4.1.3a`, 성공이면 `4.1.3b`와 `4.1.4`로 진행한다. |
| `4.1.1` | `UIStateController -> CSVExportOptions` | `validate() : ValidationResult` | 없음 | 검증 결과 | 저장 위치, 파일명, 기간/날짜 범위를 검증한다. | 저장 위치 미선택, 빈/금지 문자를 가진 파일명, 시작일이 종료일보다 늦은 범위를 오류로 반환한다. |
| `4.1.2` | `UIStateController -> UISTM` | `handle(event : UIEvent) : UITransitionResult` | validation 성공 또는 실패 event | UI 전이 결과 | 검증 결과에 맞는 UI 상태를 결정한다. | 실패면 popup 유지, 성공이면 export 진행 상태로 전이한다. |
| `4.1.3a` | `UIStateController -> csvPopup : PopupUI` | `showValidationError(result : ValidationResult) : void` | 실패 항목과 사유 | `void` | `[실패 시]` 유효하지 않은 입력을 표시한다. | 잘못된 control을 강조하고 오류 원인을 표시한 뒤 이 branch를 종료한다. |
| `4.1.3b` | `UIStateController -> csvPopup : PopupUI` | `showExportInProgress() : void` | 없음 | `void` | `[성공 시]` CSV 생성 중 상태를 표시한다. | 이후 `4.1.4`의 service 작업이 완료될 때까지 중복 내보내기를 막는다. |
| `4.1.4` | `UIStateController -> TradeHistoryController` | `exportCSV(options : CSVExportOptions) : CSVExportResult` | 검증된 option 객체 | 성공/실패 및 경로/사유 | 실제 거래 조회와 CSV 생성을 요청한다. | 노트대로 option 객체를 인자로 전달하고 성공 또는 실패 이유를 반환받는다. `4.1.4.1`~`4.1.4.3`을 조정한다. |
| `4.1.4.1` | `TradeHistoryController -> TradeHistoryQuery` | `TradeHistoryQuery(startDate : LocalDate, endDate : LocalDate, side : TradeSide = ALL) : TradeHistoryQuery` | option에서 추출한 날짜 범위, 전체 side | 조회 조건 객체 | CSVExportOptions에서 거래 조회에 필요한 조건을 추출한다. | CSV 그림에는 side 선택이 없으므로 전체 매수/매도를 대상으로 한다. |
| `4.1.4.2` | `TradeHistoryController -> TradeHistoryRepository` | `streamTrades(query : TradeHistoryQuery) : Stream<Trade>` | CSV 조회 query | Trade stream | 조건에 맞는 저장 이력을 streaming 방식으로 읽는다. | 전체 파일을 메모리에 복제하지 않고 `4.1.4.2.1`의 read stream을 query로 필터링한다. |
| `4.1.4.2.1` | `TradeHistoryRepository -> File System` | `openReadStream(path : Path) : InputStream` | 거래 이력 저장 경로 | 입력 stream | 로컬 거래 이력 파일의 읽기 stream을 연다. | stream의 수명과 close 처리는 repository operation 내부에서 보장하며 별도 메시지를 추가하지 않는다. |
| `4.1.4.3` | `TradeHistoryController -> CSVFileGateway` | `writeCSV(trades : Stream<Trade>, options : CSVExportOptions) : CSVExportResult` | Trade stream, 출력 option | CSV 생성 결과 | 조회 결과를 사용자가 지정한 CSV 파일로 쓴다. | Gateway가 Trade를 CSV header/row의 `Stream<String>`으로 직렬화한 뒤 `4.1.4.3.1`에 파일 생성을 위임한다. |
| `4.1.4.3.1` | `CSVFileGateway -> File System` | `createCustomizedCSV(path : Path, fileName : String, rows : Stream<String>) : CSVExportResult` | 저장 위치, 파일명, 직렬화된 CSV 행 stream | 생성 결과 | 지정 경로에 직렬화된 CSV 데이터를 기록해 파일을 생성한다. | 성공 시 생성 경로, 실패 시 I/O 사유를 반환한다. 반환 reply 메시지는 그리지 않는다. |
| `4.1.5` | `UIStateController -> UISTM` | `handle(event : UIEvent) : UITransitionResult` | `CSV_EXPORT_SUCCEEDED` 또는 `CSV_EXPORT_FAILED` | UI 전이 결과 | service 결과에 따라 완료 또는 오류 상태로 전이한다. | `4.1.4`의 CSVExportResult를 event로 변환한다. |
| `4.1.6a` | `UIStateController -> csvPopup : PopupUI` | `showExportError(reason : String) : void` | 실패 사유 | `void` | `[실패 시]` 내보내기 오류를 표시한다. | popup과 option을 유지해 사용자가 수정 후 다시 시도할 수 있게 한다. |
| `4.1.6b` | `UIStateController -> csvPopup : PopupUI` | `showExportComplete(path : Path) : void` | 생성된 CSV 경로 | `void` | `[성공 시]` 내보내기 완료 상태를 표시한다. | 진행 표시를 제거하고 저장 결과를 사용자에게 알린다. |



## 8. Diagram에서 추가되어야 하는 필수 메시지 요약

새 기능 제안이 아니라 현재 collaboration과 이미 등장한 클래스를 실제로 동작시키기 위한 누락만 정리하면 다음 두 건이다.

| Case | 최종 번호 | 추가 메시지 | 필요한 이유 | 기존 번호 변경 |
|---|---|---|---|---|
| 자동매매 시작 | `7.1.1.1` | `TradingController -> TradingContext: initialize(...) : void` | TradingSTM 실행 전에 계좌, 선택 REGIME, 포지션, 분할 비율 및 runtime 상태를 가진 TradingContext가 필요하다. Start/Stop 그림에는 기존 클래스 `:TradingContext` lifeline도 함께 추가해야 한다. | 기존 `7.1.1.1 run()` -> `7.1.1.2` |
| 자동매매 중지 | `8.1.1.1` | `TradingController -> TradingSTM: handle(event : TradingEvent, context : TradingContext) : TradingSTMResult` (`event = STOP_CONFIRMED`) | 매도 API 호출만으로는 TradingSTM에 신규 진입 차단과 중지 절차 시작 의도가 전달되지 않는다. | 기존 `8.1.1.1` -> `8.1.1.2`, 기존 `8.1.1.1.1` -> `8.1.1.2.1` |

다음은 추가 대상으로 보지 않는다.

- API 또는 파일 시스템의 함수 반환을 나타내는 reply 메시지
- Buy and Sell의 별도 WebSocket 체결 처리 경로
- CSV stream의 별도 close 메시지
- 필터 변경 때 변하지 않은 요약 정보를 다시 조회하는 메시지
- 현재 문서에 없는 신규 기능, 재시도 정책 또는 별도 상태 머신

## 9. 전체 클래스 Attribute 및 Operation

이 절은 네 다이어그램에 등장한 모든 시스템 클래스를 한 번씩 정리한다. `TradeHostoryUI`와 `TradingHistoryUI`는 기능상 같은 Boundary이므로 `TradeHistoryUI` 아래에 원본 alias를 함께 적었다. `User`, `Binance REST API`, `Binance WebSocket`, `Local File System`/`File System`은 외부 Actor이므로 클래스 목록과 분리해 10절에 정리한다.

Operation 목록은 다이어그램에서 실제로 수신하는 메시지와 8절에서 필수로 확정한 두 메시지를 기준으로 한다. 다이어그램에 없는 새로운 public operation은 추가하지 않는다.

### 9.1 AppShellUI

기능: 앱의 전역 UI Boundary로서 interface를 시작하고 REGIME 선택, 자동매매 시작 확인, 중지 확인을 받는다.

Attribute

- `uiStateController : UIStateController {not null}`
- `selectedRegime : RegimeType? = null`
- `tradingStatus : TradingStatus = STOPPED {not null}`

Operation

- `startInterface() : void`
- `selectRegime(regimeType : RegimeType) : void`
- `startConfirmed() : void`
- `stopConfirmed() : void`

### 9.2 UIStateController

기능: UI Boundary의 event를 UISTM과 업무 Controller에 전달하고, 선택된 UI action을 화면에 반영하는 façade이다.

Attribute

- `uiSTM : UISTM {not null}`
- `appShellUI : AppShellUI {not null}`
- `marketDataController : MarketDataController {not null}`
- `tradingController : TradingController {not null}`
- `tradeHistoryController : TradeHistoryController {not null}`
- `recentOrderUI : RecentOrderUI {not null}`
- `tradeHistoryUI : TradeHistoryUI {not null}`
- `csvPopup : PopupUI {not null}`
- `csvFileGateway : CSVFileGateway {not null}`
- `csvExportOptions : CSVExportOptions? = null`

Operation

- `selectRegime(regimeType : RegimeType) : void`
- `startTrading() : void`
- `stopTrading() : void`
- `showAllTradingDetails() : void`
- `tradeHistoryFilterChanged(period : HistoryPeriod, side : TradeSide) : void`
- `openCSVExport() : void`
- `saveLocationSelectionRequested() : void`
- `csvOptionChanged(period : CSVPeriod, startDate : LocalDate?, endDate : LocalDate?, fileName : String) : void`
- `exportCSV() : void`

### 9.3 UISTM

기능: UI Event-Action Table에 따라 화면, popup, filter 및 export 표시 상태를 결정한다.

Attribute

- `currentState : UIState {not null}`

Operation

- `run(initialEvent : UIEvent = APP_STARTED) : UITransitionResult`
- `handle(event : UIEvent) : UITransitionResult`

### 9.4 TradingController

기능: 계좌 로드, 선택 전략 연결, 자동매매 시작·중지 및 주문 실행을 조정한다. 거래 상태 결정은 TradingSTM에 위임한다.

Attribute

- `tradingSTM : TradingSTM {not null}`
- `context : TradingContext {not null}`
- `account : Account {not null}`
- `position : Position {not null}`
- `marketSnapshot : MarketSnapshot {not null}`
- `apiGateway : APIGateway {not null}`
- `webSocketGateway : WebSocketGateway {not null}`
- `tradeHistoryController : TradeHistoryController {not null}`
- `selectedRegime : RegimeType? = null`

Operation

- `loadAccount(asset : String = "ETH") : Account`
- `fetchSelectedTradingLogic(regimeType : RegimeType) : TradingSTM`
- `startTrading() : void`
- `stopTrading() : void`

### 9.5 TradingSTM

기능: trading event와 TradingContext를 사용해 자동매매 상태 및 수행 action을 결정한다.

Attribute

- `currentState : TradingState {not null}`
- `regimeType : RegimeType {not null}`

Operation

- `getSTMInstance(regimeType : RegimeType) : TradingSTM`
- `run(context : TradingContext) : TradingSTMResult`
- `handle(event : TradingEvent) : TradingSTMResult`
- `handle(event : TradingEvent, context : TradingContext) : TradingSTMResult`
- `orderFinished() : void`

### 9.6 TradingContext

기능: TradingSTM의 판정과 주문 실행에 필요한 runtime 값, 선택 REGIME, 분할 비율 및 현재 주문 의도를 보존한다.

Attribute

- `account : Account {not null}`
- `position : Position {not null}`
- `selectedRegime : RegimeType {not null}`
- `lowerEventId : String? = null`
- `positionOwner : PositionOwner? = null`
- `pendingOrderSide : OrderSide? = null`
- `pendingStrategy : StrategyType? = null`
- `tradingPhase : TradingPhase = STOPPED {not null}`
- `scaleInRatio : Decimal {not null}`
- `scaleOutRatio : Decimal {not null}`

Operation

- `initialize(account : Account, selectedRegime : RegimeType, position : Position, scaleInRatio : Decimal, scaleOutRatio : Decimal) : void`
- `applyTradingSTMResult(result : TradingSTMResult) : void`
- `getSplitRatio() : Decimal`

### 9.7 MarketDataController

기능: REST 과거 봉과 초기화 중 WebSocket buffer를 병합해 일관된 MarketSnapshot을 만들고 4H REGIME 계산을 시작한다.

Attribute

- `apiGateway : APIGateway {not null}`
- `webSocketGateway : WebSocketGateway {not null}`
- `marketSnapshot : MarketSnapshot {not null}`
- `regimeController : RegimeController {not null}`
- `symbol : String = "ETHUSDT" {not null}`
- `intervals : Set<Interval> {not null}`

Operation

- `InitializeMarketData(symbol : String = "ETHUSDT") : MarketSnapshot`

### 9.8 APIGateway

기능: Binance REST API의 Kline, 계좌 및 주문 기능을 내부 타입으로 캡슐화한다.

Attribute

- `restClient : BinanceRESTClient {not null}`
- `symbol : String = "ETHUSDT" {not null}`

Operation

- `loadAllKlines(symbol : String, limit : int) : Map<Interval, List<Kline>>`
- `fetchAccountSnapshot(asset : String = "ETH") : AccountSnapshot`
- `sellAllPosition(symbol : String, quantity : Decimal) : OrderResult`
- `submitOrder(order : Order) : OrderResult`
- `queryOrderResult(symbol : String, orderId : Long) : OrderResult`

### 9.9 WebSocketGateway

기능: Binance WebSocket 구독을 만들고 Kline 및 account event를 내부 형식으로 정규화한다.

Buy and Sell PNG의 동일 클래스 표기는 `WebsocketGateway`이다.

Attribute

- `webSocketClient : BinanceWebSocketClient {not null}`
- `klineBuffer : Map<Interval, List<Kline>> = {} {not null}`
- `klineSubscription : Subscription? = null`
- `accountSubscription : Subscription? = null`

Operation

- `startAllKlineBuffering(symbol : String, intervals : Set<Interval>) : Subscription`
- `startAccountInfoStream() : Subscription`

### 9.10 MarketSnapshot

기능: 동일 평가 시점의 시간대별 Kline과 현재 ETH 가격을 보존한다.

Attribute

- `symbol : String = "ETHUSDT" {not null}`
- `klinesByInterval : Map<Interval, List<Kline>> = {} {not null}`
- `currentETHPrice : Decimal {not null}`
- `updatedAt : Instant {not null}`

Operation

- `update(klines : Map<Interval, List<Kline>>) : void`
- `getCurrentETHPrice() : Decimal`

### 9.11 RegimeController

기능: MarketSnapshot에서 4H 판정 입력을 만들고 RegimeSTM을 실행하며 사용자 선택 REGIME을 거래 logic에 연결한다.

Attribute

- `marketSnapshot : MarketSnapshot {not null}`
- `indicatorSnapshot : IndicatorSnapshot {not null}`
- `regimeSTM : RegimeSTM {not null}`
- `tradingController : TradingController {not null}`
- `recommendedRegime : RegimeType? = null`
- `selectedRegime : RegimeType? = null`

Operation

- `calculate4HIndicators(snapshot : MarketSnapshot) : IndicatorSnapshot`
- `recommendRegime(indicators : IndicatorSnapshot) : RegimeType`
- `setRegimeType(regimeType : RegimeType) : void`

### 9.12 IndicatorSnapshot

기능: 동일 평가 시점의 REGIME 판정용 4H 지표를 보존한다.

Attribute

- `timeframe : Interval = FOUR_HOURS {not null}`
- `ema9Series : List<Decimal> = [] {not null}`
- `ema9Slope : Decimal {not null}`
- `swingStructure : SwingStructure {not null}`
- `liveEma9 : Decimal {not null}`
- `calculatedAt : Instant {not null}`

Operation

- `update(ema9Series : List<Decimal>, ema9Slope : Decimal, swingStructure : SwingStructure, liveEma9 : Decimal) : void`

### 9.13 RegimeSTM

기능: 4H 지표와 REGIME Event-Action 규칙으로 추천 REGIME을 결정한다.

Attribute

- `currentState : RegimeState {not null}`

Operation

- `run(indicators : IndicatorSnapshot) : RegimeType`

### 9.14 Order

기능: 실제 거래소 주문 전 로컬 주문 의도와 Binance 주문/fill 결과를 하나의 aggregate로 보존한다.

Attribute

- `symbol : String {not null}`
- `side : OrderSide {not null}`
- `strategy : StrategyType {not null}`
- `requestedQuantity : Decimal {not null}`
- `marketPriceAtDecision : Decimal {not null}`
- `exchangeOrderId : Long? = null`
- `status : OrderStatus? = null`
- `filledQuantity : Decimal = 0 {not null}`
- `filledAmount : Decimal = 0 {not null}`
- `fillPrice : Decimal? = null`
- `processedAt : Instant? = null`
- `fills : List<Fill> = [] {not null}`
- `failureReason : String? = null`

Operation

- `Order(symbol : String, side : OrderSide, strategy : StrategyType, requestedQuantity : Decimal, marketPriceAtDecision : Decimal) : Order`
- `applyOrderResult(result : OrderResult) : void`
- `reapplyOrderResult(result : OrderResult) : void`
- `buildExecutionSummary() : ExecutionSummary`

### 9.15 Position

기능: 현재 전략 소유 포지션의 수량, 평균 진입가, 취득원가 및 상태를 보존한다.

Attribute

- `symbol : String = "ETHUSDT" {not null}`
- `owner : PositionOwner? = null`
- `quantity : Decimal = 0 {not null}`
- `averageEntryPrice : Decimal = 0 {not null}`
- `costBasis : Decimal = 0 {not null}`
- `enteredAt : Instant? = null`
- `status : PositionStatus = CLOSED {not null}`
- `exitReason : ExitReason? = null`

Operation

- `getCostBasis(executedQuantity : Decimal) : Decimal`
- `applyExecution(summary : ExecutionSummary) : void`

### 9.16 Trade

기능: 실제 체결 결과를 조회와 영속화에 적합한 단일 거래 기록으로 보존한다.

Attribute

- `orderId : Long {not null}`
- `executedAt : Instant {not null}`
- `side : OrderSide {not null}`
- `fillPrice : Decimal {not null}`
- `quantity : Decimal {not null}`
- `amount : Decimal {not null}`
- `fee : Decimal {not null}`
- `strategy : StrategyType {not null}`
- `marketPriceAtDecision : Decimal {not null}`
- `realizedPnl : Decimal? = null`
- `realizedReturnRate : Decimal? = null`
- `exitReason : ExitReason? = null`

Operation

- `Trade(order : Order, summary : ExecutionSummary, realizedResult : RealizedResult?) : Trade`

### 9.17 TradeHistory

기능: 인메모리 Trade 목록을 보관하고 조건 조회의 원천이 된다.

Attribute

- `trades : List<Trade> = [] {not null}`

Operation

- `TradeHistory(trades : List<Trade>) : TradeHistory`
- `addTrade(trade : Trade) : void`
- `find(query : TradeHistoryQuery) : List<Trade>`

### 9.18 Performance

기능: 거래 이력으로부터 당일/누적 수익률, 실현손익, 수수료 및 매도 성과를 계산하고 보존한다.

Attribute

- `dailyReturnRate : Decimal = 0 {not null}`
- `cumulativeReturnRate : Decimal = 0 {not null}`
- `realizedPnl : Decimal = 0 {not null}`
- `dailyFee : Decimal = 0 {not null}`
- `totalFee : Decimal = 0 {not null}`
- `averageSellReturnRate : Decimal = 0 {not null}`
- `totalProfit : Decimal = 0 {not null}`
- `winningSellCount : int = 0 {not null}`
- `losingSellCount : int = 0 {not null}`

Operation

- `Performance(trades : List<Trade>) : Performance`
- `calculateRealizedResult(summary : ExecutionSummary, costBasis : Decimal) : RealizedResult`
- `applyNewTrade(trade : Trade) : void`
- `getPerformance() : Performance`

### 9.19 TradeHistoryController

기능: 거래 저장, 상세 조회, 성과 계산, repository 접근 및 CSV 내보내기를 조정한다.

Attribute

- `tradeHistory : TradeHistory {not null}`
- `performance : Performance {not null}`
- `account : Account {not null}`
- `repository : TradeHistoryRepository {not null}`
- `csvFileGateway : CSVFileGateway {not null}`

Operation

- `loadTradeHistory() : TradeHistory`
- `recordOrderExecution(order : Order, summary : ExecutionSummary, costBasis : Decimal?) : void`
- `getTradeDetails(period : HistoryPeriod = TODAY, side : TradeSide = ALL) : TradeDetailsResult`
- `exportCSV(options : CSVExportOptions) : CSVExportResult`

### 9.20 TradeHistoryRepository

기능: 로컬 파일에 저장된 Trade의 전체 읽기, 단건 저장 및 streaming 조회를 담당한다.

Attribute

- `storagePath : Path {not null}`
- `fileSystem : FileSystem {not null}`

Operation

- `getTradeHistory() : List<Trade>`
- `saveThisTradeByOrderID(orderId : Long, trade : Trade) : void`
- `streamTrades(query : TradeHistoryQuery) : Stream<Trade>`

### 9.21 Account

기능: 자산별 잔액, ETH 보유량, 현재가와 평가금액을 보존한다.

Attribute

- `balances : Map<String, Decimal> = {} {not null}`
- `currentPrice : Decimal = 0 {not null}`
- `valuation : Decimal = 0 {not null}`
- `updatedAt : Instant {not null}`

Operation

- `getHoldings(asset : String = "ETH") : Decimal`

### 9.22 RecentOrderUI

기능: 메인 화면의 최근 체결 목록과 거래 상세 화면 진입점을 제공한다.

Attribute

- `uiStateController : UIStateController {not null}`
- `recentTrades : List<Trade> = [] {not null}`

Operation

- `showAllTradingDetails() : void`

### 9.23 TradeHistoryUI

기능: 거래 내역 상세, 요약, 기간/side filter 및 CSV 내보내기 진입점을 표시한다.

원본 alias:

- History PNG: `TradeHostoryUI`
- CSV PNG: `TradingHistoryUI`

Attribute

- `uiStateController : UIStateController {not null}`
- `selectedPeriod : HistoryPeriod = TODAY {not null}`
- `selectedSide : TradeSide = ALL {not null}`
- `displayedTrades : List<Trade> = [] {not null}`
- `details : TradeDetailsResult? = null`

Operation

- `selectFilter(period : HistoryPeriod, side : TradeSide) : void`
- `displayTradeDetails(details : TradeDetailsResult) : void`
- `displayFilteredTrades(trades : List<Trade>) : void`
- `clickCSVExport() : void`

### 9.24 TradeHistoryQuery

기능: 기간과 거래 side를 결합한 불변 조회 조건을 표현한다.

Attribute

- `startDate : LocalDate {not null}`
- `endDate : LocalDate {not null}`
- `side : TradeSide = ALL {not null}`

Operation

- `TradeHistoryQuery(startDate : LocalDate, endDate : LocalDate, side : TradeSide = ALL) : TradeHistoryQuery`

### 9.25 PopupUI

기능: 역할명 `csvPopup`으로 CSV option 입력, validation 오류, 진행 상태 및 완료/실패를 표시한다.

Attribute

- `options : CSVExportOptions {not null}`
- `isVisible : bool = false {not null}`
- `validationError : String? = null`
- `exportStatus : CSVExportStatus = IDLE {not null}`

Operation

- `selectSaveLocation() : void`
- `changeCSVOptions(period : CSVPeriod, startDate : LocalDate?, endDate : LocalDate?, fileName : String) : void`
- `exportConfirmed() : void`
- `showCSVExportOptions(options : CSVExportOptions) : void`
- `displaySaveLocation(path : Path) : void`
- `refresh(options : CSVExportOptions) : void`
- `showValidationError(result : ValidationResult) : void`
- `showExportInProgress() : void`
- `showExportError(reason : String) : void`
- `showExportComplete(path : Path) : void`

### 9.26 CSVExportOptions

기능: CSV 저장 위치, 기간, 시작일/종료일 및 파일명을 하나의 임시 option 객체로 보존하고 검증한다.

Attribute

- `saveLocation : Path? = null`
- `period : CSVPeriod = TODAY {not null}`
- `startDate : LocalDate {not null}`
- `endDate : LocalDate {not null}`
- `fileName : String {not null}`

Operation

- `CSVExportOptions(period : CSVPeriod = TODAY, startDate : LocalDate = today, endDate : LocalDate = today, fileName : String = defaultFileName) : CSVExportOptions`
- `setSaveLocation(path : Path) : void`
- `apply(period : CSVPeriod, startDate : LocalDate?, endDate : LocalDate?, fileName : String) : void`
- `validate() : ValidationResult`

### 9.27 CSVFileGateway

기능: OS directory picker와 CSV 파일 쓰기를 UI/Controller에서 분리한다.

Attribute

- `fileSystem : FileSystem {not null}`

Operation

- `chooseDirectory() : Path?`
- `writeCSV(trades : Stream<Trade>, options : CSVExportOptions) : CSVExportResult`

## 10. 외부 Actor의 호출 계약

외부 Actor는 시스템 클래스의 Attribute/Operation 목록 대상은 아니지만, 다이어그램에서 호출되는 계약을 구현 참고용으로 정리한다.

### 10.1 Binance REST API

- `get1mKlines(symbol : String, limit : int) : List<Kline>`
- `get30mKlines(symbol : String, limit : int) : List<Kline>`
- `get4hKlines(symbol : String, limit : int) : List<Kline>`
- `get1dKlines(symbol : String, limit : int) : List<Kline>`
- `getAccount() : BinanceAccountResponse`
- `placeOrder(symbol : String, side : OrderSide, quantity : Decimal) : BinanceOrderResponse`
- `getOrderStatus(symbol : String, orderId : Long) : BinanceOrderStatusResponse`
- `getAccountTrades(symbol : String, orderId : Long) : List<Fill>`
- `sellAllPosition(symbol : String, quantity : Decimal) : BinanceOrderResponse`

### 10.2 Binance WebSocket

- `subscribeAllKlineStreams(symbol : String, intervals : Set<Interval>) : Subscription`
- `subscribeAccountInfo() : Subscription`

### 10.3 Local File System / File System

- `read(path : Path) : String`
- `write(path : Path, data : String) : void`
- `openDirectoryPicker() : Path?`
- `openReadStream(path : Path) : InputStream`
- `createCustomizedCSV(path : Path, fileName : String, rows : Stream<String>) : CSVExportResult`