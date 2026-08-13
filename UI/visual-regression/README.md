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
