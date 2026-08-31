# Figma visual regression references

`figma/`의 PNG 16개는 2026-08-12에 Figma 파일 `kzt9vOXj0QmPeYxO8SVPax`의 최상위 1440×1024 프레임에서 직접 내보낸 기준 이미지입니다.

[`src/stories/FigmaFrames.stories.tsx`](../src/stories/FigmaFrames.stories.tsx)의
`Figma Frames/Binance Auto Trader` story와 같은 순서로 대응합니다. 모든 story는
공통 `FigmaFrameHarness`에 프레임별 상태 fixture만 주입합니다.

1. 실시간 지표 탭
2. 최근 체결 탭
3. 지표 설정 팝오버
4. 거래 내역 상세
5. 자동매매 시작 확인
6. 포지션 보유 중지 확인
7. 포지션 미보유 중지 확인
8. CSV 종료일 달력
9. CSV 시작일 달력
10. CSV 달력 닫힘
11. REGIME 적용 확인
12. REGIME 미선택 경고
13. REGIME 패널 강조
14. 거래 내역 empty state
15. 자동매매 시작 loading
16. CSV validation error

기준 viewport는 `1440×1024`, `deviceScaleFactor=1`입니다. 날짜와 애니메이션은 fixture로 고정하며 reduced-motion에서도 REGIME 강조 outline을 유지합니다.

`baseline_manifest.json`은 16개 fixture key, PNG 파일명, viewport와 SHA-256을
결합합니다. `backend/tests/unit/scripts/test_phase13_visual_baselines.py`는 manifest와
실제 파일의 개수·이름·PNG 크기·digest를 검증합니다. 이 검사는 기준 이미지 자체의
무결성 증거입니다.

## Actual browser comparison gate

`comparison_policy.json`은 현재 캡처를 DOM viewport `1440×1024`, DPR `1`, explicit
clip `{x:0,y:0,width:1440,height:1024}`로 고정합니다. 기본 browser screenshot이
1404px 너비로 잘리는 경로는 허용하지 않습니다. 현재 browser tool의 실제 output은
PNG가 아닌 baseline 8-bit 3-component JPEG/JFIF이므로 `.jpg` signature, 크기와
`current_capture_manifest.json` SHA-256을 모두 검증합니다.

비교기는 local FFmpeg `8.0`을 preflight하고, reference와 current 양쪽에 동일한
`gblur=sigma=0.5:steps=1`과 `yuv444p` 변환을 적용해 1px anti-aliasing·JPEG sampling
차이를 대칭 정규화합니다. 그 뒤 FFmpeg `ssim`의 `All` 값이 각 frame에서
`0.980000` 이상이어야 PASS입니다. 평균으로 낮은 frame을 숨기지 않습니다.

Repository에 보존한 actual capture를 비교하는 기본 실행은 다음과 같습니다.

```text
PYTHONPATH=. backend/.venv/bin/python scripts/check_phase13_visual_regression.py
```

Checker에는 baseline 생성·교체 option이 없고 policy의
`automatic_baseline_update=false`가 아니면 실행을 거부합니다. `check_all.sh`는 repository
내 `UI/visual-regression/current/`의 actual JPEG 16개를 고정 evidence directory로 사용하며,
파일이 없거나 추가됐거나 digest·format·viewport가 다르면 fail closed합니다. Baseline 변경은
별도의 명시적 review와 manifest digest 갱신으로만 수행합니다.

2026-08-29 최종 fresh capture는 SSIM `0.914102~0.981147`, `4/16` PASS와
`12/16` FAIL이므로 visual gate는 `NO_GO`입니다. Threshold와 baseline은 변경하지 않았습니다.
같은 16개 상태에서 Storybook addon-a11y를 각각 재실행한 실제 browser 결과는 layout 기반
color contrast를 포함해 `16/16 Violations 0`이며, 이는 SSIM pixel gate와 별도 증거입니다.
