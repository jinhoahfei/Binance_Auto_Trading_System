import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { expect, it } from 'vitest';
import { create_ui_application_machine } from './uiApplicationMachine';

const source_root = path.resolve('src');

/** type-only 참조는 제외하고 barrel의 재수출까지 실제 런타임 의존 경로를 검사한다. */
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
    expect(visited.size).toBeGreaterThan(20);
});

it('builds a root with no invoked actors or implicit runtime timers', () => {
    const machine = create_ui_application_machine({ today: '2026-09-17' });
    expect(Object.keys(machine.implementations.actors)).toEqual([]);
    const inspect = (node: Record<string, any>) => {
        expect(node.invoke).toBeUndefined();
        expect(node.after).toBeUndefined();
        expect(node.meta?.command).toBeUndefined();
        expect(node.meta?.timers).toBeUndefined();
        Object.values(node.states ?? {}).forEach(child => inspect(child as Record<string, any>));
    };
    inspect(machine.config);
});
