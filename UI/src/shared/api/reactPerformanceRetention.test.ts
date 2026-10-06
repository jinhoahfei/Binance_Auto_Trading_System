import { afterEach, describe, expect, it, vi } from 'vitest';

import { start_react_performance_retention_cleanup } from './reactPerformanceRetention';


/**
 * 함수 이름: install_performance_fixture()
 * 기능: 실제 브라우저 API 계약에 맞는 측정 저장소와 observer 통지를 구성한다.
 * 인자: entries -> 기존 성능 측정 목록
 * 반환값: 통지·종료 검증 함수와 측정 저장소
 * 작성 날짜: 2026/10/04
 */
function install_performance_fixture(entries: PerformanceEntry[]) {
    const measurements = [...entries];
    let notify: PerformanceObserverCallback;
    const disconnect = vi.fn();
    const observe = vi.fn();
    const take_records = vi.fn(() => measurements);
    const clear_measures = vi.fn((name: string) => {
        for (let index = measurements.length - 1; index >= 0; index -= 1) {
            if (measurements[index]?.name === name && measurements[index]?.entryType === 'measure') {
                measurements.splice(index, 1);
            }
        }
    });
    vi.stubGlobal('performance', {
        clearMeasures: clear_measures,
        getEntriesByName: (name: string, entry_type: string) =>
            measurements.filter((entry) => entry.name === name && entry.entryType === entry_type),
    });

    /**
     * 클래스 이름: FixturePerformanceObserver
     * 기능: 성능 측정 callback과 수명 종료 API를 재현한다.
     * 작성 날짜: 2026/10/04
     */
    class FixturePerformanceObserver {
        observe = observe;
        disconnect = disconnect;
        takeRecords = take_records;

        /**
         * 함수 이름: FixturePerformanceObserver.constructor()
         * 기능: 테스트가 통지할 observer callback을 보관한다.
         * 인자: callback -> 성능 항목 관찰 함수
         * 반환값: 생성된 fixture observer
         * 작성 날짜: 2026/10/04
         */
        constructor(callback: PerformanceObserverCallback) {
            notify = callback;
        }
    }
    vi.stubGlobal('PerformanceObserver', FixturePerformanceObserver);

    return {
        clear_measures, disconnect, measurements, observe, take_records,
        notify: () => notify({ getEntries: () => measurements } as PerformanceObserverEntryList,
            {} as PerformanceObserver),
    };
}


/**
 * 함수 이름: create_measure()
 * 기능: 이름·부가 정보·종류가 다른 성능 항목을 생성한다.
 * 인자: name -> 측정 이름, detail -> 부가 정보, entry_type -> 성능 항목 종류
 * 반환값: 테스트용 성능 항목
 * 작성 날짜: 2026/10/04
 */
function create_measure(name: string, detail: unknown, entry_type = 'measure'): PerformanceEntry {
    return { name, detail, entryType: entry_type } as PerformanceMeasure;
}

afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
});

describe('React 개발 성능 측정 보관 정리', () => {
    it('React 측정만 지우고 외부 측정·mark와 이름 충돌은 보존한다', () => {
        const fixture = install_performance_fixture([
            create_measure('\u200bChartCanvas', { devtools: { track: 'Components ⚛' } }),
            create_measure('Update', { devtools: { trackGroup: 'Scheduler ⚛' } }),
            create_measure('request_latency', { elapsed_ms: 5 }),
            create_measure('request_latency', null, 'mark'),
            create_measure('shared_name', { devtools: { track: 'Components ⚛' } }),
            create_measure('shared_name', { application: true }),
        ]);
        const stop = start_react_performance_retention_cleanup(true);

        fixture.notify();

        expect(fixture.observe).toHaveBeenCalledWith({ type: 'measure', buffered: true });
        expect(fixture.clear_measures.mock.calls).toEqual([['\u200bChartCanvas'], ['Update']]);
        expect(fixture.measurements.map((entry) => entry.name)).toEqual([
            'request_latency', 'request_latency', 'shared_name', 'shared_name',
        ]);
        stop();
    });

    it('종료 시 대기 측정을 정리하고 반복 종료 또는 늦은 callback은 처리하지 않는다', () => {
        const fixture = install_performance_fixture([
            create_measure('\u200bPriceChartPanel', { devtools: { track: 'Components ⚛' } }),
        ]);
        const stop = start_react_performance_retention_cleanup(true);

        stop();
        stop();
        fixture.notify();

        expect(fixture.take_records).toHaveBeenCalledTimes(1);
        expect(fixture.disconnect).toHaveBeenCalledTimes(1);
        expect(fixture.clear_measures).toHaveBeenCalledTimes(1);
        expect(fixture.measurements).toEqual([]);
    });

    it('관찰을 지원하지 않는 환경에서도 렌더링을 중단하지 않는다', () => {
        vi.stubGlobal('PerformanceObserver', undefined);

        expect(start_react_performance_retention_cleanup(true)).toBeTypeOf('function');
    });

    it('관찰 등록 실패 시 observer를 해제한다', () => {
        const fixture = install_performance_fixture([]);
        fixture.observe.mockImplementation(() => { throw new Error('unsupported'); });

        expect(start_react_performance_retention_cleanup(true)).toBeTypeOf('function');
        expect(fixture.disconnect).toHaveBeenCalledTimes(1);
    });

    it('배포 빌드에서는 observer를 시작하거나 측정을 변경하지 않는다', () => {
        const fixture = install_performance_fixture([
            create_measure('\u200bChartCanvas', { devtools: { track: 'Components ⚛' } }),
        ]);
        const stop = start_react_performance_retention_cleanup(false);

        stop();

        expect(fixture.observe).not.toHaveBeenCalled();
        expect(fixture.clear_measures).not.toHaveBeenCalled();
        expect(fixture.measurements).toHaveLength(1);
    });
});
