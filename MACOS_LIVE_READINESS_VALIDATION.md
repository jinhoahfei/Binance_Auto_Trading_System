# Session 7 macOS live-readiness 검증

기준일: 2026-09-08 (Asia/Seoul). 시작 HEAD:
`baeabcd031c69b6e8448946c2d7511f0baf5cfab`. 시작 작업 트리는 clean이었고 이번 변경은
아직 commit하지 않았다. 이 문서는 §16.20.10의 **macOS 범위** 증거이며 Windows native 실행이나
Session 8 실제 주문의 완료 증거가 아니다.

## 현재 판정

2026-09-09 사용자 순서 변경: macOS pilot 검증 완료 후 Windows live-readiness와 pilot을 순차
진행한다. Windows는 macOS 시작의 선행 조건이 아니며, macOS 계좌 전용 사용 확인과 별도 실제
주문 승인은 유지한다. 아래 이전 양 OS 선행 차단 판정은 이 결정으로 대체한다.

**macOS 읽기 전용·ETH 잔여·BNB 수수료 회계 구현 완료 / 운영·양 OS 조건 때문에 Session 8 실제 주문 NO_GO.**
2026-09-09 BNB 납부 유지 확장 후 signed preflight는 수수료·평가 근거·잔액 모두 PASS다.
[BNB_FEE_ACCOUNTING.md](BNB_FEE_ACCOUNTING.md)의 평가 정책과 새 패키지를 사용한다.
아래 과거 수수료 차단·OFF 안내는 당시 기록이며 현재 요구사항이 아니다. 실제 주문은 0회다.

## 범위와 책임

로드맵 §1, `CODING_CONVENTIONS.md`, Communication Case 1 `1`~`5`, `7.1.1`,
`8.1.1`~`8.1.1.3`, Case 2 주문 Gateway·durable history·reconciliation 계약을 확인했다.
새 업무 Controller/STM/Communication Operation은 만들지 않았다. 추가한 타입은 configuration DTO,
native profile, protocol/permission adapter와 test helper다. 함수·클래스 docstring, 논리 블록 주석과
문장 주석을 함께 작성했다.

- `bootstrap/live.py`: Testnet을 호출하지 않는 별도 composition root.
- `bootstrap/live_configuration.py`: default disabled, exact `LIVE`, live namespace, cap·version validator.
- `bootstrap/live_permission.py`: Gateway 공개 read surface와 live mutation의 별도 권한 검사.
- `adapters/binance/live_clients.py`, `live_endpoints.py`: fixed URL, GET allowlist, read-only HTTP 방벽.
- `adapters/binance/read_facade.py`: 기존 Testnet read operation을 이동한 공통 기술 facade.
  주문 권한·Phase 13 Testnet fixture permit은 공유하지 않는다.
- `bootstrap/application.py`: `_LIVE_ORDER_CAPABILITY`를 Testnet/fake에서 분리하고 기존 journal·
  account/market recovery·unknown execution fail-closed 경로를 LIVE에도 연결했다.
- `bootstrap/sidecar.py`: 기존 seven-field Testnet wire와 별도 complete live wire를 엄격하게 구분한다.
- `sidecar/macos_profile.rs`: native Keychain profile을 process lifetime에 고정하고 wire·저장소를 선택한다.
  기존 macOS pipe/process 구현은 `sidecar/macos.rs`에 남겼다.
- `scripts/configure_live_keychain.sh`, `run_live_read_only_from_keychain.py`: hidden credential 등록과
  주문 API가 없는 actual preflight 실행 도구.

Live Keychain service는 `com.binance-auto.trader.live`, account는 각각 `api-key`, `api-secret`이다.
Native profile은 `com.binance-auto.trader.desktop-profile` / `execution-profile`에 저장한다.
Profile 부재는 기존 Testnet 동작을 유지하며 live는 disabled다. `LIVE_READ_ONLY`에서만 live 조회를
선택하고 `LIVE_ORDERS_V1`은 별도 `LIVE ORDERS 10 USDT` 입력이 있어야 저장된다. 이번 실행은
`LIVE_READ_ONLY`를 확인했고 orders profile은 활성화하지 않았다. 실행 중 profile 변경은 다음
process부터 적용되므로 현재 runtime owner 경로가 중간에 바뀌지 않는다.

