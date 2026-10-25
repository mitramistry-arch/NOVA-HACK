import { expect, test } from '@playwright/test';

test('primary workflows are reachable and mobile navigation is accessible', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  await expect(page.getByRole('heading', { name: /keep answers anchored to evidence/i })).toBeVisible();
  await expect(page.getByText(/the default development integration uses/i)).toBeVisible();

  await page.getByRole('button', { name: 'Open navigation' }).click();
  await expect(page.getByRole('navigation', { name: 'Primary navigation' })).toBeVisible();
  await page.getByRole('navigation', { name: 'Primary navigation' }).getByRole('button', { name: /verify an answer/i }).click();
  await expect(page.getByRole('heading', { name: 'Review a proposed answer' })).toBeVisible();
  await expect(page.getByLabel('Original question')).toBeVisible();
  await expect(page.getByLabel('Draft answer')).toBeVisible();
});

test('verification screen renders real response fields and explicit verdicts', async ({ page }) => {
  await page.route('**/api/healthz', (route) => route.fulfill({ json: { status: 'ok' } }));
  await page.route('**/api/verify', (route) => route.fulfill({
    status: 200,
    json: {
      action: 'qualify',
      final_answer: 'The plan needs qualification.',
      verdicts: [
        { claim: 'The database is PostgreSQL.', label: 'supported', memory_ids: ['memory-7'], rationale: 'Matched a current memory.' },
        { claim: 'The work is complete.', label: 'unsupported', memory_ids: [], rationale: 'No current evidence supports this.' },
      ],
    },
  }));
  await page.goto('/');
  await page.getByRole('navigation', { name: 'Primary navigation' }).getByRole('button', { name: /verify an answer/i }).click();
  await page.getByLabel('Original question').fill('What database was chosen?');
  await page.getByLabel('Draft answer').fill('PostgreSQL was selected and the work is complete.');
  await page.getByRole('button', { name: 'Verify answer' }).click();
  await expect(page.getByText('Supported', { exact: true })).toBeVisible();
  await expect(page.getByText('Unsupported', { exact: true })).toBeVisible();
  await expect(page.getByText('memory-7')).toBeVisible();
  await expect(page.getByText(/source quotations are not included/i)).toBeVisible();
});

test('local FastAPI verification endpoint returns the actual decision contract', async ({ request }) => {
  const response = await request.post('/api/verify', {
    data: {
      question: 'Which database is used?',
      answer: 'The database is PostgreSQL.',
    },
  });
  expect(response.status()).toBe(200);
  const payload = await response.json() as { action?: string; verdicts?: unknown[]; final_answer?: string | null; };
  expect(typeof payload.action).toBe('string');
  expect(Array.isArray(payload.verdicts)).toBe(true);
  expect('final_answer' in payload).toBe(true);
});

test('ingestion submits original fields and displays the API counts', async ({ page }) => {
  await page.route('**/api/healthz', (route) => route.fulfill({ json: { status: 'ok' } }));
  await page.route('**/api/ingest', async (route) => {
    const body = route.request().postDataJSON() as { session_id: string; messages: { id: string; text: string; timestamp: string; }[]; };
    expect(body.session_id).toBeTruthy();
    expect(body.messages).toHaveLength(1);
    expect(body.messages[0].text).toBe('We agreed to review the migration plan.');
    expect(body.messages[0].timestamp).toMatch(/Z$/);
    await route.fulfill({ status: 200, json: { added: 2, superseded: 1, dropped: 0 } });
  });
  await page.goto('/');
  await page.getByRole('navigation', { name: 'Primary navigation' }).getByRole('button', { name: /ingest conversation/i }).click();
  await page.getByLabel(/message text/i).fill('We agreed to review the migration plan.');
  await page.getByRole('form', { name: 'Ingest conversation form' }).getByRole('button', { name: /ingest conversation/i }).click();
  await expect(page.getByText('Ingestion request completed')).toBeVisible();
  await expect(page.getByText('Older values replaced')).toBeVisible();
  await expect(page.getByText('Candidates not stored')).toBeVisible();
});

