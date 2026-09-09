# macOS Session 8 실행 준비 재검증

## 2026-09-09 22:27 KST 운영 확인 완료·최신 signed PASS

사용자가 pilot 중 해당 계좌의 다른 봇·수동 거래 중지와 API 출금 권한 비활성 유지를 이미
확인했다고 명시했다. 사용자 확인 완료로 기록하고 같은 확인을 재요청하지 않는다.
`PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_live_read_only_from_keychain.py --confirm-live LIVE`
재실행 결과 BNB/잔액 포함 12개 checks true, `blockers=[]`, signed 읽기와 사전조건 PASS,
주문 mutation 0이다. 아래 22:24 NO_GO는 과거 관측이다. Profile 변경은 없다.
출금 권한의 API 검증 또는 실제 pilot 완료로 확대하지 않는다. 사용자 직접 actual 실행 후
terminal lifecycle·수수료/잔여 장부·fresh restart 대조는 여전히 남는다.

## 2026-09-09 22:24 KST 사용자 계좌 운영 확인 전 기술 작업 마감

**코드·자동 검증·새 macOS package·native 읽기 재시작 완료 / 실제 주문 미실행.**
계좌 운영 확인 단계로 필요한 기술 결과를 제공한다. 다만 마감 시점 signed 사전조건은
**BNB `NO_TRADES_AFTER_BOUNDED_READS` 한 항목으로 NO_GO**다. 실제 거래 시작 GO로 해석하지 않는다.
이전 22:13 PASS와 이번 작업 중 PASS는 그 시점의 관측이며 마지막 실패를 덮어쓰지 않는다.

### 변경과 책임

- `backend/src/binance_auto_trader/adapters/binance/bnb_fee_valuator.py`: 원 체결의 동일 UTC 1초
  구간에 대한 정상 빈/무체결 응답만 최대 4회 GET. 정수 1·1·2초 대기(합계 4초), 각 요청은 기존
  transport timeout. HTTP 오류·timeout·malformed 값·다른 시간 구간은 즉시 전파한다.
  계속 무체결이면 기존 정책대로 차단하며 다른 시점 가격을 사용하지 않는다.
- `scripts/run_live_read_only_from_keychain.py`: 고정 allowlist reason code만 보고한다.
  알 수 없는 예외 원문·서명 URL은 `UNCLASSIFIED_REDACTED`로 숨긴다.
- 기존 회계 V1·원자산 수수료·durable 이력·잔여 ETH 정책, order/position 각각 10 USDT,
  daily loss None, 주문 권한 경계는 유지했다. 새 업무 클래스나 Operation을 추가하지 않았다.
  변경 함수 docstring과 블록·문장 주석을 작성했다.
- 새 `test_bnb_valuation_retry.py` 및 기존 회계/runner 테스트를 같은 변경 묶음에 포함했다.
  Source 기준은 `1e7df5fd0e76c015cb2f9921289885567a39c819` + 이번 작업 트리이며 commit은 만들지 않았다.

### 실행 증거

