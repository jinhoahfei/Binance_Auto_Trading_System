# UI 코딩 컨벤션 주석 보완 및 동작 보존 검증

검증 날짜: 2026-09-17

변경 전 기준 커밋: `620a929c8dee1d183b1d058268259e699ff19fbc`

## 수정 범위

UI 소스 47개 파일에서 주석과 공백만 수정했다. 클래스·함수·메서드 설명 170개를 추가하거나 필수 항목을 완성했고, 기존 설명 중 실제 인자나 현재 Controller 구조와 맞지 않던 내용을 바로잡았다.

`CODING_CONVENTIONS.md`의 이름·기능·인자·반환값·작성 날짜 항목을 TypeScript/JavaScript의 `/** */`와 Rust의 `///`에 적용했다. 클래스 설명에는 클래스 이름·기능·작성 날짜를 기재했다. 수정 파일의 최상위 함수와 클래스 사이, 메서드 사이의 빈 줄도 정리했다.

- `UISTM`: 순수 초기 평가, 날짜 사전 평가, 이전 상태 전달, 초기 요청의 단일 반환, final과 stop의 차이, Action 추출을 설명했다.
- `UIStateController`: 외부 시간 입력, 직렬 이벤트 큐, 재진입 처리, 요청 실행 후 발행, 실행 수명 정리를 설명했다.
- `UiCommandExecutor`: 요청 순서, 동일 작업 교체, 읽기 취소, 절대 만료 시각, 늦은 완료 차단, Port 연결을 설명했다.
- Adapter·진단·차트·테스트 도우미·네이티브 실행부의 누락된 함수 설명과 주요 작업 단위 설명을 보완했다.

식별자, 함수 시그니처, 상태 전이, 가드, 명령 인자, 타이머 값, 테스트 기대값은 유지했다. Python 전용 문법을 다른 언어에 적용하거나 framework/API의 이름을 바꾸지 않았다. 생성 코드, 타입 선언 전용 파일, 외부 의존성은 주석 보완 대상에서 제외했다.

## 주석 검사

| 검사 | 대상 | 결과 |
| --- | --- | --- |
| TypeScript/JavaScript | 233개 파일의 클래스·메서드·이름 있는 함수 등 676개 선언 | 필수 설명 항목 누락 0개 |
| Rust | 23개 파일의 함수·메서드 271개 | 필수 설명 항목 누락 0개 |
| 이번 보완 | TypeScript/JavaScript 126개, Rust 44개 선언 | 총 170개 |

TypeScript/JavaScript 검사는 Babel 구문 분석으로 선언을 찾고 바로 앞 문서 주석을 검사했다. 익명 클래스는 포함하며, 호출 인자로 전달하는 모든 익명 callback에 개별 함수 문서를 강제하지는 않았다. Rust 검사는 함수 선언과 그 항목의 속성 앞 문서 주석을 확인했다. 이 검사는 설명 항목의 존재를 확인하는 검사이며 모든 문서의 의미를 자동 증명하는 검사는 아니다.

## 변경 전후 비교

| 검사 | 변경 전 | 변경 후 |
| --- | --- | --- |
| UI 전체 테스트 | 60개 파일, 660개 통과 | 60개 파일, 660개 통과 |
| Rust 라이브러리 테스트 | 59개 통과 | 59개 통과 |
| TypeScript 타입 검사 | 통과 | 통과 |
| Production build | 통과 | 통과 |
| TypeScript/JavaScript 실행 AST | 기준 233개 파일 | 233개 모두 동일 |
| Rust 코드 토큰 | 기준 23개 파일 | 23개 모두 동일 |
| Production 산출물 | 612개 파일 | 파일 목록과 모든 파일의 SHA-256 동일 |
| 공백 오류 검사 | 기준 작업 트리 clean | `git diff --check` 통과 |

UI 전체 테스트에는 순수 STM, Controller·실행부, 아키텍처 경계, 기존 비동기 시나리오 replay 및 로컬 backend process fixture 연동 검사가 포함된다. 기존 테스트와 replay 기대값을 삭제하거나 완화하지 않았다.

TypeScript/JavaScript는 변경 전 소스와 변경 후 소스를 동일한 Babel parser로 파싱하여 주석·소스 위치·parser 부가 정보를 제외한 AST를 비교했다. Rust는 `proc-macro2`로 파싱하여 문서 속성만 제외하고 식별자·리터럴·구두점·그룹 구조를 비교했다. 두 비교 모두 차이가 없었다.

빌드 결과는 각 파일의 상대 경로와 SHA-256을 비교했다. 정렬한 manifest의 SHA-256도 변경 전후 모두 다음 값이다.

```text
04ef1127c0b462ac5a4958056ceee96fc0f2e45fb49e3450b1080ef87bbd2537
```

실행 코드는 동일하며, 회귀 테스트와 배포용 결과물에서도 동작 변경의 근거가 발견되지 않았다. 주석 삽입으로 개발 소스의 행 번호는 이동한다. 실제 거래 주문과 실제 창을 사용하는 장시간 desktop 실행은 이번 주석 작업의 검증에 포함하지 않았다. Production build의 기존 500 kB 초과 chunk 경고는 변경 전후 동일하게 남아 있다.

## 실행한 검증 명령

`UI` 디렉터리에서 설치된 도구를 직접 실행했다.

```sh
node node_modules/vitest/vitest.mjs run
node node_modules/typescript/bin/tsc -b --pretty false
node node_modules/vite/bin/vite.js build
```

`UI/apps/desktop/src-tauri` 디렉터리에서 네이티브 테스트를 실행했다.

```sh
cargo test --offline --lib
```

검사 집계와 변경 파일 목록은 [verification.json](verification.json), 변경 전후 실행 결과는 같은 디렉터리의 `baseline-tests.txt`, `after-tests.txt`, `baseline-rust-tests.txt`, `after-rust-tests.txt`에 보관했다.
