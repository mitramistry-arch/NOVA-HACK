import { afterEach, describe, expect, it, vi } from 'vitest';
import { api, readableApiError } from '../src/api';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('API client', () => {
  it('maps ingestion responses without inventing counts', async () => {
    const response = { added: 2, superseded: 1, dropped: 3 };
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(response), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    const result = await api.ingest('session-1', [{
      id: 'm1', session_id: 'session-1', role: 'user', text: 'Original text', timestamp: '2026-01-01T10:00:00.000Z',
    }]);
    expect(result).toEqual(response);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toEqual({
      session_id: 'session-1',
      messages: [{ id: 'm1', session_id: 'session-1', role: 'user', text: 'Original text', timestamp: '2026-01-01T10:00:00.000Z' }],
    });
    expect(init.credentials).toBe('same-origin');
  });

  it('does not retry or hide API authorization errors', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: 'unauthorized', detail: 'Invalid or missing API key' }), { status: 401 }));
    vi.stubGlobal('fetch', fetchMock);
    await expect(api.health()).rejects.toThrow(/Authentication is required/);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('uses a neutral not-found message for audit lookups', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: 'http_error', detail: 'Audit record not found' }), { status: 404 }));
    vi.stubGlobal('fetch', fetchMock);
    await expect(api.audit('missing')).rejects.toThrow(/No audit record was found/);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('keeps validation errors readable without returning arbitrary response text', () => {
    expect(readableApiError(422)).toMatch(/invalid/i);
    expect(readableApiError(503, 'auth_not_configured')).toMatch(/not safely configured/i);
    expect(readableApiError(503)).toMatch(/service could not complete/i);
  });
});

describe('API response contract validation', () => {
  it('rejects malformed success payloads instead of rendering invented defaults', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ added: 'two', superseded: 0, dropped: 0 }), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    await expect(api.ingest('session-1', [])).rejects.toThrow(/does not match the supported API contract/i);
  });

  it('rejects unknown verdict labels instead of treating them as a known verdict', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      action: 'qualify',
      verdicts: [{ claim: 'A claim', label: 'probably', memory_ids: [], rationale: '' }],
      final_answer: null,
    }), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    await expect(api.verify('Question?', 'Answer.')).rejects.toThrow(/does not match the supported API contract/i);
  });

  it('handles invalid JSON without misreporting a network outage', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response('not-json', { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    await expect(api.health()).rejects.toThrow(/does not match the supported API contract/i);
  });

  it('does not surface server detail text in client errors', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: 'validation_error', detail: 'internal-path /srv/private' }), { status: 422 }));
    vi.stubGlobal('fetch', fetchMock);
    try {
      await api.verify('Question?', 'Answer.');
      throw new Error('Expected a validation error.');
    } catch (error) {
      expect(error).toBeInstanceOf(Error);
      expect((error as Error).message).toMatch(/required fields/i);
      expect((error as Error).message).not.toContain('internal-path');
    }
  });
});
