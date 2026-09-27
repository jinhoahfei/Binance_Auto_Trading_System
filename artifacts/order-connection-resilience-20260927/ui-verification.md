# UI 연결 복구 수정 검증 — 2026-09-27

기존 화면과 코딩 컨벤션 유지 조건으로 R4 연결 재시도 변경을 검증했다. UI 제품 변경은 `UI/src/shared/api/BackendUiAdapter.ts`이며 나머지 UI 변경은 해당 어댑터 테스트 2개다.

## 자동 검증

- 전체 Vitest: 60개 파일, 686개 테스트 중 sandbox 실행에서 59개 파일/684개 통과. 실제 localhost Python fixture 테스트 2개는 sandbox 로컬 포트 제한으로 descriptor를 받지 못해 실패했다.
- 같은 process fixture 파일을 로컬 포트 사용이 가능한 환경에서 다시 실행하여 2개 모두 통과했다. 총 검증 대상 686개가 통과했다.
- TypeScript `tsc -b --pretty false`: 종료 코드 0.
- 검증 명령: `node node_modules/vitest/vitest.mjs run`, `node node_modules/vitest/vitest.mjs run src/app/bootstrap/createLiveUiApplication.process.test.mjs`, `node node_modules/typescript/bin/tsc -b --pretty false`.
- 실제 거래소 연결 및 실주문을 사용하지 않았다. Process fixture는 가짜 거래소 데이터와 로컬 HTTP 서버를 사용했다.

## 화면 보존 증거

- HEAD의 UI 추적 파일 374개를 현재 파일과 byte 단위로 대조했다. 어댑터와 테스트 2개를 제외한 371개가 동일하다. JSX, CSS, 디자인 토큰, 폰트, 아이콘, 문구를 소유하는 화면 컴포넌트, fixture, 이미지, Storybook 설정을 변경하지 않았다.
- 수정 전 HEAD를 별도 임시 디렉터리에 복원하고 수정 전/후 Storybook을 각각 새로 빌드했다. 과거 Figma 캡처를 이번 결과로 재사용하지 않았다.
- 두 빌드의 기존 FigmaFrameHarness 16개 상태를 동일한 고정 fixture와 1440×1024 viewport에서 새로 캡처했다. 실시간 지표, 체결 내역, 지표 설정, 거래 내역, 매매 시작/중지 확인, CSV 달력/오류, REGIME 확인/강조, 빈 내역, 로딩 화면을 포함한다.
- 16개 전후 쌍 모두 표시 문구가 동일하며, 최종 JPEG 파일 SHA-256과 디코딩한 RGB 픽셀이 완전히 동일하다. 모든 쌍의 달라진 픽셀 수는 0이다.
- 로딩 상태의 기존 pulse 애니메이션은 두 임시 정적 빌드의 iframe에만 동일한 `animation-play-state: paused; animation-delay: 0s; transition: none` 캡처 조건을 추가하여 같은 시점을 비교했다. 저장소 제품/Storybook 소스나 visual baseline/비교 기준은 수정하지 않았다. 나머지 15개 상태는 원래 fixture 그대로 byte 단위로 동일했다.
- 브라우저의 임시 viewport override를 해제하고 검증용 탭과 로컬 정적 서버를 종료했다.

소스 증거: `ui-source-preservation.json`. 화면 증거: `ui-captures/`의 32개 JPEG, `capture-state.json`, `pixel-comparison.json`. 실행 로그는 이 문서와 같은 폴더에 있다.

이 검증은 고정 상태에서 기존 화면의 보존과 통신 어댑터 회귀를 확인한다. 실제 거래소 또는 실제 데스크톱 앱 운영 시험을 했다는 의미는 아니다.