| 범위 | 명령과 결과 |
|---|---|
| Backend 전체 | backend에서 외부 Testnet/order opt-in 4개를 0으로 고정하고 credential 환경을 제거한 `PYTHONPATH=src PYTHONWARNINGS=error .venv/bin/python -m unittest discover -s tests -q` → **1,089 실행 / 1,079 PASS / 10 safe skip**, 35.175초. `/private/tmp/session8-backend-final.log` |
| UI 전체 | UI에서 `node node_modules/vitest/vitest.mjs run` → **488 PASS**, 48 files. `/private/tmp/session8-ui-tests.log` |
| Rust | src-tauri에서 별도 `CARGO_TARGET_DIR=.../target/session8-tests cargo test --locked --offline -q` → **47 PASS**. `/private/tmp/session8-rust-final.log` |
| 도구 | `PYTHONPATH=backend/src backend/.venv/bin/python -m unittest scripts.test_live_runtime_readiness scripts.test_run_live_read_only_from_keychain scripts.test_package_sidecar scripts.test_windows_development scripts.test_check_communication_traceability -q` → **48 PASS** |
| Communication | `backend/.venv/bin/python scripts/check_communication_traceability.py` → **126/126, gap 0** |
| TypeScript/Vite | 최종 native build의 tsc와 Vite production build PASS |
| 실제 읽기 runtime | `PYTHONPATH=backend/src backend/.venv/bin/python scripts/live_runtime_readiness.py --confirm-live LIVE` → 이번 작업 중 2회 각각 **13/13 PASS**, 최종 native 종료 뒤 독립 process 포함 |
| 실제 signed 사전검사 | `PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_live_read_only_from_keychain.py --confirm-live LIVE` → 작업 중 12/12 PASS, 마감 시 11/12 true·BNB 한 항목 NO_GO |
| Secret scan | `scripts/check_phase12_secrets.py --keychain-service com.binance-auto.trader.live --keychain-account api-key --keychain-account api-secret backend/src UI/dist UI/apps/desktop/src-tauri/target/session8-1e7df5f/release/bundle scripts MACOS_SESSION8_VALIDATION.md` → **2 canary / 1,149 files PASS** |

최초 전체 Backend는 대기 시간의 float literal/annotation이 금융 source float 금지 검사를
위반해 1 FAIL이었다. 정수 초로 수정하고 최종 전체 PASS를 확인했다. Rust 최초 sandbox 실행은
실제 HTML server fixture의 소켓 제한으로 46 PASS/1 FAIL이었고, 로컬 소켓 허용 환경에서
동일 코드 47 PASS를 확인했다. CUA 최초 앱 조회 timeout은 재조회로 해소됐다. Full supply master의
기존 GAP는 이번 테스트 성공으로 완료 처리하지 않는다. 세 dependency lockfile 변경은 없다.

### 최종 macOS artifact와 native smoke

최종 빌드 명령은 UI에서 `CARGO_TARGET_DIR=.../target/session8-1e7df5f CARGO_BUILD_JOBS=4
node scripts/desktopLauncher.mjs build --bundles app --config '{"bundle":{"macOS":{"hardenedRuntime":false}}}'`다.
최종 정수 대기 수정 이후 다시 빌드했으며 `/private/tmp/session8-build-final.log`에 기록했다.
기존 Session 7 package는 보존했다. 개인용 non-hardened ad-hoc 신뢰 범위를 유지한다.

- App: `UI/apps/desktop/src-tauri/target/session8-1e7df5f/release/bundle/macos/Binance Auto Trader.app`
  — content tree SHA-256 `b1cb06f518bb23dc1476637c6e7f04c56d00d1c9e40148a4dfb58a26f762eafc`,
  5 files / 29,021,567 bytes.
- DMG: `UI/apps/desktop/src-tauri/target/session8-1e7df5f/release/bundle/dmg/Binance Auto Trader_0.1.0_aarch64_session8-adhoc.dmg`
  — SHA-256 `eadee96991444673c515692846c16444910304152c949a46f7660411349ddff4`, 21,144,092 bytes.
- `codesign --force --deep --sign - --timestamp=none` 후 source/mounted app의 strict verify PASS,
  `hdiutil verify` PASS, read-only mount와 `diff -qr` 차이 0, mount 해제 완료.
- Bundle 옆 `session8-source-manifest.json`에 HEAD·source/lockfile 364개 hash와 artifact digest를
  기록했다. 이는 작업 트리 빌드의 로컬 증거이며 공개 release provenance로 확대하지 않는다.
- Native Keychain profile `LIVE_READ_ONLY`를 확인한 후 CUA로 해당 앱을 실행했다. 실제 process
  경로도 위 Session 8 bundle과 일치했다. `실계좌 · 주문 비활성`, 연결됨, USDT 40.46,
  ETH 0.0000, LIVE chart, History 체결 0건을 확인했다. 자동매매 실행 버튼은 누르지 않았다.
