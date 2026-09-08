# Lower BB 수정 앱 재빌드 및 설치

2026-09-09 사용자 요청에 따라 코드 보완 이후의 macOS 앱을 재빌드하고 설치했다.

- 소스: `e75b9e6` (`Trading STM Review`). 빌드 시작 시 작업 트리 clean.
- 설치 경로: `/Applications/Binance Auto Trader.app`.
- 기존 `/Applications` 및 사용자 Applications에는 해당 앱이 없었고, Spotlight에는 프로젝트의 빌드 폴더 앱들만 등록되어 있었다. 실행 중인 Binance 앱/사이드카는 없었다.
- 현재 `target/release/bundle/macos/Binance Auto Trader.app`을 재빌드하고 `/Applications`에 복사한 뒤 LaunchServices에 등록했다. 이전 session별 빌드 폴더와 DMG는 유지했다. 앞으로 실행할 최신 설치본은 위 `/Applications` 경로다.
- Python sidecar(PyInstaller), TypeScript, Vite, Rust/Tauri release 빌드 모두 성공했다. 기존 개인용 macOS 패키지와 같은 non-hardened ad-hoc 서명을 적용했다. 앱 버전은 기존 `0.1.0`이며 정확한 내용 식별에는 아래 파일 해시를 사용한다.
- 빌드본과 설치본 `codesign --verify --deep --strict` 통과. `diff -qr`로 두 앱의 파일 내용 일치를 확인했다.
- 패키지 내부 PYZ의 production 모듈 108개를 현재 소스를 컴파일한 코드 객체와 비교해 모두 일치했다. 비교 시 파일 경로만 정규화했다. Lower BB/STM 수정이 실제 배포용 sidecar에 포함된 것을 확인했다.
- 설치된 sidecar의 로더 검사 통과: credential 및 설정 FD를 전달하지 않고 실행해 정상 로딩 후 필수 FD 부재(EBADF)로 중단하는 기존 검증 함수를 사용했다. UI 전체 기동/계좌 연결 검사는 실행하지 않았다.
- 실제 주문, 키 조회, 주문 profile 변경 및 앱 데이터 변경은 하지 않았다. 앱 설치가 실제 주문 시작을 의미하지 않는다.

증거:

- `lower_bb_installed_app_build_result.txt`: 전체 빌드 로그
- `lower_bb_packaged_modules_result.txt`: 현재 소스와 일치하는 패키지 모듈 목록
- `lower_bb_installed_app_verification.json`: 설치 경로, 전체 소스 commit, 검증 결과, 설치 앱 파일별 SHA-256

이 기록으로 이전 [코드 보완 보고서](Lower_bb_remediation_2026-09-09.md)의 “설치된 앱 재빌드를 수행하지 않았다”는 후속 작업은 완료되었다. 기존 전략·실주문 검증 범위는 그대로다.
