# macOS Session 8 준비 상태

> **2026-09-09 22:27 KST 최신 상태:** 사용자가 다른 봇·수동 거래 배제와 출금 권한 비활성
> 유지를 확인했다. 해당 운영 확인은 완료다. 최신 signed 사전검사도 BNB/USDT 포함 12/12 PASS,
> blockers=[], 주문 0이다. 아래 이전 NO_GO/사용자 확인 대기보다 이번 기록을 우선 적용한다.
> 다음은 사용자 직접 실제 실행과 사후 회계·재시작 검증이며 actual pilot은 아직 미완료다.

> **2026-09-09 22:24 KST 기술 작업 마감:** BNB 동일 구간 최대 4회 읽기와 실패 사유를 보완하고
> Backend/UI/Rust/도구·새 package·native 읽기 재시작을 검증했다. 최신 package는
> `UI/apps/desktop/src-tauri/target/session8-1e7df5f/release/bundle/`다. 이전 Session 7 package 대신
> 이 수정본을 사용한다. 최종 signed 검사는 USDT PASS, BNB `NO_TRADES_AFTER_BOUNDED_READS`로
> NO_GO다. 사용자 계좌 운영 확인은 진행할 수 있지만 실제 시작 전에는 최신 PASS가 필요하다.
> 자세한 증거와 남은 조건은 `MACOS_SESSION8_VALIDATION.md`의 22:24 기록을 따른다.

> **2026-09-09 22:13 KST 재확인:** Spot USDT free `40.45627619`, locked `0`.
> Signed 사전검사 12개 모두 PASS, `blockers=[]`, 주문 0이다. 아래 22:01 NO_GO는 과거
> 관측이다. BNB 평가도 이번에는 PASS지만 지연/무체결 처리 코드 수정은 아직 수행하지 않았다.
> 실제 pilot 완료를 의미하지 않는다. 최신 상세는 `MACOS_SESSION8_VALIDATION.md`를 따른다.

> **최신 관측 우선 — 2026-09-09 22:01 KST:** signed 읽기는 성공했으나 MARKET 잔액·BNB
> 평가 검사가 실패해 현재 pilot 사전조건은 NO_GO다. 주문 비활성 runtime은 독립 2회
> 13/13 PASS. 아래 이전 PASS는 과거 관측이며 actual pilot은 미실행이다. 사용자 사전 승인은
> 확인했으며 추가 승인 대기는 아니다. 최신 결과와 Windows 진입 보류 근거는
> [MACOS_SESSION8_VALIDATION.md](MACOS_SESSION8_VALIDATION.md)를 따른다.

기준일: 2026-09-09. 시작 기준 HEAD는 `baeabcd031c69b6e8448946c2d7511f0baf5cfab`이며,
현재 Session 7·잔여 정책 구현은 아직 commit하지 않은 작업 트리다. 실제 주문 실행 기록은 아니다.

## 승인된 진행 순서

2026-09-09 사용자 지시: **macOS pilot → macOS lifecycle·회계·fresh restart 검증 완료 →
Windows live-readiness 검증 → 별도 승인된 Windows pilot**. Windows 검증은 macOS 시작의
선행 조건이 아니다. 기존 양 OS master 완료 조건은 유지하며 각 단계의 실제 증거로만 체크한다.
이번 변경은 순서 확정이며 실제 주문 profile을 활성화하지 않았다.

## 최신 확인

| 조건 | 결과 |
|---|---|
| signed account/filter/reference/commission 조회 | PASS |
| MARKET 최소 주문 금액 잔액 | PASS |
| ETH 수수료 잔여 정책 | PASS |
| 지원 수수료 자산·BNB 평가 근거 | PASS: BNB 납부 유지 |
| 실제 REST/WS runtime READY와 정상 종료 | PASS |
| 전략 Position·잔여 자산·pending·unknown·거래소 open order/list | 모두 0 |
| reconciliation | clear |
| 주문 비활성 | 유지, 이번 주문 mutation 0 |
| pilot 동안 수동 거래·다른 봇 배제 | 사용자 확인 대기 |
| Windows 별도 계좌와 live 읽기 READY | macOS pilot 검증 완료 후 수행 |

