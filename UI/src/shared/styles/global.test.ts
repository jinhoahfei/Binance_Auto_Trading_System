import global_styles from './global.css?inline';

describe('global reduced-motion contract', () => {
  afterEach(() => {
    // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
    document.querySelector('[data-global-style-contract]')?.remove();
    document.querySelector('[data-reduced-motion-probe]')?.remove();
  });

  it('production CSS가 motion과 transition을 단일 저동작 계약으로 제한한다', () => {
    // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
    const style_element = document.createElement('style');

    style_element.dataset.globalStyleContract = 'true';
    style_element.textContent = global_styles;
    document.head.append(style_element);

    // 실제 production CSSOM에서 reduced-motion media와 공통 selector를 직접 찾는다.
    const style_sheet = style_element.sheet;
    const reduced_motion_rule = style_sheet === null
      ? undefined
      : Array.from(style_sheet.cssRules).find(
        (rule): rule is CSSMediaRule => (
          rule instanceof CSSMediaRule
          && rule.media.mediaText === '(prefers-reduced-motion: reduce)'
        ),
      );
    const universal_rule = reduced_motion_rule === undefined
      ? undefined
      : Array.from(reduced_motion_rule.cssRules).find(
        (rule): rule is CSSStyleRule => (
          rule instanceof CSSStyleRule
          && rule.selectorText.replace(/\s+/gu, ' ') === '*, *::before, *::after'
        ),
      );

    // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
    expect(universal_rule).toBeDefined();
    expect(universal_rule?.style.getPropertyValue('scroll-behavior')).toBe('auto');
    expect(universal_rule?.style.getPropertyPriority('scroll-behavior')).toBe('important');
    expect(universal_rule?.style.getPropertyValue('animation-duration')).toBe('0.01ms');
    expect(universal_rule?.style.getPropertyPriority('animation-duration')).toBe('important');
    expect(universal_rule?.style.getPropertyValue('animation-iteration-count')).toBe('1');
    expect(universal_rule?.style.getPropertyPriority('animation-iteration-count')).toBe('important');
    expect(universal_rule?.style.getPropertyValue('transition-duration')).toBe('0.01ms');
    expect(universal_rule?.style.getPropertyPriority('transition-duration')).toBe('important');

    const computed_style_probe = document.createElement('div');

    computed_style_probe.dataset.reducedMotionProbe = 'true';
    computed_style_probe.style.animationDuration = universal_rule!.style.animationDuration;
    computed_style_probe.style.animationIterationCount = universal_rule!.style.animationIterationCount;
    computed_style_probe.style.transitionDuration = universal_rule!.style.transitionDuration;
    document.body.append(computed_style_probe);

    // jsdom이 media query를 선택하지 않으므로 CSSOM 값을 inline에 적용해 computed parsing도 검증한다.
    const computed_style = getComputedStyle(computed_style_probe);

    expect(computed_style.animationDuration).toBe('0.01ms');
    expect(computed_style.animationIterationCount).toBe('1');
    expect(computed_style.transitionDuration).toBe('0.01ms');
  });
});