Live data directory는
`~/Library/Application Support/com.binance-auto.trader/com.binance-auto.trader.live/`이며
`trade-history.jsonl`, 해당 history에서 파생되는 pending/manual-kill 파일과 `.backend-runtime.lock`을
같은 live directory에 둔다. 기존 Testnet `history.jsonl`과 owner를 fallback하거나 복사하지 않는다.
Live history의 잘못된 namespace·symlink·hardlink는 조립 전에 거부한다.

## 공식 API 확인

2026-09-08에 아래 Binance 공식 자료를 확인했다.

- [REST base endpoint 및 signed 요청](https://developers.binance.com/en/docs/products/spot/rest-api)
- [공식 market WebSocket 주소·combined stream](https://github.com/binance/binance-spot-api-docs/blob/master/web-socket-streams.md)
- [WebSocket API base endpoint](https://developers.binance.com/en/docs/products/spot/web-socket-api)
- [계좌·commission·myFilters](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/account)
- [Reference price](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/market)
- [Signed user-data subscription](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/ws-api/user-data-stream)
- [수수료와 수신 자산 처리](https://developers.binance.com/en/docs/products/spot/faqs/commission_faq)
- [오류 코드 `-2015`](https://developers.binance.com/en/docs/products/spot/errors)

고정 주소는 REST `https://api.binance.com/api`, market `wss://stream.binance.com:443`,
account WS `wss://ws-api.binance.com:443/ws-api/v3`이다. URL 환경변수·renderer·local file override나
다른 host fallback은 제공하지 않는다. 서명 GET은 기존 HMAC/server-time 구현을 사용하며 live POST의
자동 timestamp retry는 비활성화했다. HTTP 3xx도 다른 endpoint로 따라가지 않는다.

## 위험 정책과 미완료 운영 조건

기존 `RiskPolicy(version=1)`의 order `10 USDT`, projected Position `10 USDT`, daily loss `None`,
`REALIZED_ONLY`, `CANCEL_AND_LIQUIDATE`를 조립했다. Daily loss의 계산·게시를 보존하고 새로운
일일 손실 차단 정책은 추가하지 않았다. REST/permission/Controller의 cap·policy version이 일치해야
주문 gate를 얻는다. Testnet/fake 표식, cap 누락·초과·policy drift는 조립 또는 전송 전에 거부한다.
STOP/recovery SELL은 기존 authoritative Position 전량 정리 계약과 BUY cap 예외를 유지한다.

기존 fee 정책도 완화하지 않았다. 제3 자산 수수료나 MARKET BUY의 수신 ETH 수수료로 미지원 dust가
생길 수 있으면 주문 prepare를 차단한다. `run_live_read_only_from_keychain.py`는 읽기 성공과 이
pilot 조건의 성공을 구분해 보고한다. Withdrawal 권한 비활성, 두 OS 사용자의 서로 다른 Binance
account/API key, pilot 동안 다른 process·수동 거래 배제는 native/운영 증거로 별도 확인해야 한다.
`GET /api/v3/account` 성공만으로 API key withdrawal 권한을 증명했다고 해석하지 않는다.

## 자동 검증

| 검증 | 실행 명령과 결과 |
|---|---|
| Backend 전체 | `cd backend && env -u BINANCE_TESTNET_API_KEY -u BINANCE_TESTNET_API_SECRET -u BINANCE_TESTNET_BASELINE_HISTORY -u BINANCE_TESTNET_BASELINE_HISTORY_FD BINANCE_RUN_TESTNET=0 BINANCE_RUN_TESTNET_ORDERS=0 BINANCE_RUN_PHASE13_PUBLIC_CASE2=0 BINANCE_RUN_PHASE13_RECOVERY_ONLY=0 PYTHONPATH=src PYTHONWARNINGS=error .venv/bin/python -m unittest discover -s tests -q` → `1058` 실행, `1048` PASS / `10` 외부 safe skip, `38.830s`. |
| Live 집중 | `PYTHONPATH=backend/src:backend backend/.venv/bin/python -m unittest tests.unit.bootstrap.test_live_bootstrap tests.integration.test_live_readiness_flow -q` → `12/12` PASS. Exact HTTP/WS, disabled·namespace·cap·version·Testnet 혼용·redirect, history symlink/hardlink, native wire, 실제 root READY와 새 graph restart, unknown external execution 차단을 검증했다. |
| 도구 집중 | `python3 -m unittest scripts.test_run_live_read_only_from_keychain scripts.test_package_sidecar scripts.test_windows_development scripts.test_check_communication_traceability -q` → `42/42` PASS. |
| UI | `cd UI && node node_modules/typescript/bin/tsc -b --pretty false`; `node node_modules/vitest/vitest.mjs run` → typecheck PASS, `47` files / `483` tests PASS. 실제 live read-only의 실계좌·주문 비활성 표기 포함. |
| Rust | `cd UI/apps/desktop/src-tauri && cargo test --locked --offline` → `47/47` PASS. `cargo clippy --lib --locked --offline -- -D warnings`, `cargo fmt --all --check`, `cargo check --tests --locked --offline` PASS. |
| Communication | `python3 scripts/check_communication_traceability.py` → `total=126 complete=126 gap=0`. Live permission의 exact 세 위임 method만 architecture allowlist에 추가했다. |
| 주석·정적 검사 | 신규 Python 9개 파일 AST의 모든 함수·클래스 필수 docstring field 누락 `0`; 논리 블록/문장 주석 확인. Shell syntax·`git diff --check` PASS. |

최초 Rust loopback test와 UI Python child test는 sandbox의 local bind 제한 때문에 실패했다.
사전 승인된 host 권한으로 재실행한 결과만 PASS로 기록한다. 최초 backend 전체 실행의 두 실패는
Session 7의 엄격한 mode 혼용 거부와 새 permission 위임에 맞지 않던 기존 test 계약을 갱신한 뒤
해소했다. 검증을 통과시키기 위해 live gate를 완화하지 않았다.

전체 root scripts는 `python3 -m unittest discover -s scripts -p 'test_*.py' -q`에서 `192` 실행,
`6` failures / `3` errors였다. Retained supply inventory/SBOM과 현재 lockfile 좌표가 불일치하고
license test의 고정 NOASSERTION 기대값 `372`가 현재 `373`과 다르다. 세 lockfile과 두 retained
inventory 파일 모두 시작 HEAD와 byte-identical임을 확인했으므로 이번 live source 변경의 dependency
추가로 해석하지 않는다. 기존 historical evidence를 현재 PASS로 재결속하거나 full supply `NO_GO`를
해제하지 않았다. 별도 공개 배포 track의 정직한 후속 갱신이 필요하다.

세 lockfile은 이번 작업에서 변경하지 않았다.

| 파일 | SHA-256 |
|---|---|
| `backend/uv.lock` | `0347322723c810df54581b9cd634e0e0b36eb69d6b38cd464df608b1ac373ccf` |
| `UI/pnpm-lock.yaml` | `cd48652d053fef9ef8c30d8eb14543c25a5988de1f301c9aa1f3408a00d64e97` |
| `UI/apps/desktop/src-tauri/Cargo.lock` | `00eb3a55aac1ccff40bed9a2e76ad2a4e6195fb343d58036a2f7815c2261f54a` |

## macOS package 증거

macOS `26.6.2` arm64, Python `3.11.14`, PyInstaller `6.22.2`에서 별도 target에 current working source를
빌드했다. Session 4의 app/DMG는 변경하지 않았다.

```sh
cd UI
CARGO_TARGET_DIR=/Users/oscar/Desktop/Binance_Auto/UI/apps/desktop/src-tauri/target/session7-baeabcd \
CARGO_BUILD_JOBS=4 node scripts/desktopLauncher.mjs build --bundles app \
  --config '{"bundle":{"macOS":{"hardenedRuntime":false}}}'
```

Build 실행에는 Apple signing material과 Testnet credential 환경을 제거했다. 최종 Python sidecar와
native wire 변경 후 재빌드했으며 TypeScript/Vite `295` modules도 통과했다. Outer app은
`codesign --force --deep --sign - --timestamp=none`으로 봉인했다.

- App: `UI/apps/desktop/src-tauri/target/session7-baeabcd/release/bundle/macos/Binance Auto Trader.app`
- DMG: `UI/apps/desktop/src-tauri/target/session7-baeabcd/release/bundle/dmg/Binance Auto Trader_0.1.0_aarch64_session7-adhoc.dmg`
- App tree SHA-256: `d1920ab1f2e376d0b5e964bac964f9d20a1ade4f8c47e2b7fdaa65d2656dcd2d`
  (`_calculate_app_content_tree`: regular files `5`, total bytes `28,986,783`).
- DMG SHA-256: `87284a09a745fe973db02e6ecb4986aae347dfda949f788a1aa73416848953d2`,
  bytes `21,109,018`.
- `codesign --verify --deep --strict` PASS; `Signature=adhoc`, `flags=0x2`, Team ID 없음, arm64.
- `hdiutil verify` PASS; read-only mount한 app의 strict signature PASS, source app과 `diff -qr` 차이 `0`;
  검증 후 mount 해제 완료.
- `python3 scripts/check_phase12_secrets.py --keychain-service com.binance-auto.trader.live --keychain-account api-key --keychain-account api-secret backend/src UI/src scripts <bundle-directory>` → live credential canary `2`, 검사 파일 `745`, 유출 `0` PASS.

이는 개인용 non-hardened ad-hoc artifact이며 Developer ID·notarization·Gatekeeper 공개 배포 신뢰를
의미하지 않는다. 실제 signed live READY 여부는 아래 actual 검증 결과와 별개다.

## Actual read-only 관측 및 재개

사용자가 이번 작업의 권한을 사전 승인했으므로 추가 permission 요청 없이 Keychain read와 official
live read-only preflight를 수행했다. Initial sandbox metadata 조회의 `44` unavailable 결과는 실제
Keychain item 부재 증거로 사용하지 않는다. Host 권한 조회에서는 두 item이 존재했고 native profile은
`LIVE_READ_ONLY`였다.

1. `PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_live_read_only_from_keychain.py --confirm-live LIVE`
   → 첫 signed account GET에서 `BinanceAPIError`, `api_code=-2015`, mutation `0`.
2. 사용자 요청에 따라 값 노출 없이 live key/secret 형식과 기존 Testnet pair 일치를 검사했다.
   둘 다 canonical alphanumeric, key와 secret은 서로 다름, Testnet pair와 같은 값·뒤바뀐 pair 모두
   아님을 확인했다. 값·길이·부분 문자열은 출력하지 않았다.
3. live service/account가 기본 Keychain 한 곳에만 각각 1개 존재함을 확인했다. 오래된 다른
   Keychain 중복 item을 조회하는 경우는 관측되지 않았다. 같은 live 계좌 GET 재검증도 `-2015`;
   시스템 HTTP proxy 설정 없음. 공식 문서상 key/IP/permission
   오류이며 원본과 저장 값의 일치나 구체 거부 원인까지 입증한 것은 아니다.
4. Credential 없는 실제 공개 live GET `exchangeInfo`와 `referencePrice`,
   `wss://stream.binance.com:443` market 구독은 모두 PASS, signed `0`, mutation `0`.
5. Keychain 등록 도구의 두 password prompt는 같은 값을 두 번 입력하는 확인 단계다.
   API key를 두 번 입력한 뒤 API secret을 두 번 입력하며 `native profile saved`로 완료된다.
6. 2026-09-08 사용자의 “재등록 완료” 이후 production preflight도 `-2015`였다.
   앱 REST client를 사용하지 않는 Python 표준 라이브러리 독립 검증에서도 공식 serverTime을
   받아 HMAC-SHA256으로 서명한 `GET https://api.binance.com/api/v3/account`가 HTTP `401`,
   code `-2015`를 반환했다. Proxy와 redirect는 비활성화했고 키·서명·계좌 원문은 출력/저장하지
   않았다. 독립 검증의 주문 mutation은 `0`이다. 이 결과는 앱 REST wrapper만의 문제라는
   가설을 약화하지만 저장 값과 Binance 원본의 일치 또는 거부 원인을 확정하지 않는다.

7. API restrictions 스크린샷 검토와 읽기 전용 권한 안내 후 사용자가 다시 “재등록 완료”라고
   알린 시점에도 같은 production preflight를 실행했다. 결과는 `BinanceAPIError`,
   `api_code=-2015`, exit `1`, mutation `0`이었다. 실제 저장된 Binance 권한 설정 자체는
   스크린샷 이후 확인하지 못했으며, 인증 성공·package READY로 기록하지 않는다.

등록/재등록 명령:

```sh
/bin/sh /Users/oscar/Desktop/Binance_Auto/scripts/configure_live_keychain.sh set
```

아래 내용은 인증 성공 전의 재개 조건 기록이다. 당시 남은 macOS 검증은 account/filter/commission·open orders/lists zero preflight,
위 fresh app의 실계좌·주문 비활성 READY, History/local Position/pending/unknown 확인,
정상 shutdown·owner RELEASED·잔여 process 0, fresh restart와 동일 상태 재확인이다.
당시 실패·미실행 항목을 PASS로 바꾸지 않았다. 당시에는 이 증거가 없으므로 **macOS Session 8 진입은
`NO_GO`**이고 Session 7 전체, Cross-platform package master, Private Beta master와 Phase 13/live
master는 계속 미완료다. Windows package/live credential/readiness는 다른 OS에서 별도로 수행해야 한다.

## 공인 IP 수정 후 첫 실제 검증 — 중간 기록

사용자가 공인 IP를 허용 목록에 재등록했다고 알린 뒤 같은 preflight 명령이 exit `0`으로 성공했다.
`account`, `account_filters`, `symbol_filters`, `commission`, `reference_price`,
`account_open_orders_zero`, `account_open_order_lists_zero` 모두 true다.
`read_only_preflight=PASS`, `supported_fee_asset=false`, `buy_fee_without_base_dust=false`,
`pilot_prerequisites=NO_GO`, `order_mutations=0`이다. 과거 `-2015`는 보존하되 현재 인증 실패로
해석하지 않는다. IP 변경 후 성공했으므로 IP 허용 설정 관련 원인이 유력하며 서버 내부 원인까지
확정한 것은 아니다. 수수료 설정은 자동 변경하지 않았다.

같은 digest의 Session 7 app을 `LIVE_READ_ONLY` profile로 실행했다. Native UI에서 Binance 연결됨,
실제 시장·실계좌·주문 비활성, 실제 차트, 매매 시작 전, 최근 체결 없음이 확인됐다.
Command-Q → 종료로 정상 종료했고 live namespace owner `RELEASED`, 앱·sidecar process `0`을
확인했다. 첫 process 조회는 sandbox에서 거부됐고 host 권한의 이름만 조회한 결과로 검증했다.
전체 History 버튼 조작은 UI 상태 변경 감지로 실행되지 않아 전체 History 검증으로 확대하지 않는다.

Fresh launch에서 native app process는 존재하지만 sidecar는 없고 owner는 `RELEASED`다.
SecurityAgent process가 나타났으며 앱 AX 조회는 timeout이었다. Computer Use가
`com.apple.SecurityAgent` 접근을 금지하여 대화상자 내용은 확인하지 못했다. 사용자의 시스템
Keychain 확인 후 fresh READY·local Position/pending/unknown·최종 종료를 추가 검증해야 한다.
이 중간 시점의 Session 8 NO_GO 이유는 인증이 아니라 수수료 조건 미충족과 fresh restart/운영 증거 공백이었다. 최신 결과는 아래와 같다.

## Session 7 macOS 기술 검증 마감

사용자의 Session 7 마무리 요청 뒤 이전에 대기하던 native 앱이 정상 연결된 것을 확인했다.
실제 시장·실계좌·주문 비활성 표시, LIVE 차트와 전체 History 체결 0건을 확인하고 Command-Q의
종료 확인으로 정상 종료했다. 전체 History는 이번에 실제로 열었으며 앞선 UI 조작 실패를 대체한다.

새 `scripts/live_runtime_readiness.py`는 별도 업무 graph나 fixture를 만들지 않고 production
`create_live_application_runtime`과 native와 같은 `.backend-runtime.lock`·live history를 사용한다.
주문 가능 configuration은 lock 획득 전에 거부한다. Lock 충돌·미조정 owner는 우회하지 않는다.
실패·원문 exception을 보고하지 않고 타입만 표시하며 cleanup 실패는 ORPHANED로 남긴다.
다음 명령을 서로 다른 Python 프로세스에서 2회 실행해 각각 exit 0을 확인했다.

```sh
PYTHONPATH=backend/src backend/.venv/bin/python scripts/live_runtime_readiness.py --confirm-live LIVE
```

두 실행 모두 `ready`, `orders_disabled`, `startup_reconciled`, `position_zero`, `pending_zero`,
`pending_queries_zero`, `unknown_execution_zero`, `reconciliation_clear`, `history_zero`,
`exchange_open_orders_zero`, `exchange_open_lists_zero`, `closed`가 true였다. 주문 mutation은 0이다.
Actual 성공 경로 실행 뒤 cleanup 예외를 ORPHANED로 보존하는 실패 경로를 보강했으며 해당 경로는
unit test로 확인했다. Native package source/binary는 변경하지 않아 앞선 app/DMG digest를 유지한다.

최종 signed preflight도 exit 0, `read_only_preflight=PASS`다. 최신 도구는 잔액 원문 없이 MARKET에
적용되는 공식 notional 하한 충족 여부를 검사하고 실패 항목을 `blockers`로 반환한다.
실제 blocker는 `supported_fee_asset`, `buy_fee_without_base_dust`,
`quote_balance_meets_market_minimum` 3개다. 잔액 하한 검사는 주문 수량 step·수수료 여유·실행 시점
가격까지 검증한 충분조건이 아니며 통과해도 주문 승인이 되지 않는다.

추가/수정 도구 회귀:

```sh
PYTHONPATH=backend/src backend/.venv/bin/python -m unittest scripts.test_live_runtime_readiness scripts.test_run_live_read_only_from_keychain scripts.test_package_sidecar scripts.test_windows_development scripts.test_check_communication_traceability -q
```

`47` tests PASS (`17.780s`), `git diff --check` PASS. Readiness 신규 4개는 주문 설정 거부, owner
충돌, cleanup 실패의 ORPHANED 처리, pending/unknown 실패 보존을 검증한다. Preflight 5개는
기존 secret-safe 검증과 실제 MARKET 금액 하한 경계·비적용 filter 제외를 검사한다.
최종 host process 확인은 native 0, owner RELEASED이며 secret scan은 2 canary/751 files PASS다.

### Session 8을 실제로 열기 위해 남은 사항

1. BNB 수수료 환산·원래 fee 자산·체결별 환산 근거·durable replay를 회계 모델에서 지원하거나,
   공식 live commission 조회로 현재 기존 모델이 지원하는 조건을 확인해야 한다.
2. BNB가 부족하면 수신 ETH에서 수수료가 빠질 수 있다. ETH 수수료 차감 후 LOT_SIZE 미만 잔여를
   처리할 정책과 durable 회계·STOP/restart 검증이 필요하다. 잔여를 버리거나 0으로 반올림해서
   기존 zero exposure 조건을 통과시킬 수 없다. BNB 할인 해제만으로는 이 문제가 해소되지 않는다.
3. 실제 계좌의 USDT 가용 잔액이 공식 MARKET 최소 주문 금액보다 작다. 입금·전환은 수행하지 않았다.
4. 양 OS 계좌 분리와 pilot 중 다른 process/수동 거래 배제는 사용자 확인이 필요하다. API withdrawal
   비활성은 앞선 사용자 제공 화면의 관측이며 signed account GET로 API key 권한을 증명하지 않는다.

수수료 근거: Binance 공식
[Commission FAQ](https://raw.githubusercontent.com/binance/binance-spot-api-docs/master/faqs/commission_faq.md).
현재 live fee 차단은 위 미지원 회계를 숨기지 않는 동작이다. 이를 삭제하거나 Session 8을 GO로
바꾸는 수정은 하지 않았다. Windows live/master 미완료도 그대로이며 macOS 기술 완료와 구분한다.

### 잔여 수량 정책의 최초 제안 — 이후 사용자 승인 및 구현 완료

수수료 때문에 발생한 ETH 중 공식 LOT_SIZE stepSize 미만의 수량만 별도 durable 잔여 장부로
이관하고, 원래 수량·수수료·원가를 보존해 재시작 시 계좌와 대조하는 방안을 제안한다.
이 경우 청산 완료는 거래 가능한 전략 Position 0과 별도 공개된 잔여 장부로 판정하며,
전체 ETH가 0이라는 기존 zero exposure 주장과는 다르다. MIN_NOTIONAL 미달이라는 이유만으로
stepSize 이상의 Position을 잔여로 이관하지 않는다. 자동 변환·추가 매수·출금은 포함하지 않는다.
이 변경은 기존 Session 8의 완전한 zero exposure 기준을 바꾸므로 최초 제안 당시에는 적용하지
않았다. 이후 사용자가 설명을 확인하고 “그래 구현해”라고 승인했다. 당시 BNB 수수료 환산 및 잔액 조건도 남아 있었다. 현재 잔액 조건은 충족됐으며 제3 자산 수수료 차단은 유지한다.

## 승인된 잔여 정책의 구현·새 패키지

구현 계약과 source별 책임은 [RESIDUAL_ETH_ACCOUNTING.md](RESIDUAL_ETH_ACCOUNTING.md)를 따른다.
Backend 최종 전체 `1065`, `OK (skipped=10)`, `35.945s`; UI 최종 전체 `487` PASS(48 files, 7.06s), typecheck PASS. 도구 `9`와 Communication `126/126`
PASS. Full supply의 기존 GAP는 변경하지 않았다. 실제 source 읽기 runtime도 READY/CLOSED PASS다.

새 artifact는 `UI/apps/desktop/src-tauri/target/session7-residual-baeabcd/release/bundle/` 아래다.
기존 `session7-baeabcd` package는 보존했다. 마지막 source 보강 후 cached build를 다시 실행했으며
최종 로그는 `/private/tmp/binance-session7-residual-final-build.log`다.

- App: `macos/Binance Auto Trader.app`, tree SHA-256
  `3cda7f30c53c0bd240e2d90e01b8a09cb556ff7043e8d8315e87a81cd0cfbaa3`, 5 files / 29,003,567 bytes.
- DMG: `dmg/Binance Auto Trader_0.1.0_aarch64_residual-adhoc.dmg`, SHA-256
  `559e41f160c53f89a603dd90fba7cab41566fc587244e0414eb8bb561b2d3a05`, 21,126,032 bytes.
- Non-hardened ad-hoc strict codesign PASS, DMG checksum PASS, read-only mount strict signature 및
  source와 mounted app byte parity PASS, mount 해제 완료, live secret scan 2 canary/762 files PASS.
- 새 native app에서 실제 시장/실계좌/주문 비활성·LIVE chart·빈 History를 확인하고 Command-Q로
  정상 종료했다. Owner RELEASED, native app/sidecar 0. 실제 잔여가 없는 계좌이므로 잔여 notice의
  positive UI와 실제 잔여 생성은 fixture 검증이며 live 주문으로 만든 증거로 주장하지 않는다.

최신 signed preflight는 `read_only_preflight=PASS`, `base_fee_residual_policy=true`,
`quote_balance_meets_market_minimum=true`, `supported_fee_asset=false`, mutation 0이다.
잔액이 변경된 사실을 관측했으나 입금/전환은 수행하지 않았다. 현재 자동 blocker는 제3 자산 수수료다.
이전의 세 blocker와 패키지 digest들은 각 시점의 역사 기록으로만 해석한다.

최종 독립 runtime 재검증: `scripts/live_runtime_readiness.py --confirm-live LIVE`의
READY/CLOSED·`residual_assets_zero` 포함 13개 check PASS, `order_mutations=0`.

최종 UI 실행은 `cd UI && node node_modules/vitest/vitest.mjs run`이다. Sandbox 안에서는
process fixture 두 건이 readiness 전에 종료(485 PASS/2 FAIL)했으며, 로컬 통신이 허용된
host 실행에서 같은 source 전체 487 PASS를 확인했다. pnpm wrapper의 무출력 대기는 중단하고
이미 설치된 동일 Vitest entrypoint를 직접 실행했다.

## 2026-09-09 다음 세션 준비

[MACOS_SESSION8_PREPARATION.md](MACOS_SESSION8_PREPARATION.md)에 수수료 설정 해결 절차와
실제 signed/runtime 재검증을 기록했다. 읽기 PASS·13 runtime checks PASS·주문 0이며
BNB 설정 변경 후 사전검사와 운영 계좌/Windows 조건 확인을 기다린다.

## 2026-09-09 BNB 유지 확장 최종 검증

BNB OFF 대신 사용자 승인 회계 확장을 완료했다. Signed preflight `blockers=[]` 및 BNB 가격 근거
PASS, 실제 source runtime 13 checks PASS, 주문 0이다. Backend 1,074(10 skip), 추가 최종 집중 10,
UI 488, 도구 9, Communication 126/126 PASS. 세부 명령은 `BNB_FEE_ACCOUNTING.md`를 따른다.
새 target `session7-bnb-baeabcd` app tree SHA-256
`6f79dcebfce3b621a4bcde9ccbd27a206d9e7b87d6c3e754045afb1fac262991` (5 files, 29,018,255 bytes),
DMG SHA-256 `b6651cfb4e0ddf348ec95a23d9b1653e5edbe20c0f955d870b5455101d66783d`
(21,140,587 bytes). Ad-hoc strict signature·DMG checksum·read-only mount parity·2 canary/772 files
secret scan PASS. Native 최초 Keychain 대기는 사용자 허용 뒤 해결됐으며 실계좌 읽기 연결·LIVE chart·
정상 종료·fresh restart·다시 정상 종료 PASS다. 최종 owner RELEASED, 앱/sidecar 0.
BNB 양수 체결·잔여 생성은 fixture이며 실제 주문은 실행하지 않았다. 계좌 격리 운영 확인·Windows
live와 platform/account별 실제 주문 승인은 미완료이므로 Session 8 전체 GO로 확대하지 않는다.

## macOS Session 8 진입 재판정 — 2026-09-09T00:41:50+09:00

**기술적 진입 GO / 실제 주문은 계좌 전용 사용 확인과 별도 승인 후 시작.**
사용자 승인 순서에 따라 Windows 검증은 macOS pilot 완료 후 진행하며 선행 차단이 아니다.
이번 실제 signed preflight는 `blockers=[]`, BNB 평가·지원 수수료·잔액 포함 PASS다.
`live_runtime_readiness.py --confirm-live LIVE`는 13 checks 모두 true로 READY/CLOSED를 통과했다.
Position·잔여 자산·pending·unknown·history·open order/list는 0이고 reconciliation은 clear다.
검증 패키지 manifest에 기록된 변경 production source 45개를 대조해 추가 drift가 없음을 확인했다.
이는 저장된 변경 파일의 대조이며 전체 저장소 clean/공개 release provenance 검증으로 확대하지 않는다.
실제 order mutation 0, 주문 profile 변경 없음. 실제 주문 직전에 macOS 계좌의 수동 거래·다른 봇
중지를 확인하고 order/position 각각 10 USDT 범위의 주문 승인을 적용한다.
