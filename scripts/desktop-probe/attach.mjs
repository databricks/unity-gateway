import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { _electron } from 'playwright-core';

const [executablePath, outputDirectory] = process.argv.slice(2);
assert(executablePath && outputDirectory, 'Expected executable path and output directory');
assert.equal(process.platform, 'win32', 'This probe requires a disposable Windows runner');
assert.equal(process.env.GITHUB_ACTIONS, 'true', 'Run on a disposable GitHub Actions runner');
await mkdir(outputDirectory, { recursive: true });

const report = { attached: false, coworkControlFound: false, coworkOpened: false };
let app;
try {
  app = await _electron.launch({
    executablePath,
    args: [`--user-data-dir=${path.join(outputDirectory, 'app-profile')}`],
    timeout: 60_000,
  });
  report.attached = true;
  const window = await app.firstWindow({ timeout: 60_000 });
  await window.waitForLoadState('domcontentloaded');
  await window.locator('body').waitFor({ state: 'visible' });
  await window.screenshot({ path: path.join(outputDirectory, 'launch.png') });
  await writeFile(path.join(outputDirectory, 'launch-ui.txt'), await window.locator('body').innerText());

  for (const role of ['tab', 'button', 'link']) {
    const control = window.getByRole(role, { name: /^cowork$/i });
    if (await control.count() === 1 && await control.isVisible()) {
      report.coworkControlFound = true;
      await control.click({ timeout: 15_000 });
      report.coworkOpened = true;
      break;
    }
  }
  await window.screenshot({ path: path.join(outputDirectory, 'after-cowork.png') });
  await writeFile(path.join(outputDirectory, 'after-cowork-ui.txt'), await window.locator('body').innerText());
  assert(report.coworkOpened, 'Cowork control unavailable; inspect launch UI for onboarding or sign-in');
  report.runtimeValidated = false;
  report.nextCheck = 'Opening the Cowork UI does not establish VM startup or inference; inspect artifacts before adding assertions.';
} catch (error) {
  report.error = error.message;
  process.exitCode = 1;
} finally {
  if (app) {
    try {
      await app.close();
    } catch (error) {
      report.cleanupError = error.message;
      process.exitCode = 1;
    }
  }
  await writeFile(path.join(outputDirectory, 'attachment.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
}
