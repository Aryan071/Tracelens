// Launches a real VS Code, loads the extension and runs src/test/suite inside it.
//   npm run test:vscode            (VS Code is downloaded once into .vscode-test/)
//   TRACELENS_API_URL=http://localhost:18080 npm run test:vscode     (walkthrough against a live API)
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { runTests } from "@vscode/test-electron";
import { startStub } from "./stub";

async function main(): Promise<void> {
  const live = process.env.TRACELENS_API_URL;
  const stub = live ? undefined : await startStub();
  try {
    await runTests({
      extensionDevelopmentPath: resolve(__dirname, "../.."),
      extensionTestsPath: resolve(__dirname, "suite/index"),
      version: process.env.VSCODE_VERSION ?? "stable",
      launchArgs: [
        process.env.TRACELENS_TEST_WORKSPACE ?? resolve(__dirname, "../../test/workspace"),
        "--user-data-dir", mkdtempSync(join(tmpdir(), "tracelens-vscode-user-")),
        "--extensions-dir", mkdtempSync(join(tmpdir(), "tracelens-vscode-ext-")),
        "--disable-workspace-trust", "--disable-gpu",
      ],
      extensionTestsEnv: {
        TRACELENS_TEST_API: live ?? stub!.url,
        TRACELENS_TEST_MODE: live ? "live" : "stub",
        TRACELENS_TEST_SERVICE: process.env.TRACELENS_SERVICE ?? "orders",
      },
    });
  } finally {
    await stub?.close();
  }
}

main().catch((e) => {
  console.error("VS Code tests failed:", e);
  process.exit(1);
});
