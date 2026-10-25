export type NavigationItem = 'home' | 'ingest' | 'ask' | 'verify' | 'audit';

export type MessageRole = 'user' | 'assistant' | 'system' | 'tool';
export type VerdictLabel = 'supported' | 'contradicted' | 'outdated' | 'unsupported';
export type DecisionAction = 'answer' | 'flag_conflict' | 'correct' | 'qualify' | 'abstain';

export interface IngestMessage {
  id: string;
  session_id: string;
  role: MessageRole;
  text: string;
  timestamp: string;
}

export interface IngestReport {
  added: number;
  superseded: number;
  dropped: number;
}

export interface Verdict {
  claim: string;
  label: VerdictLabel;
  memory_ids: string[];
  rationale: string;
}

export interface Decision {
  action: DecisionAction;
  verdicts: Verdict[];
  final_answer: string | null;
}

export interface AuditRecord {
  id: string;
  timestamp: string;
  question: string;
  answer: string;
  retrieved_ids: string[];
  verdicts: Verdict[];
  decision_action: DecisionAction;
  latency_ms: number;
  prompt_tokens: number | null;
  completion_tokens: number | null;
}

export interface ApiErrorBody {
  error?: string;
  detail?: string;
  request_id?: string;
}

export interface ApiFailure extends Error {
  status: number;
  code?: string;
  requestId?: string;
}
