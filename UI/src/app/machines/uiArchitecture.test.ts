import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { expect, it } from 'vitest';
import { create_ui_application_machine } from './uiApplicationMachine';

const source_root = path.resolve('src');


/**
 * 함수 이름: runtime_dependencies()
 * 기능: 타입 전용 참조를 제외한 import·재수출을 추적하고 STM의 실행 의존성을 검사한다.
 * 인자: file -> 읽을 TypeScript 소스 경로
 * 반환값: 해석한 로컬 런타임 의존 경로 배열
 * 작성 날짜: 2026/09/17
 */
function runtime_dependencies(file: string): string[] {
    const source = readFileSync(file, 'utf8').replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
    const edges: string[] = [];
    for (const match of source.matchAll(/^(?:import|export)\s+(type\s+)?([\s\S]*?)\sfrom\s*['"]([^'"]+)['"]/gm)) {
        const [, type_only, specifiers, target] = match;
        if (type_only) continue;
        if (specifiers!.startsWith('{') && specifiers!.slice(1, specifiers!.lastIndexOf('}')).split(',').filter(part => part.trim()).every(part => part.trim().startsWith('type '))) continue;
        if (!target!.startsWith('.')) {
            expect(target, `${file}: only XState is allowed in the decision layer`).toBe('xstate');
            continue;
        }

        const base = path.resolve(path.dirname(file), target!);
        const resolved = [base + '.ts', base + '.tsx', path.join(base, 'index.ts')].find(existsSync);
        expect(resolved, `${file}: unresolved runtime dependency ${target}`).toBeDefined();
        edges.push(resolved!);
    }
    expect(source, file).not.toMatch(/\b(?:createActor|fromPromise|fromCallback|sendTo|spawnChild|setTimeout|setInterval|clearTimeout|clearInterval|fetch|WebSocket|AbortController)\s*\(/);
    expect(source, file).not.toMatch(/\b(?:Date\.now|Math\.random|performance\.now)\s*\(|new\s+Date\s*\(\s*\)|\bimport\s*\(/);
    expect(source, file).not.toMatch(/self\.getSnapshot\s*\(|options\.(?:get_current_kst_date|get_summary_revision|on_details_loaded)\s*\??\./);

    return edges;
}

it('keeps every transitive STM dependency inside pure definitions, contracts and error conversion', () => {
    // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
    const pending = ['UISTM.ts', 'uiApplicationMachine.ts'].map(name => path.join(source_root, 'app/machines', name));
    const visited = new Set<string>();
    while (pending.length) {
        const file = pending.pop()!;
        if (visited.has(file)) continue;
        visited.add(file);
        const relative = path.relative(source_root, file);
        expect(relative, 'STM must not depend on Controller, ports, adapters, views or test helpers').toMatch(
            /^(?:app\/machines\/|features\/[^/]+\/machines\/|shared\/(?:contracts|errors)\/)/,
        );
        pending.push(...runtime_dependencies(file));
    }
    expect(visited.size).toBeGreaterThan(20);  // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
});

it('builds a root with no invoked actors or implicit runtime timers', () => {
    // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
    const machine = create_ui_application_machine({ today: '2026-09-17' });

    // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
    expect(Object.keys(machine.implementations.actors)).toEqual([]);

    /**
     * 함수 이름: inspect()
     * 기능: 조립된 모든 상태 노드에 실행 actor·암묵적 타이머·미조립 작업 정의가 없는지 검사한다.
     * 인자: node -> 검사할 상태 정의
     * 반환값: 없음; 금지된 정의가 있으면 assertion 실패
     * 작성 날짜: 2026/09/17
     */
    const inspect = (node: Record<string, any>) => {
        expect(node.invoke).toBeUndefined();
        expect(node.after).toBeUndefined();
        expect(node.meta?.command).toBeUndefined();
        expect(node.meta?.timers).toBeUndefined();
        Object.values(node.states ?? {}).forEach(child => inspect(child as Record<string, any>));
    };
    inspect(machine.config);
});
