import axe, { type Result as AxeResult } from 'axe-core';
import { cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { FigmaFrameHarness } from './FigmaFrameHarness';
import { FIGMA_FRAME_KEYS, type FigmaFrameKey } from './figmaFrameFixtures';

/**
 * 함수 이름: format_accessibility_violations()
 * 기능: axe 위반 결과를 frame별 실패에서 바로 진단할 수 있는 짧은 문자열로 변환한다.
 * 인자: violations -> axe가 반환한 접근성 위반 목록
 * 반환값: rule ID와 영향받은 selector를 묶은 진단 문자열
 * 작성 날짜: 2026/08/29
 */
function format_accessibility_violations(violations: AxeResult[]): string {
  return violations
    .map((violation) => {
      const selectors = violation.nodes
        .flatMap((node) => node.target)
        .join(', ');

      return `${violation.id}: ${selectors}`;
    })
    .join('\n');
}

/**
 * 함수 이름: scan_frame_accessibility()
 * 기능: 포털을 포함한 한 Figma frame DOM을 axe WCAG A/AA 규칙으로 독립 검사한다.
 * 인자: frame_key -> 검사 결과의 frame 식별에 사용할 결정적 fixture key
 * 반환값: axe가 발견한 접근성 위반 목록
 * 작성 날짜: 2026/08/29
 */
async function scan_frame_accessibility(frame_key: FigmaFrameKey): Promise<AxeResult[]> {
  render(<FigmaFrameHarness frame={frame_key} />);

  // jsdom이 layout을 계산하지 못하는 색 대비만 제외하고 구조적 WCAG 규칙은 모두 실행한다.
  const scan_result = await axe.run(document.body, {
    runOnly: {
      type: 'tag',
      values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'],
    },
    rules: {
      'color-contrast': { enabled: false },
    },
  });

  return scan_result.violations;
}

describe('FigmaFrameHarness accessibility', () => {
  afterEach(() => {
    cleanup();
  });

  for (const frame_key of FIGMA_FRAME_KEYS) {
    it(`passes an independent axe scan for ${frame_key}`, async () => {
      // 수동 ARIA assertion과 분리된 axe engine으로 16개 fixture를 모두 같은 규칙으로 검사한다.
      const violations = await scan_frame_accessibility(frame_key);

      expect(
        violations,
        format_accessibility_violations(violations),
      ).toEqual([]);
    });
  }
});