- Command-Q와 종료 확정 후 process 0, 같은 앱 fresh restart 후 실계좌 읽기·LIVE 복구,
  종료 취소·다시 종료 확정을 검증했다. 이후 독립 source runtime 13/13 PASS,
  최종 owner `RELEASED`, native/sidecar 잔여 process 0. 실제 order mutation 0.

### 다음 단계와 한계

사용자는 이제 pilot 계좌에서 다른 봇과 수동 거래가 배제되는지 등 **계좌 운영 상태를 확인**할 수 있다.
이 확인과 실제 주문 시작은 별도다. 현재 마감 사전조건 NO_GO는 유지하며 actual 시작 전 최신
검사를 다시 통과해야 한다. “직전 1초에 체결 존재”라는 V1 규칙 때문에 모든 시점의 PASS를
보장하지 않는다. 무체결 봉의 가격이나 더 오래된 가격을 허용하려면 별도의 회계 정책 결정이 필요하다.
이를 준비 완료를 위해 자동 완화하지 않았다. 실제 체결 후 회계/잔여/restart 검증, Windows
Session 7 순차 진입과 양 OS master는 아직 미완료다.

## 2026-09-09 22:13 KST 최신 잔액·사전조건 재확인

사용자 화면에서 USDT가 Earn에 있음을 확인한 뒤 재조회 요청을 받았다.
`2026-09-09T13:13:12.560298Z`의 signed Spot account 응답은 USDT free
`40.45627619`, locked `0.00000000`이다. 이어 기존 `run_preflight`를 주문 비활성
LiveConfiguration으로 실행해 12개 checks 모두 true, `blockers=[]`,
`read_only_preflight=PASS`, `pilot_prerequisites=PASS`를 확인했다. 실제 주문 mutation 0.
이전 USDT 부족 관측은 현재 blocker가 아니다. 이번 BNB 평가도 PASS지만 이전 동일 구간
응답 변화에 대한 코드 보완을 수행한 것은 아니며, 단일 PASS로 지연/무체결 문제가 해결됐다고
판정하지 않는다. 이번 재검사는 signed 사전조건이며 runtime/UI·실제 lifecycle을 재검증한 것은
아니다. macOS actual pilot과 체결 후 회계/restart·Windows 순차 진입은 여전히 미완료다.

## 2026-09-09 22:10 KST 사용자 화면과 API 잔액 불일치 확인

사용자는 계좌에 40 USDT가 있다고 보고했다. 전체 Binance 계좌의 USDT가 없다고 단정하지 않는다.
`2026-09-09T13:10:58.731145Z`에 같은 live Keychain credential로 signed
`https://api.binance.com/api/v3/account`를 재조회했다. accountType `SPOT`, 원응답의 USDT
free/locked는 각각 `0.00000000`이었다. 이 **동일 payload**를 `_parse_account_snapshot`에
전달한 결과도 각각 Decimal `0E-8`로 일치했다. 이번 불일치가 해당 parser의 40→0 변환 때문이라는
증거는 없다. 다른 wallet, 총 평가금액, 다른 계정/API 연결 중 어떤 원인인지는 아직 미확정이다.
사용자 화면의 40 USDT가 표시된 위치를 확인한 뒤 대조해야 한다. 추가 입금이나 계좌 이동이
필요하다고 단정하지 않는다. 주문·profile 변경은 0회다.

## 2026-09-09 22:07 KST 후속 원인 진단

사용자 요청에 따라 계좌 설정과 주문 권한을 변경하지 않고 다음 실측을 수행했다.
이번 단계는 원인 진단이며 production 코드 수정 또는 실제 pilot 완료가 아니다.

- 기존 Keychain reader와 주문 capability 없는 `BinanceLiveRESTClient`의 signed account/filter
  조회에서 Spot USDT free `0.00000000`, locked `0.00000000`, ETHUSDT MARKET 적용 최소
  notional `5.00000000 USDT`를 확인했다. 비교상 부족분은 `5.00000000 USDT`다. 이 값은
  최소 notional 사전조건만 설명하며 실제 주문 수량·수수료·10 USDT cap 전체 통과를 뜻하지 않는다.
  다른 wallet의 잔액이나 사용자 의도와 API 계좌의 일치 여부는 추정하지 않는다.
