// A 30-line test runner for the extension host (avoids a mocha dependency).
type Fn = () => Promise<void> | void;
const tests: { name: string; fn: Fn; only?: string }[] = [];

export function test(name: string, fn: Fn): void {
  tests.push({ name, fn });
}

export async function runAll(): Promise<void> {
  let failed = 0;
  for (const t of tests) {
    const t0 = Date.now();
    try {
      await t.fn();
      console.log(`  ok   ${t.name} (${Date.now() - t0} ms)`);
    } catch (e) {
      failed++;
      console.error(`  FAIL ${t.name}\n       ${e instanceof Error ? (e.stack ?? e.message) : String(e)}`);
    }
  }
  console.log(`\n${tests.length - failed}/${tests.length} editor tests passed`);
  if (failed) throw new Error(`${failed} editor test(s) failed`);
}
