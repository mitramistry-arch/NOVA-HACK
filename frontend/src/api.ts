import type {
  ApiErrorBody,
  ApiFailure,
  AuditRecord,
  Decision,
  DecisionAction,
  IngestMessage,
  IngestReport,
  Verdict,
  VerdictLabel,
} from './types';

const API_ROOT = '/api';
const REQUEST_TIMEOUT_MS = 20_000;

const DECISION_ACTIONS = new Set<DecisionAction>([
  'answer',
  'flag_conflict',
  'correct',
  'qualify',
  'abstain',
]);
const VERDICT_LABELS = new Set<VerdictLabel>([
  'supported',
  'contradicted',
  'outdated',
  'unsupported',
]);

class ApiContractError extends Error {
  constructor() {
    super('Nova returned a response that does not match the supported API contract. Check that the frontend and backend versions match.');
    this.name = 'ApiContractError';
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function requireString(value: unknown): string {
  if (typeof value !== 'string') throw new ApiContractError();
  return value;
}

function requireStringArray(value: unknown): string[] {
  if (!Array.isArray(value) || value.some((item) => typeof item !== 'string')) {
    throw new ApiContractError();
  }
  return value;
}

function requireNonNegativeInteger(value: unknown): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0) {
    throw new ApiContractError();
  }
  return value;
}

function parseVerdict(value: unknown): Verdict {
  if (!isRecord(value)) throw new ApiContractError();
  if (typeof value.label !== 'string' || !VERDICT_LABELS.has(value.label as VerdictLabel)) {
    throw new ApiContractError();
  }
  return {
    claim: requireString(value.claim),
    label: value.label as VerdictLabel,
    memory_ids: requireStringArray(value.memory_ids),
    rationale: requireString(value.rationale),
  };
}

function parseDecision(value: unknown): Decision {
  if (!isRecord(value) || typeof value.action !== 'string') throw new ApiContractError();
  if (!DECISION_ACTIONS.has(value.action as DecisionAction) || !Array.isArray(value.verdicts)) {
    throw new ApiContractError();
  }
  if (value.final_answer !== null && typeof value.final_answer !== 'string') {
    throw new ApiContractError();
  }
  return {
    action: value.action as DecisionAction,
    verdicts: value.verdicts.map(parseVerdict),
    final_answer: value.final_answer,
  };
}

function parseIngestReport(value: unknown): IngestReport {
  if (!isRecord(value)) throw new ApiContractError();
  return {
    added: requireNonNegativeInteger(value.added),
    superseded: requireNonNegativeInteger(value.superseded),
    dropped: requireNonNegativeInteger(value.dropped),
  };
}

function parseAuditRecord(value: unknown): AuditRecord {
  if (!isRecord(value) || typeof value.decision_action !== 'string') {
    throw new ApiContractError();
  }
  if (!DECISION_ACTIONS.has(value.decision_action as DecisionAction)) {
    throw new ApiContractError();
  }
  const timestamp = requireString(value.timestamp);
  if (Number.isNaN(Date.parse(timestamp))) throw new ApiContractError();
  const tokens = (tokenValue: unknown): number | null => {
    if (tokenValue === null) return null;
    return requireNonNegativeInteger(tokenValue);
  };
  if (typeof value.latency_ms !== 'number' || !Number.isFinite(value.latency_ms) || value.latency_ms < 0) {
    throw new ApiContractError();
  }
  if (!Array.isArray(value.verdicts)) throw new ApiContractError();

  return {
    id: requireString(value.id),
    timestamp,
    question: requireString(value.question),
    answer: requireString(value.answer),
    retrieved_ids: requireStringArray(value.retrieved_ids),
    verdicts: value.verdicts.map(parseVerdict),
    decision_action: value.decision_action as DecisionAction,
    latency_ms: value.latency_ms,
    prompt_tokens: tokens(value.prompt_tokens),
    completion_tokens: tokens(value.completion_tokens),
  };
}

function parseHealth(value: unknown): { status: string; } {
  if (!isRecord(value) || typeof value.status !== 'string' || value.status.length === 0) {
    throw new ApiContractError();
  }
  return { status: value.status };
}

export function readableApiError(status: number, code?: string): string {
  if (status === 401) return 'Authentication is required. Configure a trusted same-origin session proxy for this deployment.';
  if (status === 403) return 'This session is not authorized to access that resource.';
  if (status === 404) return 'No audit record was found for that ID in your authorized workspace.';
  if (status === 422) return 'Some fields are invalid. Review the required fields and try again.';
  if (status === 429) return 'The service is receiving too many requests. Wait a moment before trying again.';
  if (code === 'auth_not_configured') return 'API authentication is not safely configured on the server.';
  if (status >= 500) return 'The service could not complete the request. Your entered text is still available; please retry when the service is healthy.';
  return 'The request was rejected. Check the input and server configuration, then try again.';
}

function isApiFailure(error: unknown): error is ApiFailure {
  return error instanceof Error && 'status' in error && typeof (error as ApiFailure).status === 'number';
}

async function request<T>(
  path: string,
  decode: (value: unknown) => T,
  init: RequestInit = {},
): Promise<T> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  const headers = new Headers(init.headers);
  headers.set('Accept', 'application/json');
  if (init.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');

  try {
    let response: Response;
    try {
      response = await fetch(`${API_ROOT}${path}`, {
        ...init,
        headers,
        signal: controller.signal,
        credentials: 'same-origin',
        cache: 'no-store',
      });
    } catch (error) {
      if (controller.signal.aborted || (error instanceof DOMException && error.name === 'AbortError')) {
        throw new Error('The request timed out. Your entered text has been kept so you can try again.');
      }
      throw new Error('Could not reach Nova. Check the connection and server status, then try again.');
    }

    if (!response.ok) {
      let body: ApiErrorBody = {};
      try {
        body = (await response.json()) as ApiErrorBody;
      } catch {
        // Never echo an arbitrary error body; it could include sensitive data.
      }
      const failure = new Error(readableApiError(response.status, body.error)) as ApiFailure;
      failure.status = response.status;
      failure.code = body.error;
      failure.requestId = body.request_id;
      throw failure;
    }

    if (response.status === 204) return undefined as T;

    let payload: unknown;
    try {
      payload = await response.json();
    } catch {
      throw new ApiContractError();
    }
    try {
      return decode(payload);
    } catch {
      throw new ApiContractError();
    }
  } catch (error) {
    if (isApiFailure(error)) throw error;
    if (error instanceof ApiContractError) throw error;
    if (error instanceof Error && /request timed out|Could not reach Nova/.test(error.message)) throw error;
    throw new Error('The request could not be completed. Keep your entered text and check the service before retrying.');
  } finally {
    window.clearTimeout(timer);
  }
}

export const api = {
  ingest(sessionId: string, messages: IngestMessage[]) {
    return request('/ingest', parseIngestReport, {
      method: 'POST',
      body: JSON.stringify({ session_id: sessionId, messages }),
    });
  },
  ask(question: string, draftAnswer?: string) {
    return request('/ask', parseDecision, {
      method: 'POST',
      body: JSON.stringify({ question, ...(draftAnswer?.trim() ? { draft_answer: draftAnswer.trim() } : {}) }),
    });
  },
  verify(question: string, answer: string) {
    return request('/verify', parseDecision, {
      method: 'POST',
      body: JSON.stringify({ question, answer }),
    });
  },
  audit(id: string) {
    return request(`/audit/${encodeURIComponent(id.trim())}`, parseAuditRecord);
  },
  health() {
    return request('/healthz', parseHealth);
  },
};