## 수수료 조건 해결

사용자의 후속 승인에 따라 BNB 납부를 유지하는 회계를 구현했다. 이전 OFF 안내는 더 이상 현재
실행 절차가 아니다. [BNB_FEE_ACCOUNTING.md](BNB_FEE_ACCOUNTING.md)의 v3 원 체결·환율 증거·
혼합 수수료·재시작 회계와 새 패키지를 사용한다. 실제 signed 재검사는 `blockers=[]`,
`supported_fee_asset=true`, `bnb_fee_valuation=true`, `pilot_prerequisites=PASS`다.

시작 직전에는 repo root에서 다음 읽기 검증을 다시 실행한다.

```sh
PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_live_read_only_from_keychain.py --confirm-live LIVE
```

이 PASS는 그 시점의 계좌 사전조건이다. macOS 운영 계좌 전용 사용·별도 실제 주문 승인을
대체하지 않는다. 환율 데이터 누락 또는 이후 계좌 설정 변화는 다시 차단될 수 있다.

## 시작 직전 순서

1. macOS pilot 계좌에서 다른 봇과 수동 거래를 중지한다. Windows 별도 계좌 확인은 Windows 검증 단계에서 수행한다.
2. 기존 native app을 정상 종료한다. 검증한 잔여 정책 app/DMG는
   `UI/apps/desktop/src-tauri/target/session7-bnb-baeabcd/release/bundle/` 아래에 있다.
   정확한 digest·서명·smoke 증거는 `MACOS_LIVE_READINESS_VALIDATION.md`를 따른다.
3. 위 signed 사전검사와 아래 독립 runtime 검증을 실행해 현재 상태를 확인한다.

```sh
PYTHONPATH=backend/src backend/.venv/bin/python scripts/live_runtime_readiness.py --confirm-live LIVE
```

4. macOS 기술 조건과 해당 계좌의 10 USDT order/position 실제 주문 승인을 확인한 뒤에만
   native 주문 profile을 활성화한다. 이번 준비 요청으로 profile을 변경하지 않았다.
5. 10 USDT order/position 상한과 자연 strategy signal을 사용한다. 신호가 없으면 기다리며
   fixture나 강제 주문으로 체결을 만들지 않는다. 일일 손실 자동 차단은 설정하지 않은 기존 정책이다.
6. 첫 lifecycle 또는 STOP 뒤 durable Trade·수수료·잔여 장부와 계좌를 대조하고 fresh restart한다.
   전략 Position/pending/unknown/open order/list는 0이어야 하며 잔여가 있으면 실제 수량·원가를
   명시한다. 잔여가 있는 종료를 zero exposure로 기록하지 않는다.
7. Timeout·체결 불명·저장 실패·UI 불일치가 발생하면 새 BUY를 차단하고 복구한다. 반복 클릭이나
   재주문으로 해소하지 않는다. macOS lifecycle/restart가 통과하기 전에 Windows pilot을 시작하지 않는다.

## 검증 명령과 증거의 범위

2026-09-09 위 두 명령을 실제 Keychain·공식 Binance endpoint로 실행했다.
확장 후 첫 명령은 읽기 PASS, `blockers=[]`, BNB 평가 PASS, 주문 0이었다.
두 번째는 residual_assets_zero를 포함한 13개 check 모두 true, runtime_readiness PASS, 주문 0이었다.
이번 BNB 확장 후 Backend 1,064 PASS/10 skip, UI 488 PASS로 재검증했다. 이전 패키지 증거는 보존하고
새 BNB 패키지의 결과는 MACOS_LIVE_READINESS_VALIDATION.md의 후속 기록을 따른다.
현재 macOS Session 8 진입의 기술적 순서 조건은 충족됐다. 실제 주문 시작은 계좌 전용 사용 확인·
최신 사전검사·별도 주문 승인 후 진행한다. Windows 미검증을 macOS 선행 차단으로 재사용하지 않는다.

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
