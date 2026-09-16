import { build } from 'vite';
import { fileURLToPath } from 'node:url';
await build({ configFile: false, root: fileURLToPath(new URL('./background-soak', import.meta.url)),
  build: { outDir: fileURLToPath(new URL('../apps/desktop/soak-dist', import.meta.url)), emptyOutDir: true },
});

if (process.argv.includes('--native')) {
  const { readFile } = await import('node:fs/promises');
  const { spawnSync } = await import('node:child_process');
  const cwd = fileURLToPath(new URL('../apps/desktop/src-tauri', import.meta.url));
  const config = await readFile(new URL('../apps/desktop/src-tauri/tauri.soak.conf.json', import.meta.url), 'utf8');
  const result = spawnSync('cargo', ['build', '--offline', '--features', 'background-liveness-smoke', '--bin', 'background-liveness-soak'],
    { cwd, stdio: 'inherit', env: { ...process.env, TAURI_CONFIG: config } });
  process.exitCode = result.status ?? 1;
}