test('ask workflow shows the response and claim rationale returned by the API', async ({ page }) => {
  await page.route('**/api/healthz', (route) => route.fulfill({ json: { status: 'ok' } }));
  await page.route('**/api/ask', (route) => route.fulfill({
    status: 200,
    json: {
      action: 'answer',
      final_answer: 'The current plan uses PostgreSQL.',
      verdicts: [{ claim: 'The current plan uses PostgreSQL.', label: 'supported', memory_ids: ['memory-22'], rationale: 'A current memory contains the database decision.' }],
    },
  }));
  await page.goto('/');
  await page.getByRole('navigation', { name: 'Primary navigation' }).getByRole('button', { name: /ask a question/i }).click();
  await page.getByLabel('Question').fill('What database is in the current plan?');
  await page.getByRole('form', { name: 'Ask question form' }).getByRole('button', { name: /ask nova/i }).click();
  await expect(page.getByText('The current plan uses PostgreSQL.').first()).toBeVisible();
  await expect(page.getByText('memory-22')).toBeVisible();
  await expect(page.getByText('A current memory contains the database decision.')).toBeVisible();
});

test('audit lookup shows a real record contract with sensitive text collapsed by default', async ({ page }) => {
  await page.route('**/api/healthz', (route) => route.fulfill({ json: { status: 'ok' } }));
  await page.route('**/api/audit/audit-42', (route) => route.fulfill({
    status: 200,
    json: {
      id: 'audit-42', timestamp: '2026-08-12T09:30:00Z', question: 'A sensitive question text.', answer: 'A sensitive recorded answer.',
      retrieved_ids: ['memory-13'], verdicts: [{ claim: 'A claim.', label: 'unsupported', memory_ids: [], rationale: 'No evidence was found.' }],
      decision_action: 'abstain', latency_ms: 14.2, prompt_tokens: null, completion_tokens: null,
    },
  }));
  await page.goto('/');
  await page.getByRole('navigation', { name: 'Primary navigation' }).getByRole('button', { name: /audit lookup/i }).click();
  await page.getByLabel('Audit record ID').fill('audit-42');
  await page.getByRole('form', { name: 'Audit lookup form' }).getByRole('button', { name: /look up record/i }).click();
  await expect(page.getByText('audit-42')).toBeVisible();
  await expect(page.getByText('memory-13')).toBeVisible();
  await expect(page.getByText('A sensitive question text.')).not.toBeVisible();
  await page.getByRole('button', { name: /recorded question and answer/i }).click();
  await expect(page.getByText('A sensitive question text.')).toBeVisible();
});

test('provider note is explicit about FakeLLM and the UI does not store credentials', async ({ page }) => {
  await page.route('**/api/healthz', (route) => route.fulfill({ json: { status: 'ok' } }));
  await page.goto('/');
  await page.getByRole('button', { name: 'Open provider information' }).click();
  await expect(page.getByRole('dialog', { name: /know what is—and isn't—configured/i })).toBeVisible();
  await expect(page.getByText(/FakeLLM.*not a real language model/i)).toBeVisible();
  expect(await page.evaluate(() => ({ local: localStorage.length, session: sessionStorage.length }))).toEqual({ local: 0, session: 0 });
});

test('authorization errors are clear and do not erase the draft', async ({ page }) => {
  await page.route('**/api/healthz', (route) => route.fulfill({ json: { status: 'ok' } }));
  await page.route('**/api/verify', (route) => route.fulfill({ status: 401, json: { error: 'unauthorized', detail: 'Invalid or missing API key' } }));
  await page.goto('/');
  await page.getByRole('navigation', { name: 'Primary navigation' }).getByRole('button', { name: /verify an answer/i }).click();
  await page.getByLabel('Original question').fill('What was the decision?');
  await page.getByLabel('Draft answer').fill('The draft should remain after a failure.');
  await page.getByRole('form', { name: 'Verify answer form' }).getByRole('button', { name: /verify answer/i }).click();
  await expect(page.getByRole('alert')).toContainText(/Authentication is required/i);
  await expect(page.getByLabel('Draft answer')).toHaveValue('The draft should remain after a failure.');
});


test('network failures show a safe message and preserve entered text', async ({ page }) => {
  await page.route('**/api/healthz', (route) => route.fulfill({ json: { status: 'ok' } }));
  await page.route('**/api/verify', (route) => route.abort());
  await page.goto('/');
  await page.getByRole('navigation', { name: 'Primary navigation' }).getByRole('button', { name: /verify an answer/i }).click();
  await page.getByLabel('Original question').fill('Which decision is current?');
  await page.getByLabel('Draft answer').fill('Preserve this draft if the network goes down.');
  await page.getByRole('form', { name: 'Verify answer form' }).getByRole('button', { name: /verify answer/i }).click();
  await expect(page.getByRole('alert')).toContainText(/Could not reach Nova/i);
  await expect(page.getByLabel('Draft answer')).toHaveValue('Preserve this draft if the network goes down.');
});