- `2026-09-09T13:07:03.077241Z` 기준 거래소 보정 시각과 로컬 시각 차이는 `-39ms`였다.
  두 시각 모두 같은 직전 1초 구간을 선택했고, resolver는 각각
  `BNB valuation requires actual market trades`를 반환했다. 원 예외는 알려진 고정 문구만
  허용해 출력했으며 credential·signature·계좌 식별자는 출력하지 않았다.
- 실패 구간 `1788959222000`~`1788959222999`를 나중에 공식 public `/api/v3/klines`로
  다시 조회하니 1개 봉과 체결 `6`건을 반환했다. 처음 결과가 실제 영구 무체결이었다고 단정할 수
  없다. 응답 반영 시점 차이가 원인일 가능성을 뒷받침하나 거래소 내부 원인은 확정하지 않는다.
- 추가 완료 60초 표본 `1788959201000`~`1788959260999`는 60개 봉, 체결 수 0인 봉 7개,
  양수인 봉 53개, 체결 합계 991건이었다. 이는 해당 조회 시점의 관측이다.
- 이후 기존 signed preflight를 다시 실행해 두 blocker가 유지되는 것을 확인했다.
  BNB 회계/STOP 통합과 읽기 도구 회귀는 아래 명령으로 **19/19 PASS**다.

```sh
PYTHONPATH=backend/src:backend backend/.venv/bin/python -m unittest tests.unit.trading.test_bnb_fee_accounting tests.integration.test_bnb_stop_flow scripts.test_run_live_read_only_from_keychain scripts.test_live_runtime_readiness -q
PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_live_read_only_from_keychain.py --confirm-live LIVE
```

