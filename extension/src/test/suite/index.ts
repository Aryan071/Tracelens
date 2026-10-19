import { runAll } from "./harness";

export async function run(): Promise<void> {
  if (process.env.TRACELENS_TEST_MODE === "live") await import("./live.test");
  else await import("./stub.test");
  await runAll();
}
