const REACT_COMPONENT_TRACK = 'Components ⚛';
const REACT_SCHEDULER_TRACK_GROUP = 'Scheduler ⚛';


/**
 * 함수 이름: is_react_performance_measure()
 * 기능: React 개발 빌드가 만드는 성능 측정만 DevTools track 식별자로 구분한다.
 * 인자: entry -> 브라우저 성능 timeline 항목
 * 반환값: React component 또는 scheduler 측정 여부
 * 작성 날짜: 2026/10/04
 */
function is_react_performance_measure(entry: PerformanceEntry): boolean {
    if (entry.entryType !== 'measure') return false;
    const detail: unknown = (entry as PerformanceMeasure).detail;
    if (typeof detail !== 'object' || detail === null || !('devtools' in detail)) return false;
    const devtools = detail.devtools;
    if (typeof devtools !== 'object' || devtools === null) return false;

    return ('track' in devtools && devtools.track === REACT_COMPONENT_TRACK)
        || ('trackGroup' in devtools && devtools.trackGroup === REACT_SCHEDULER_TRACK_GROUP);
}


/**
 * 함수 이름: clear_react_performance_measures()
 * 기능: 전달된 React 측정의 이름만 정리하고 같은 이름을 쓰는 외부 측정은 보존한다.
 * 인자: entries -> 이번 observer 통지 또는 종료 시 남은 성능 측정
 * 반환값: 없음
 * 작성 날짜: 2026/10/04
 */
function clear_react_performance_measures(entries: readonly PerformanceEntry[]): void {
    const react_names = new Set(entries.filter(is_react_performance_measure).map((entry) => entry.name));

    // clearMeasures는 개별 객체가 아닌 이름 단위이므로 소유권이 겹치는 이름은 지우지 않는다.
    for (const measure_name of react_names) {
        const matching_entries = performance.getEntriesByName(measure_name, 'measure');
        if (matching_entries.length > 0 && matching_entries.every(is_react_performance_measure)) {
            performance.clearMeasures(measure_name);
        }
    }
}


/**
 * 함수 이름: start_react_performance_retention_cleanup()
 * 기능: 개발 중 React prop 차이 문자열이 User Timing 저장소에 계속 누적되지 않게 정리한다.
 * 인자: enabled -> 개발 빌드에서만 활성화하는 실행 조건
 * 반환값: observer와 남은 React 측정을 해제하는 멱등 종료 함수
 * 작성 날짜: 2026/10/04
 */
export function start_react_performance_retention_cleanup(enabled: boolean): () => void {
    // 호출자는 개발 빌드에서만 설치한다. 지원되지 않는 런타임은 기존 렌더링을 유지한다.
    if (!enabled || typeof PerformanceObserver === 'undefined'
        || typeof performance.getEntriesByName !== 'function'
        || typeof performance.clearMeasures !== 'function') return () => {};

    let stopped = false;
    const observer = new PerformanceObserver((entry_list) => {
        if (!stopped) clear_react_performance_measures(entry_list.getEntries());
    });

    try {
        observer.observe({ type: 'measure', buffered: true });
    } catch {
        observer.disconnect();

        return () => {};
    }

    return () => {
        if (stopped) return;
        stopped = true;
        const remaining_entries = observer.takeRecords();
        observer.disconnect();
        clear_react_performance_measures(remaining_entries);
    };
}
