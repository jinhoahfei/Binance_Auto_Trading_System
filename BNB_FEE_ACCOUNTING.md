# BNB 수수료 회계 확장

2026-09-09 사용자가 BNB 납부를 유지하는 회계 확장을 승인했다. Binance 계좌의 BNB 할인 설정을
변경하지 않았다. 실제 주문·BNB 매수·전환은 수행하지 않았다.

## 실제 수수료와 평가액

Binance `myTrades`와 `executionReport`의 실제 `commission`/`commissionAsset` 및 체결 시각을
원본 사실로 사용한다. BNB 차감량을 ETH나 USDT 차감량으로 바꾸지 않는다.
공식 응답에는 해당 BNB 수수료의 USDT 정산액이나 내부 환산율이 없으므로 이를 역산하지 않는다.

USDT 손익의 평가 정책은 `BINANCE_BNBUSDT_PREVIOUS_1S_CLOSE_V1`이다. 체결 시각이
00:00:12.345 UTC이면 00:00:11.000~11.999 UTC의 공식 BNBUSDT 1초봉 종가를 사용한다.
정확한 구간·종가·체결 존재를 확인하고 실제 BNB 수량에 Decimal128로 곱한다. 이 값은 **프로그램의
USDT 평가 비용**이며 Binance 내부 환산액이나 실제 USDT 출금액이라고 주장하지 않는다.
시간 구간과 환율·정책 식별자를 원 체결 ID·시각에 결속해 저장한다. 재시작 시 이미 저장된 거래는
가격을 다시 조회하지 않는다. 미완료 주문의 복구는 원 체결 시각으로 과거 구간을 조회하고 저장된
증거가 있다면 대조한다. 근거 누락·빈 구간·미래/오래된 가격·체결 충돌은 회계를 확정하지 않는다.

BNB 부족으로 한 주문에 BNB와 ETH/USDT가 섞이면 원자산별 fill을 모두 보존한다. 혼합 주문의
legacy scalar는 `fee_asset=MIXED`, `fee_amount=0`을 사용하며 이 0은 수수료 면제가 아니라
단위가 다른 원 수량을 합산하지 않는 표식이다. 실제 자산별 수량은 `fee_fills`와 화면 설명에 있다.
`fee_quote_amount`는 모든 fill의 USDT 평가 합계다.

## 자산 흐름과 손익

- BUY: ETH 수수료만 실제 취득 ETH에서 차감한다. BNB/USDT 수수료 평가 비용은 매수 원가에 더한다.
  ETH 차감량을 다시 비용으로 더하지 않는다.
- SELL: 기존 보유 원가 배분 후 매도 금액에서 BNB/USDT 수수료 평가 비용을 빼서 실현손익을 계산한다.
- Account overlay: 실제 BNB 수수료는 BNB에서 차감한다. USDT 잔액에서 BNB 수량/평가액을 빼지 않는다.
- ETH 수수료 fallback으로 생긴 sub-step 잔여는 기존 승인 장부로 이관하고 정확한 잔여량·원가를 보존한다.
- 기존 10 USDT order/position 제한, 자연 신호, 하루 손실 한도 None, unknown 시 신규 주문 차단은 유지한다.

## 책임과 저장 호환

| 계층 | 책임 |
|---|---|
| `domain/trading/fee_valuation.py` | 불변 평가 정책·시간·Decimal 검증 |
| `adapters/binance/bnb_fee_valuator.py` | fixed live public GET으로 정확한 과거 구간 조회 |
| 기존 REST/WS mapper와 live root | 같은 resolver 사용; FULL의 BNB 체결은 개별 시간이 있는 signed myTrades로 보강 |
| Fill/ExecutionSummary/Position | 혼합 자산의 수량·원가·평가 비용과 중복 체결 처리 |
| `domain/history/fill_record.py`, Trade | JSONL v3의 원 체결·평가 근거 직렬화 및 aggregate 대조 |
| 기존 Controller | terminal persistence·fresh replay·계좌 원자산 overlay·잔여 장부 연결 |
| transport/UI | 원자산별 수수료와 BNB USDT 평가 설명 표시 |
| CSV gateway | CSV v2 23열에 원 Trade version과 `fee_evidence_json` 추가 |

BNB를 포함하는 신규 Trade는 JSONL v3로 기록한다. 기존 v1/v2는 원 의미 그대로 읽고 고쳐 쓰지
않는다. ETH/USDT만 포함하는 신규 거래는 기존 v2를 유지한다. 이전 실행 파일은 v3를 읽지 못하므로
BNB 거래 이후에는 이번 새 패키지를 사용해야 한다. 이력·장부를 삭제해 구버전을 실행하지 않는다.
CSV v2의 첫 21개 열 순서는 유지하고 마지막 두 열을 추가한다. 첫 열 값은 CSV version `2`이고
원 Trade version은 별도 열이다. 기존 CSV v1 golden은 보존하고 v2 golden을 추가했다.