공식 [Kline API](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints)는
1초 주기, UTC start/end, 응답 index 8의 체결 수와 Database source를 명시한다.
[Filters](https://developers.binance.com/docs/binance-spot-api-docs/filters)의 MARKET 최소 notional
적용 조건도 확인했다. 공식 명세가 응답 반영 지연의 상한이나 모든 1초 구간의 양수 체결을
보장한다고 해석하지 않는다.

**필요한 후속 처리:** USDT는 사용자가 의도한 Spot 계좌가 맞는지와 해당 계좌 상태를 확인해야 한다.
BNB는 동일 평가 구간의 제한된 재조회·시간 예산·실패 사유 구분으로 일시적인 빈/무체결 응답을
처리하는 코드 보완과 지연/영구 무체결 회귀 검증이 필요하다. 영구 무체결이면 기존 V1 정책은
계속 차단한다. 다른 과거 가격이나 무체결 봉 종가를 허용하는 것은 `BNB_FEE_ACCOUNTING.md`의
평가 정책 변경이므로 단순 버그 수정으로 몰래 적용하지 않는다. 현재 주문 mutation 0,
Session 8 미완료와 Windows 순차 진입 보류를 유지한다.

검증일: 2026-09-09 21:59~22:01 KST. 시작 clean HEAD:
`1e7df5fd0e76c015cb2f9921289885567a39c819`. 별도 commit은 생성하지 않았다.

## 판정

**macOS 실제 pilot 미완료 / 최신 사전조건 NO_GO / Windows Session 7 순차 진입 보류.**
사용자의 이번 사전 승인은 macOS credential 읽기와 pilot 요청 범위에 적용했다.
추가 주문 승인을 기다리는 상태로 기록하지 않는다. 다만 실행 에이전트는 실제 자금의
암호화폐 주문 제출 또는 자동매매 활성화를 수행할 수 없으므로 실제 pilot은 사용자 직접
실행 단계로 남는다. 이번 실행의 주문 mutation은 0이며 native 주문 profile은 변경하지 않았다.
계좌의 다른 봇·수동 거래 배제는 단순 권한 승인으로 확인된 사실이 아니므로 미확인으로 유지한다.

## §1 적용과 검증 증거

Phase 13 Session 8의 macOS 범위만 확인했다. `CODING_CONVENTIONS.md`와 Communication
메시지 명세의 Case 1 startup `1`~`5`, start `7`, stop `8.1.1`, Case 2 결과 반영
`14` 및 기존 live root·readiness 도구를 확인했다. Production 코드와 클래스는 추가·수정하지 않았다.

| 실행 명령 (저장소 root 기준) | 결과 |
|---|---|
| `PYTHONPATH=backend/src backend/.venv/bin/python -m unittest scripts.test_run_live_read_only_from_keychain scripts.test_live_runtime_readiness` | 9/9 PASS |
| `PYTHONPATH=backend/src:backend backend/.venv/bin/python -m unittest tests.unit.bootstrap.test_live_bootstrap tests.integration.test_live_readiness_flow -q` | 12/12 PASS |
| `backend/.venv/bin/python scripts/check_communication_traceability.py` | 126/126, gap 0 |
| `PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_live_read_only_from_keychain.py --confirm-live LIVE` | signed 읽기 PASS, pilot 사전조건 NO_GO |
| `PYTHONPATH=backend/src backend/.venv/bin/python scripts/live_runtime_readiness.py --confirm-live LIVE` | 독립 process 2회, 각각 13/13 checks PASS, READY/CLOSED |

최초 sandbox 실행은 `LiveKeychainRunnerError`로 실패했다. 승인된 권한 확장으로 기존 읽기 전용
도구를 실행한 뒤 signed 조회에 성공했다. Credential·signature·원 계좌 잔액은 기록하지 않았다.

Signed 검사의 실패 항목은 `quote_balance_meets_market_minimum=false`와
`bnb_fee_valuation=false`다. 나머지 10개 검사는 true다. 첫 항목은 도구가 조회한 free USDT가
MARKET 최소 금액 사전조건을 충족하지 않았다는 뜻이다. BNB 항목의 구체 원인은 이 boolean
보고만으로 확정할 수 없으며 BNB 잔액 부족으로 단정하지 않는다. 이전 00:41의 `blockers=[]`는
과거 관측으로 보존하되 현재 진입 근거로 재사용하지 않는다. 이 도구는 사전조건 NO_GO여도
읽기 성공 시 exit 0을 반환하므로 종료 코드만으로 pilot을 승인하면 안 된다.

두 runtime 결과는 `orders_disabled`, `ready`, `closed`, `startup_reconciled`,
`reconciliation_clear`, `position_zero`, `residual_assets_zero`, `pending_zero`,
`pending_queries_zero`, `unknown_execution_zero`, `history_zero`,
`exchange_open_orders_zero`, `exchange_open_lists_zero`가 모두 true였다.
이는 주문 전 동일 저장소의 읽기 재시작 검증이다. 실제 BUY/SELL 뒤 수수료·잔여 장부 대조나
실계좌 ETH 합계 검증을 통과했다는 의미가 아니다. 새 native package/UI 검증도 수행하지 않았다.

## 남은 완료 조건과 Windows 판정

- [x] 최신 signed 읽기 검사와 주문 비활성 runtime 시작·종료·독립 재시작 검증.
- [ ] MARKET 잔액·BNB 평가 사전조건 해소 후 최신 재검증 및 계좌 전용 운영 확인.
- [ ] 사용자 직접 실행에 의한 자연 신호 기반 첫 terminal BUY/SELL 또는 STOP.
- [ ] 실제 durable History·수수료·잔여 수량/미실현 원가와 재시작 후 계좌 대조.
- [ ] macOS 완료 후 Windows 별도 계좌의 Session 7 live-readiness.

**Windows Session 7로 지금 넘어가도 되는가: 현재 합의된 순서에서는 NO_GO(보류).**
macOS terminal lifecycle과 실제 체결 후 회계·fresh restart 증거가 없고 최신 사전조건도 실패했다.
읽기 runtime PASS만으로 이 조건을 대체할 수 없다. Windows 자체의 결함이 확인됐다는 뜻은 아니다.
Windows live 검증·pilot·동시 운영과 양 OS master 체크는 미완료로 유지한다.
