import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import path from 'node:path';
import { chromium } from 'playwright-core';

const [endpoint, outputDirectory] = process.argv.slice(2);
assert.equal(process.env.GITHUB_ACTIONS, 'true');
assert.equal(endpoint, 'http://127.0.0.1:9222');
const report = { attached: false, rendererInspected: false };
let browser;
try {
  browser = await chromium.connectOverCDP(endpoint, { timeout: 30_000 });
  report.attached = true;
  const deadline = Date.now() + 30_000;
  let page;
  while (!page && Date.now() < deadline) {
    page = browser.contexts().flatMap(context => context.pages())[0];
    if (!page) await new Promise(resolve => setTimeout(resolve, 500));
  }
  assert(page, 'No app renderer exposed through CDP');
  await page.locator('body').waitFor({ state: 'visible', timeout: 30_000 });
  report.title = await page.title();
  report.buttonCount = await page.getByRole('button').count();
  report.rendererInspected = true;
} catch (error) {
  report.error = error.message;
  process.exitCode = 1;
} finally {
  if (browser) await browser.close();
  await writeFile(path.join(outputDirectory, 'attachment.json'), JSON.stringify(report, null, 2));
}