## 검증

- Backend 전체 1,074 실행, 1,064 PASS / 외부 safe skip 10, 38.278s.
- UI 전체 488 PASS / 48 files, 7.66s. TypeScript typecheck PASS.
- 도구 읽기/runtime 회귀 9 PASS.
- 신규 BNB 평가·혼합 원가·변조 거부·REST/WS 일치·CSV 증거·durable 재전송·BNB BUY/혼합 SELL 9개와 실제 Controller
  BUY(BNB+ETH)→STOP SELL(BNB)→JSONL v3→잔여 장부→fresh replay 통합 1개 PASS. 마지막 추가 테스트까지 집중 10개 PASS이며 전체 1,074 실행 후 테스트만 추가했다.
- 최초 통합 fixture가 두 fill 중 한 개만 반환해 terminal 수량 검증에서 실패한 것을 fixture의 실제
  누적 fill 두 개로 수정했다. 손익 기대값도 production과 같은 Decimal128로 비교했다.
- 실제 signed 읽기: `blockers=[]`, `supported_fee_asset=true`, `bnb_fee_valuation=true`,
  `pilot_prerequisites=PASS`, 주문 mutation 0. 이 PASS는 계좌 사전조건이며 Session 8 전체 승인이 아니다.

공식 근거:
[Commission FAQ](https://github.com/binance/binance-spot-api-docs/blob/master/faqs/commission_faq.md),
[REST myTrades 및 klines](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md).


## 새 macOS 패키지와 실제 읽기 검증

Target은 `UI/apps/desktop/src-tauri/target/session7-bnb-baeabcd`다. 이전 residual 패키지는 보존했다.
App tree SHA-256: `6f79dcebfce3b621a4bcde9ccbd27a206d9e7b87d6c3e754045afb1fac262991`
(5 files, 29,018,255 bytes). DMG SHA-256:
`b6651cfb4e0ddf348ec95a23d9b1653e5edbe20c0f955d870b5455101d66783d` (21,140,587 bytes).

Non-hardened ad-hoc strict signature, DMG checksum, read-only mount byte parity PASS.
Secret scan은 2 credential canary / 772 files PASS다. 새 native 앱의 실제 계좌·주문 비활성 연결과
정상 종료, fresh restart 후 LIVE chart와 정상 종료를 확인했다. 최종 owner RELEASED, 앱/sidecar 0.
첫 실행은 macOS Keychain 창에서 대기했고 사용자가 직접 허용한 뒤 진행했다. 실제 BNB 체결은
발생시키지 않았으며 양수 수수료·혼합 잔여는 fixture 검증이다. Source 실제 runtime도 잔여 자산을
포함한 13 checks READY/CLOSED PASS, order mutation 0이다. Windows live 검증은 수행하지 않았다.

실행 명령(repo root; Backend unittest는 backend 디렉터리):

```sh
PYTHONPATH=src PYTHONWARNINGS=error .venv/bin/python -m unittest discover -s tests -q
PYTHONPATH=backend/src:backend backend/.venv/bin/python -m unittest tests.unit.trading.test_bnb_fee_accounting tests.integration.test_bnb_stop_flow -q
PYTHONPATH=backend/src backend/.venv/bin/python -m unittest discover -s scripts -p 'test_*live*.py' -q
python3 scripts/check_communication_traceability.py
PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_live_read_only_from_keychain.py --confirm-live LIVE
PYTHONPATH=backend/src backend/.venv/bin/python scripts/live_runtime_readiness.py --confirm-live LIVE
```

Backend 전체 실행에서는 모든 외부 Testnet/order opt-in을 0으로 고정하고 credential 환경변수를
제거했다. UI는 `cd UI && node node_modules/vitest/vitest.mjs run`으로 실행했다.
Communication 126/126 gap 0, diff whitespace PASS. Dependency lockfile은 변경하지 않았다.
Tauri의 pnpm 버전 조회는 제한된 네트워크에서 대기해 중단한 뒤 허용된 host 빌드에서 성공했다.
빌드 로그는 `/private/tmp/binance-bnb-build.log`에 있다. 기존 full supply GAP·Windows·master는
별도 미완료로 유지한다. Source는 기준 HEAD 위의 uncommitted 변경이며 새 commit은 만들지 않았다.
