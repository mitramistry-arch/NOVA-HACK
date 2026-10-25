import { useEffect, useMemo, useRef, useState } from 'react';
import type { ButtonHTMLAttributes, FormEvent, ReactNode } from 'react';
import {
  Activity,
  AlertCircle,
  ArrowDownLeft,
  ArrowRight,
  BookOpenCheck,
  Check,
  CheckCircle2,
  ChevronDown,
  CircleHelp,
  Clock3,
  FileSearch,
  FileText,
  Fingerprint,
  Home,
  LoaderCircle,
  Menu,
  MessageSquareText,
  Plus,
  Search,
  ShieldCheck,
  ShieldQuestion,
  Sparkles,
  X,
} from 'lucide-react';
import { api } from './api';
import type {
  AuditRecord,
  Decision,
  DecisionAction,
  IngestMessage,
  IngestReport,
  MessageRole,
  NavigationItem,
  Verdict,
  VerdictLabel,
} from './types';

const NAV_ITEMS: { id: NavigationItem; label: string; icon: typeof Home; }[] = [
  { id: 'home', label: 'Overview', icon: Home },
  { id: 'ingest', label: 'Ingest conversation', icon: ArrowDownLeft },
  { id: 'ask', label: 'Ask a question', icon: MessageSquareText },
  { id: 'verify', label: 'Verify an answer', icon: ShieldCheck },
  { id: 'audit', label: 'Audit lookup', icon: FileSearch },
];

const PAGES: Record<NavigationItem, { eyebrow: string; title: string; subtitle: string; }> = {
  home: {
    eyebrow: 'Your knowledge workspace',
    title: 'Keep answers anchored to evidence.',
    subtitle: 'Capture conversation context, ask grounded questions, and inspect how a decision was reached.',
  },
  ingest: {
    eyebrow: 'Capture context',
    title: 'Ingest a conversation',
    subtitle: 'Add the original message text and timestamps. Nova extracts only memories that can be traced back to a source.',
  },
  ask: {
    eyebrow: 'Retrieve and reason',
    title: 'Ask your workspace',
    subtitle: 'Get an answer checked against retrieved memories. Missing evidence should stay missing—not become a guess.',
  },
  verify: {
    eyebrow: 'Inspect a draft',
    title: 'Verify an answer',
    subtitle: 'Submit a question and draft answer to see claim-level verdicts and the evidence references behind them.',
  },
  audit: {
    eyebrow: 'Trace a decision',
    title: 'Look up an audit record',
    subtitle: 'Use the exact audit ID returned by a previous request. Records are scoped by the backend authorization policy.',
  },
};

const VERDICT_LABELS: Record<VerdictLabel, string> = {
  supported: 'Supported',
  outdated: 'Outdated',
  contradicted: 'Contradicted',
  unsupported: 'Unsupported',
};

const ACTION_LABELS: Record<DecisionAction, string> = {
  answer: 'Answer',
  flag_conflict: 'Conflict flagged',
  correct: 'Correction suggested',
  qualify: 'Qualified answer',
  abstain: 'Abstained',
};

function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  return 'Something went wrong. Please try again.';
}

function currentTimestamp(): string {
  return new Date().toISOString();
}

type MessageDraft = IngestMessage & { rowKey: string; };

function newMessage(sessionId: string, role: MessageRole = 'user'): MessageDraft {
  const unique = globalThis.crypto?.randomUUID?.() ?? `msg-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  return {
    rowKey: `row-${unique}`,
    id: unique,
    session_id: sessionId,
    role,
    text: '',
    timestamp: currentTimestamp(),
  };
}

function toDateTimeLocal(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  date.setMinutes(date.getMinutes() - date.getTimezoneOffset());
  return date.toISOString().slice(0, 16);
}

function formatDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(date);
}

function NavButton({
  item,
  active,
  onClick,
}: {
  item: (typeof NAV_ITEMS)[number];
  active: boolean;
  onClick: () => void;
}) {
  const Icon = item.icon;
  return (
    <button className={`nav-item${active ? ' active' : ''}`} onClick={onClick} aria-current={active ? 'page' : undefined}>
      <Icon size={18} strokeWidth={1.8} aria-hidden="true" />
      <span>{item.label}</span>
      {active && <span className="nav-active-mark" aria-hidden="true" />}
    </button>
  );
}

function Feedback({ kind, children, onDismiss }: { kind: 'success' | 'error' | 'info'; children: ReactNode; onDismiss?: () => void; }) {
  const Icon = kind === 'success' ? CheckCircle2 : kind === 'error' ? AlertCircle : CircleHelp;
  return (
    <div className={`feedback feedback-${kind}`} role={kind === 'error' ? 'alert' : 'status'} aria-live={kind === 'error' ? 'assertive' : 'polite'}>
      <Icon size={18} aria-hidden="true" />
      <div className="feedback-copy">{children}</div>
      {onDismiss && <button className="icon-button feedback-dismiss" aria-label="Dismiss message" onClick={onDismiss}>
        <X size={16} />
      </button>}
    </div>
  );
}

function LoadingButton({ loading, children, ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { loading?: boolean; }) {
  return (
    <button {...props} disabled={props.disabled || loading}>
      {loading ? <LoaderCircle size={17} className="spin" aria-hidden="true" /> : null}
      {children}
    </button>
  );
}

function VerdictBadge({ label }: { label: VerdictLabel; }) {
  const Icon = label === 'supported' ? CheckCircle2 : label === 'outdated' ? Clock3 : label === 'contradicted' ? AlertCircle : ShieldQuestion;
  return (
    <span className={`verdict-badge verdict-${label}`}>
      <Icon size={14} aria-hidden="true" />
      {VERDICT_LABELS[label]}
    </span>
  );
}

function DecisionPanel({ decision, title = 'Verification report' }: { decision: Decision; title?: string; }) {
  const overall = ACTION_LABELS[decision.action] ?? decision.action;
  return (
    <section className="result-panel" aria-labelledby="decision-title">
      <div className="result-heading">
        <div>
          <p className="section-eyebrow">Result</p>
          <h2 id="decision-title">{title}</h2>
        </div>
        <span className={`action-badge action-${decision.action}`}>
          <ShieldCheck size={15} aria-hidden="true" />{overall}</span>
      </div>
      {decision.final_answer ? (
        <div className="answer-card">
          <span className="micro-label">Response returned by Nova</span>
          <p>{decision.final_answer}</p>
        </div>
      ) : (
        <div className="abstain-note">
          <ShieldQuestion size={19} aria-hidden="true" />
          <div>
            <strong>No final answer was returned.</strong>
            <p>The service may be unable to answer safely from the available evidence.</p>
          </div>
        </div>
      )}
      <div className="claims-heading">
        <div>
          <h3>Claim review</h3>
          <p>{decision.verdicts.length} {decision.verdicts.length === 1 ? 'claim' : 'claims'} returned by the verifier</p>
        </div>
        <span className="claim-count">{decision.verdicts.length.toString().padStart(2, '0')}</span>
      </div>
      {decision.verdicts.length === 0 ? (
        <div className="empty-inline">
          <FileSearch size={18} aria-hidden="true" />
          <span>The service returned no claim-level verdicts for this request.</span>
        </div>
      ) : (
        <div className="claim-list">
          {decision.verdicts.map((verdict, index) => <VerdictCard key={`${verdict.claim}-${index}`} verdict={verdict} index={index + 1} />)}
        </div>
      )}
      <p className="privacy-footnote">
        <Fingerprint size={14} aria-hidden="true" /> Evidence IDs and explanations are shown exactly as returned. Source quotations are not included in this API response.</p>
    </section>
  );
}

function VerdictCard({ verdict, index }: { verdict: Verdict; index: number; }) {
  return (
    <article className="claim-card">
      <div className="claim-card-top">
        <span className="claim-index">CLAIM {String(index).padStart(2, '0')}</span>
        <VerdictBadge label={verdict.label} />
      </div>
      <p className="claim-text">{verdict.claim}</p>
      {verdict.rationale ? <div className="claim-rationale">
        <span className="micro-label">Why this verdict</span>
        <p>{verdict.rationale}</p>
      </div> : null}
      {verdict.memory_ids.length > 0 ? (
        <div className="evidence-refs">
          <span className="micro-label">Evidence references</span>
          <div className="reference-list">{verdict.memory_ids.map((id) => <code key={id} title={`Memory reference ${id}`}>{id}</code>)}</div>
        </div>
      ) : <div className="no-evidence">
        <FileSearch size={14} aria-hidden="true" /> No memory reference was attached to this claim.</div>}
    </article>
  );
}

function CountCard({ label, value, note, icon: Icon }: { label: string; value: number; note: string; icon: typeof Check; }) {
  return (
    <div className="count-card">
      <div className="count-icon">
        <Icon size={18} aria-hidden="true" />
      </div>
      <span>{label}</span>
      <strong className="tabular">{value}</strong>
      <small>{note}</small>
    </div>
  );
}

export default function App() {
  const [page, setPage] = useState<NavigationItem>('home');
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [health, setHealth] = useState<'checking' | 'online' | 'offline'>('checking');
  const [sessionId, setSessionId] = useState(`session-${new Date().toISOString().slice(0, 10)}`);
  const [messages, setMessages] = useState<MessageDraft[]>([newMessage(`session-${new Date().toISOString().slice(0, 10)}`)]);
  const [ingestBusy, setIngestBusy] = useState(false);
  const [ingestResult, setIngestResult] = useState<IngestReport | null>(null);
  const [ingestError, setIngestError] = useState('');
  const [question, setQuestion] = useState('');
  const [draftAnswer, setDraftAnswer] = useState('');
  const [askBusy, setAskBusy] = useState(false);
  const [askResult, setAskResult] = useState<Decision | null>(null);
  const [askError, setAskError] = useState('');
  const [verifyQuestion, setVerifyQuestion] = useState('');
  const [verifyAnswer, setVerifyAnswer] = useState('');
  const [verifyBusy, setVerifyBusy] = useState(false);
  const [verifyResult, setVerifyResult] = useState<Decision | null>(null);
  const [verifyError, setVerifyError] = useState('');
  const [auditId, setAuditId] = useState('');
  const [auditBusy, setAuditBusy] = useState(false);
  const [auditResult, setAuditResult] = useState<AuditRecord | null>(null);
  const [auditError, setAuditError] = useState('');

  useEffect(() => {
    let alive = true;
    api.health().then(() => { if (alive) setHealth('online'); }).catch(() => { if (alive) setHealth('offline'); });
    return () => { alive = false; };
  }, []);

  const activePage = PAGES[page];
  const formattedMessageCount = useMemo(() => messages.length.toString().padStart(2, '0'), [messages.length]);

  const navigate = (next: NavigationItem) => {
    setPage(next);
    setMobileNavOpen(false);
  };

  const updateMessage = (index: number, update: Partial<IngestMessage>) => {
    setMessages((current) => current.map((message, i) => i === index ? { ...message, ...update, session_id: sessionId } : message));
    setIngestResult(null);
    setIngestError('');
  };

  const updateSession = (value: string) => {
    setSessionId(value);
    setMessages((current) => current.map((message) => ({ ...message, session_id: value })));
    setIngestResult(null);
    setIngestError('');
  };

  const submitIngest = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setIngestError('');
    setIngestResult(null);
    if (!sessionId.trim()) { setIngestError('Add a session ID before submitting.'); return; }
    if (sessionId !== sessionId.trim()) { setIngestError('Remove leading or trailing whitespace from the session ID; Nova will not silently rewrite it.'); return; }
    if (!messages.length || messages.some((message) => !message.text.trim())) {
      setIngestError('Each message row needs text. Remove unused rows or finish the message before submitting.');
      return;
    }
    if (messages.some((message) => !message.id.trim() || !message.timestamp || Number.isNaN(new Date(message.timestamp).getTime()))) {
      setIngestError('Each message needs a valid message ID and timestamp. Review the message fields.');
      return;
    }
    if (messages.some((message) => message.id !== message.id.trim())) {
      setIngestError('Remove leading or trailing whitespace from each message ID; Nova will not silently rewrite IDs.');
      return;
    }
    const messageIds = messages.map((message) => message.id.trim().toLowerCase());
    if (new Set(messageIds).size !== messageIds.length) {
      setIngestError('Each message ID must be unique within this submission.');
      return;
    }
    setIngestBusy(true);
    try {
      const cleanMessages: IngestMessage[] = messages.map((message) => ({
        id: message.id,
        session_id: sessionId,
        role: message.role,
        text: message.text,
        timestamp: new Date(message.timestamp).toISOString(),
      }));
      const result = await api.ingest(sessionId, cleanMessages);
      setIngestResult(result);
    } catch (error) {
      setIngestError(errorMessage(error));
    } finally {
      setIngestBusy(false);
    }
  };

  const submitAsk = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setAskError(''); setAskResult(null);
    if (!question.trim()) { setAskError('Enter a question before submitting.'); return; }
    setAskBusy(true);
    try { setAskResult(await api.ask(question, draftAnswer)); }
    catch (error) { setAskError(errorMessage(error)); }
    finally { setAskBusy(false); }
  };

  const submitVerify = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setVerifyError(''); setVerifyResult(null);
    if (!verifyQuestion.trim() || !verifyAnswer.trim()) { setVerifyError('Enter both a question and the complete answer you want checked.'); return; }
    setVerifyBusy(true);
    try { setVerifyResult(await api.verify(verifyQuestion, verifyAnswer)); }
    catch (error) { setVerifyError(errorMessage(error)); }
    finally { setVerifyBusy(false); }
  };

  const submitAudit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setAuditError(''); setAuditResult(null);
    if (!auditId.trim()) { setAuditError('Enter an audit ID to look it up.'); return; }
    setAuditBusy(true);
    try { setAuditResult(await api.audit(auditId)); }
    catch (error) { setAuditError(errorMessage(error)); }
    finally { setAuditBusy(false); }
  };


  return (
    <div className="app-shell">
      {mobileNavOpen && <button className="mobile-scrim" onClick={() => setMobileNavOpen(false)} aria-label="Close navigation" />}
      <aside className={`sidebar${mobileNavOpen ? ' sidebar-open' : ''}`} aria-label="Workspace sidebar">
        <div className="brand-lockup">
          <div className="brand-mark">
            <Fingerprint size={23} strokeWidth={1.7} aria-hidden="true" />
          </div>
          <div>
            <span className="brand-name">nova<span className="brand-period">.</span>
            </span>
            <span className="brand-caption">EVIDENCE WORKSPACE</span>
          </div>
          <button className="icon-button mobile-close" onClick={() => setMobileNavOpen(false)} aria-label="Close menu">
            <X size={19} />
          </button>
        </div>
        <div className="workspace-label">WORKSPACE</div>
        <nav className="primary-nav" aria-label="Primary navigation">
          {NAV_ITEMS.map((item) => <NavButton key={item.id} item={item} active={page === item.id} onClick={() => navigate(item.id)} />)}
        </nav>
        <div className="sidebar-bottom">
          <div className="trust-card">
            <div className="trust-icon">
              <ShieldCheck size={17} aria-hidden="true" />
            </div>
            <div>
              <strong>Evidence first</strong>
              <p>Claims stay distinct from the sources used to check them.</p>
            </div>
          </div>
          <div className="sidebar-footer">
            <span className={`connection-dot connection-${health}`} aria-hidden="true" />
            <span>{health === 'checking' ? 'Checking API connection' : health === 'online' ? 'API reachable' : 'API connection unavailable'}</span>
            <button className="icon-button connection-info" aria-label="About the provider" title="About the provider" onClick={() => window.dispatchEvent(new CustomEvent('nova-provider-info'))}>
              <CircleHelp size={15} />
            </button>
          </div>
          <p className="version-label">Nova workspace · Phase 5</p>
        </div>
      </aside>

      <main className="main-column">
        <header className="topbar">
          <button className="icon-button mobile-menu" onClick={() => setMobileNavOpen(true)} aria-label="Open navigation">
            <Menu size={20} />
          </button>
          <div className="breadcrumbs">
            <span>Workspace</span>
            <span className="crumb-divider">/</span>
            <strong>{NAV_ITEMS.find((item) => item.id === page)?.label}</strong>
          </div>
          <div className="topbar-right">
            <span className={`connection-pill connection-pill-${health}`}>
              <span className="connection-dot" />{health === 'online' ? 'Connected' : health === 'checking' ? 'Connecting' : 'Offline'}</span>
            <button className="avatar-button" aria-label="Open provider information" onClick={() => window.dispatchEvent(new CustomEvent('nova-provider-info'))}>N</button>
          </div>
        </header>

        <div className="content-wrap">
          <section className="page-intro">
            <p className="eyebrow">
              <span className="eyebrow-line" />{activePage.eyebrow}</p>
            <h1>{activePage.title}</h1>
            <p className="page-subtitle">{activePage.subtitle}</p>
          </section>

          <div className="provider-notice">
            <div className="provider-notice-icon">
              <Sparkles size={17} aria-hidden="true" />
            </div>
            <p>
              <strong>Transparent by design.</strong> The default development integration uses <code>FakeLLM</code>. This interface does not claim real-model inference; provider identity is not exposed by the health endpoint.</p>
            <button className="notice-link" onClick={() => window.dispatchEvent(new CustomEvent('nova-provider-info'))}>Details <ArrowRight size={14} aria-hidden="true" />
            </button>
          </div>

          {page === 'home' && <HomePage onNavigate={navigate} health={health} />}
          {page === 'ingest' && (
            <div className="page-grid page-grid-form">
              <form className="surface form-surface" aria-label="Ingest conversation form" onSubmit={submitIngest} noValidate>
                <FormHeader icon={ArrowDownLeft} step="01" title="Conversation details" description="Message IDs should remain stable if you retry the same conversation." />
                <div className="field-block">
                  <label htmlFor="session-id">Session ID <span className="required-mark">*</span>
                  </label>
                  <input id="session-id" value={sessionId} onChange={(event) => updateSession(event.target.value)} maxLength={128} required autoComplete="off" placeholder="e.g. project-planning-2026" />
                  <p className="field-help">Used to group messages from one conversation session.</p>
                </div>
                <div className="messages-header">
                  <div>
                    <h3>Messages</h3>
                    <p>Keep original wording and timestamps whenever possible.</p>
                  </div>
                  <span className="counter-pill">{formattedMessageCount} rows</span>
                </div>
                <div className="message-editor-list">
                  {messages.map((message, index) => (
                    <article className="message-editor" key={message.rowKey}>
                      <div className="message-editor-heading">
                        <span className="message-number">{String(index + 1).padStart(2, '0')}</span>
                        <span className="message-editor-label">MESSAGE</span>{messages.length > 1 && <button type="button" className="remove-message" onClick={() => { setMessages((current) => current.filter((_, i) => i !== index)); setIngestError(''); setIngestResult(null); }} aria-label={`Remove message ${index + 1}`}>
                          <X size={15} /> Remove</button>}</div>
                      <div className="field-row field-row-compact">
                        <div className="field-block">
                          <label htmlFor={`message-role-${index}`}>Role</label>
                          <div className="select-wrap">
                            <select id={`message-role-${index}`} value={message.role} onChange={(event) => updateMessage(index, { role: event.target.value as MessageRole })}>
                              <option value="user">User</option>
                              <option value="assistant">Assistant</option>
                              <option value="system">System</option>
                              <option value="tool">Tool</option>
                            </select>
                            <ChevronDown size={15} aria-hidden="true" />
                          </div>
                        </div>
                        <div className="field-block">
                          <label htmlFor={`message-id-${index}`}>Message ID</label>
                          <input id={`message-id-${index}`} value={message.id} onChange={(event) => updateMessage(index, { id: event.target.value })} maxLength={128} required />
                        </div>
                      </div>
                      <div className="field-block">
                        <label htmlFor={`message-text-${index}`}>Message text <span className="required-mark">*</span>
                        </label>
                        <textarea id={`message-text-${index}`} value={message.text} onChange={(event) => updateMessage(index, { text: event.target.value })} maxLength={20000} rows={4} placeholder="Paste the original conversation message here…" required />
                        <div className="textarea-meta">
                          <span>Original source text</span>
                          <span className="tabular">{message.text.length.toLocaleString()} / 20,000</span>
                        </div>
                      </div>
                      <div className="field-block">
                        <label htmlFor={`message-time-${index}`}>Timestamp</label>
                        <input id={`message-time-${index}`} type="datetime-local" value={toDateTimeLocal(message.timestamp)} onChange={(event) => updateMessage(index, { timestamp: event.target.value ? new Date(event.target.value).toISOString() : '' })} required />
                        <p className="field-help">Your browser timezone is applied when this value is saved.</p>
                      </div>
                    </article>
                  ))}
                </div>
                <button type="button" className="button button-secondary add-message" onClick={() => { setMessages((current) => [...current, newMessage(sessionId)]); setIngestError(''); setIngestResult(null); }} disabled={messages.length >= 200}>
                  <Plus size={17} /> Add another message</button>
                {ingestError && <Feedback kind="error">{ingestError}</Feedback>}
                {ingestResult && <IngestSuccess result={ingestResult} />}
                <div className="form-footer">
                  <div className="form-footnote">
                    <ShieldCheck size={15} />
                    <span>No user content is written to the browser's local storage.</span>
                  </div>
                  <LoadingButton className="button button-primary" type="submit" loading={ingestBusy}>
                    <ArrowDownLeft size={17} /> Ingest conversation <ArrowRight size={16} />
                  </LoadingButton>
                </div>
              </form>
              <aside className="side-column">
                <div className="surface side-note">
                  <div className="side-note-icon">
                    <BookOpenCheck size={19} />
                  </div>
                  <h3>What happens next?</h3>
                  <ol className="steps-list">
                    <li>
                      <span>1</span>
                      <div>
                        <strong>Extract</strong>
                        <p>Candidate memories are derived from the messages you submit.</p>
                      </div>
                    </li>
                    <li>
                      <span>2</span>
                      <div>
                        <strong>Check source quotes</strong>
                        <p>Memories without valid supporting quotes may be dropped.</p>
                      </div>
                    </li>
                    <li>
                      <span>3</span>
                      <div>
                        <strong>Update timeline</strong>
                        <p>Newer facts may supersede previously stored values.</p>
                      </div>
                    </li>
                  </ol>
                </div>
                <div className="quiet-callout">
                  <AlertCircle size={16} />
                  <p>Ingestion is not a verbatim transcript archive. The result reports counts, not a browsable memory list.</p>
                </div>
              </aside>
            </div>
          )}

          {page === 'ask' && (
            <div className="page-grid page-grid-form">
              <form className="surface form-surface" aria-label="Ask question form" onSubmit={submitAsk} noValidate>
                <FormHeader icon={MessageSquareText} step="02" title="Your question" description="Ask a clear question. The response is evaluated against retrieved evidence before it is returned." />
                <div className="field-block">
                  <label htmlFor="ask-question">Question <span className="required-mark">*</span>
                  </label>
                  <textarea id="ask-question" value={question} onChange={(event) => { setQuestion(event.target.value); setAskError(''); }} maxLength={2000} rows={4} placeholder="What did we decide about the database migration?" required />
                  <div className="textarea-meta">
                    <span>Be specific about names, decisions, or dates</span>
                    <span className="tabular">{question.length} / 2,000</span>
                  </div>
                </div>
                <div className="field-block">
                  <label htmlFor="ask-draft">Optional draft answer <span className="optional-label">Optional</span>
                  </label>
                  <textarea id="ask-draft" value={draftAnswer} onChange={(event) => { setDraftAnswer(event.target.value); setAskError(''); }} maxLength={10000} rows={3} placeholder="Leave blank to let the configured backend attempt a draft…" />
                  <p className="field-help">The API supports a draft answer. When left blank, answer generation depends on the server's configured provider.</p>
                </div>
                {askError && <Feedback kind="error">{askError}</Feedback>}
                <div className="form-footer">
                  <div className="form-footnote">
                    <FileSearch size={15} />
                    <span>Evidence is shown as references and rationale.</span>
                  </div>
                  <LoadingButton className="button button-primary" type="submit" loading={askBusy}>
                    <Search size={17} /> Ask Nova <ArrowRight size={16} />
                  </LoadingButton>
                </div>
              </form>
              <aside className="side-column">
                <div className="surface side-note">
                  <div className="side-note-icon">
                    <ShieldCheck size={19} />
                  </div>
                  <h3>Designed to be careful</h3>
                  <div className="principle-list">
                    <div>
                      <CheckCircle2 size={16} />
                      <p>
                        <strong>Supported</strong>
                        <span>A retrieved memory supports the claim.</span>
                      </p>
                    </div>
                    <div>
                      <Clock3 size={16} />
                      <p>
                        <strong>Outdated</strong>
                        <span>Evidence conflicts with a superseded value.</span>
                      </p>
                    </div>
                    <div>
                      <AlertCircle size={16} />
                      <p>
                        <strong>Contradicted</strong>
                        <span>Available evidence conflicts with the claim.</span>
                      </p>
                    </div>
                    <div>
                      <ShieldQuestion size={16} />
                      <p>
                        <strong>Unsupported</strong>
                        <span>The available evidence is not enough.</span>
                      </p>
                    </div>
                  </div>
                </div>
                <div className="quiet-callout">
                  <CircleHelp size={16} />
                  <p>Nova may abstain. An unknown answer is safer than an unsupported assertion.</p>
                </div>
              </aside>
              {askResult && <div className="result-column">
                <DecisionPanel decision={askResult} title="Answer & evidence review" />
              </div>}
            </div>
          )}

          {page === 'verify' && (
            <div className="page-grid page-grid-form">
              <form className="surface form-surface" aria-label="Verify answer form" onSubmit={submitVerify} noValidate>
                <FormHeader icon={ShieldCheck} step="03" title="Review a proposed answer" description="The original question gives the verifier context for assessing each claim in the draft." />
                <div className="field-block">
                  <label htmlFor="verify-question">Original question <span className="required-mark">*</span>
                  </label>
                  <textarea id="verify-question" value={verifyQuestion} onChange={(event) => { setVerifyQuestion(event.target.value); setVerifyError(''); }} maxLength={2000} rows={3} placeholder="What question was this answer intended to resolve?" required />
                  <div className="textarea-meta">
                    <span>Maximum 2,000 characters</span>
                    <span className="tabular">{verifyQuestion.length} / 2,000</span>
                  </div>
                </div>
                <div className="field-block">
                  <label htmlFor="verify-answer">Draft answer <span className="required-mark">*</span>
                  </label>
                  <textarea id="verify-answer" value={verifyAnswer} onChange={(event) => { setVerifyAnswer(event.target.value); setVerifyError(''); }} maxLength={10000} rows={6} placeholder="Paste the full answer you want to check…" required />
                  <div className="textarea-meta">
                    <span>Multi-claim answers are reviewed claim by claim</span>
                    <span className="tabular">{verifyAnswer.length.toLocaleString()} / 10,000</span>
                  </div>
                </div>
                {verifyError && <Feedback kind="error">{verifyError}</Feedback>}
                <div className="form-footer">
                  <div className="form-footnote">
                    <Fingerprint size={15} />
                    <span>The submitted answer remains visible beside the result.</span>
                  </div>
                  <LoadingButton className="button button-primary" type="submit" loading={verifyBusy}>
                    <ShieldCheck size={17} /> Verify answer <ArrowRight size={16} />
                  </LoadingButton>
                </div>
              </form>
              <aside className="side-column">
                <div className="surface side-note">
                  <div className="side-note-icon">
                    <FileText size={19} />
                  </div>
                  <h3>What the report contains</h3>
                  <ul className="report-list">
                    <li>Each claim returned by the splitter and judge</li>
                    <li>A verdict label with the returned rationale</li>
                    <li>Memory IDs associated with the claim, if any</li>
                    <li>The deterministic overall decision</li>
                  </ul>
                  <div className="report-separator" />
                  <p className="muted-copy">This API does not return source quotations with decisions. References are not the same as verified source text.</p>
                </div>
                <div className="quiet-callout">
                  <ShieldQuestion size={16} />
                  <p>A report evaluates what the backend returned; it is not a guarantee of truth.</p>
                </div>
              </aside>
              {verifyResult && <div className="result-column">
                <div className="surface submitted-answer">
                  <span className="micro-label">Submitted answer · preserved</span>
                  <p>{verifyAnswer}</p>
                </div>
                <DecisionPanel decision={verifyResult} /> </div>}
            </div>
          )}

          {page === 'audit' && (
            <div className="audit-page">
              <form className="surface audit-search" aria-label="Audit lookup form" onSubmit={submitAudit} noValidate>
                <div className="audit-search-icon">
                  <Search size={21} />
                </div>
                <div className="audit-search-field">
                  <label htmlFor="audit-id">Audit record ID</label>
                  <input id="audit-id" value={auditId} onChange={(event) => { setAuditId(event.target.value); setAuditError(''); setAuditResult(null); }} maxLength={128} placeholder="Paste the ID returned by an API request" required autoComplete="off" />
                  <p className="field-help">For privacy, use an exact ID. The API does not provide a list or search endpoint for audit records.</p>
                </div>
                <LoadingButton className="button button-primary" type="submit" loading={auditBusy}>
                  <Search size={16} /> Look up record</LoadingButton>
              </form>
              {auditError && <Feedback kind="error">{auditError}</Feedback>}
              {auditResult && <AuditDetail record={auditResult} />}
              {!auditResult && !auditError && <div className="audit-empty surface">
                <div className="audit-empty-icon">
                  <FileSearch size={26} />
                </div>
                <h2>One record at a time</h2>
                <p>Audit records are not browsable from this API. Enter an ID you already have to review the stored decision and evidence references.</p>
                <div className="audit-empty-meta">
                  <span>
                    <ShieldCheck size={15} /> Tenant-scoped access</span>
                  <span>
                    <Clock3 size={15} /> Retention policy applies</span>
                </div>
              </div>}
            </div>
          )}

          <footer className="page-footer">
            <span>Nova · Context Anchor</span>
            <span>Evidence is a reference point, not a substitute for judgment.</span>
          </footer>
        </div>
      </main>

      <ProviderInfoDialog />
    </div>
  );
}

function FormHeader({ icon: Icon, step, title, description }: { icon: typeof Home; step: string; title: string; description: string; }) {
  return <div className="form-header">
    <div className="form-header-icon">
      <Icon size={19} aria-hidden="true" />
    </div>
    <div className="form-header-copy">
      <span className="step-label">STEP {step}</span>
      <h2>{title}</h2>
      <p>{description}</p>
    </div>
  </div>;
}

function IngestSuccess({ result }: { result: IngestReport; }) {
  return <div className="ingest-success" role="status" aria-live="polite">
    <div className="success-title">
      <CheckCircle2 size={18} />
      <div>
        <strong>Ingestion request completed</strong>
        <p>These counts are the API's actual response for the submitted messages.</p>
      </div>
    </div>
    <div className="count-grid">
      <CountCard label="Added" value={result.added} note="New memories stored" icon={Check} />
      <CountCard label="Superseded" value={result.superseded} note="Older values replaced" icon={Clock3} />
      <CountCard label="Dropped" value={result.dropped} note="Candidates not stored" icon={ArrowDownLeft} />
    </div>{result.dropped > 0 && <p className="partial-success-note">
      <AlertCircle size={15} /> Some candidate memories were dropped. Review the source quotes and extraction rules before retrying.</p>}</div>;
}

function HomePage({ onNavigate, health }: { onNavigate: (page: NavigationItem) => void; health: 'checking' | 'online' | 'offline'; }) {
  return (
    <div className="home-content">
      <section className="welcome-banner">
        <div className="welcome-copy">
          <span className="welcome-kicker">
            <span /> SOURCE-LINKED MEMORY</span>
          <h2>Knowledge you can<br />
            <em>trace back to its source.</em>
          </h2>
          <p>Nova keeps extracted memories connected to the conversation they came from—and checks proposed answers against that evidence.</p>
          <button className="button button-light" onClick={() => onNavigate('ingest')}>Start with a conversation <ArrowRight size={16} />
          </button>
        </div>
        <div className="welcome-art" aria-hidden="true">
          <div className="orbit orbit-one" />
          <div className="orbit orbit-two" />
          <div className="orbit orbit-three" />
          <div className="art-core">
            <Fingerprint size={48} strokeWidth={1.2} />
          </div>
          <div className="art-node node-a">
            <FileText size={18} />
          </div>
          <div className="art-node node-b">
            <ShieldCheck size={18} />
          </div>
          <div className="art-node node-c">
            <MessageSquareText size={18} />
          </div>
          <div className="art-line line-a" />
          <div className="art-line line-b" />
        </div>
      </section>
      <div className="section-title-row">
        <div>
          <p className="section-eyebrow">Get started</p>
          <h2>Choose a workflow</h2>
          <p>Use the real API workflows available in this deployment.</p>
        </div>
        <span className={`api-status ${health}`}>
          <span className="connection-dot" />{health === 'online' ? 'API reachable' : health === 'checking' ? 'Checking connection' : 'API unavailable'}</span>
      </div>
      <div className="workflow-grid">
        <WorkflowCard number="01" title="Ingest conversation" description="Submit original message text and timestamps so candidate memories can be extracted and checked against source quotes." icon={ArrowDownLeft} tone="violet" action="Open ingestion" onClick={() => onNavigate('ingest')} />
        <WorkflowCard number="02" title="Ask a question" description="Ask about stored context and inspect the answer, per-claim verdicts, rationale, and memory references returned by the API." icon={MessageSquareText} tone="green" action="Ask Nova" onClick={() => onNavigate('ask')} />
        <WorkflowCard number="03" title="Verify a draft" description="Check an existing answer claim by claim before you rely on it or pass it to someone else." icon={ShieldCheck} tone="amber" action="Verify answer" onClick={() => onNavigate('verify')} />
        <WorkflowCard number="04" title="Audit lookup" description="Trace a prior decision with an audit ID. This API version does not provide an audit list or free-text search." icon={FileSearch} tone="blue" action="Look up a record" onClick={() => onNavigate('audit')} />
      </div>
      <section className="how-it-works">
        <div className="how-title">
          <span className="how-icon">
            <Activity size={19} />
          </span>
          <div>
            <h2>What makes Nova different</h2>
            <p>The workflow separates stored context, evidence, and the decision made from that evidence.</p>
          </div>
        </div>
        <div className="how-steps">
          <div>
            <span>01</span>
            <strong>Capture</strong>
            <p>Start from original messages with stable IDs and timestamps.</p>
          </div>
          <div>
            <span>02</span>
            <strong>Trace</strong>
            <p>Keep source-linked memory references and supersession history.</p>
          </div>
          <div>
            <span>03</span>
            <strong>Verify</strong>
            <p>Evaluate answer claims and make unsupported cases visible.</p>
          </div>
        </div>
      </section>
      <div className="capability-note">
        <CircleHelp size={17} />
        <p>
          <strong>Intentionally not shown:</strong> memory browsing, recent-activity feeds, analytics, and team/account settings. The current API does not expose authorized endpoints for those capabilities.</p>
      </div>
    </div>
  );
}

function WorkflowCard({ number, title, description, icon: Icon, tone, action, onClick }: { number: string; title: string; description: string; icon: typeof Home; tone: string; action: string; onClick: () => void; }) {
  return <button className="workflow-card" onClick={onClick}>
    <div className="workflow-top">
      <span className={`workflow-icon tone-${tone}`}>
        <Icon size={20} aria-hidden="true" />
      </span>
      <span className="workflow-number">{number}</span>
    </div>
    <h3>{title}</h3>
    <p>{description}</p>
    <span className="workflow-action">{action}<ArrowRight size={15} aria-hidden="true" />
    </span>
  </button>;
}

function AuditDetail({ record }: { record: AuditRecord; }) {
  const [showText, setShowText] = useState(false);
  return <div className="audit-detail">
    <section className="surface audit-record-header">
      <div className="audit-record-icon">
        <FileSearch size={20} />
      </div>
      <div className="audit-record-meta">
        <span className="section-eyebrow">Audit record</span>
        <h2>Decision detail</h2>
        <code>{record.id}</code>
      </div>
      <span className={`action-badge action-${record.decision_action}`}>
        <ShieldCheck size={14} />{ACTION_LABELS[record.decision_action] ?? record.decision_action}</span>
    </section>
    <div className="audit-summary-grid">
      <div className="surface audit-stat">
        <span>Recorded</span>
        <strong>{formatDate(record.timestamp)}</strong>
      </div>
      <div className="surface audit-stat">
        <span>Latency</span>
        <strong className="tabular">{Math.max(0, record.latency_ms).toFixed(1)} ms</strong>
      </div>
      <div className="surface audit-stat">
        <span>Claims reviewed</span>
        <strong className="tabular">{record.verdicts.length}</strong>
      </div>
      <div className="surface audit-stat">
        <span>Evidence references</span>
        <strong className="tabular">{record.retrieved_ids.length}</strong>
      </div>
    </div>
    <section className="surface audit-evidence-section">
      <div className="audit-section-head">
        <div>
          <p className="section-eyebrow">Evidence trail</p>
          <h3>Retrieved memory IDs</h3>
        </div>
        <span className="counter-pill">{record.retrieved_ids.length}</span>
      </div>{record.retrieved_ids.length ? <div className="reference-list reference-list-large">{record.retrieved_ids.map((id) => <code key={id}>{id}</code>)}</div> : <p className="muted-copy">No retrieved memory IDs were recorded.</p>}<div className="audit-section-head audit-verdict-heading">
        <div>
          <p className="section-eyebrow">Outcome</p>
          <h3>Claim verdicts</h3>
        </div>
        <span className="counter-pill">{record.verdicts.length}</span>
      </div>{record.verdicts.length ? <div className="claim-list">{record.verdicts.map((verdict, index) => <VerdictCard key={`${verdict.claim}-${index}`} verdict={verdict} index={index + 1} />)}</div> : <p className="muted-copy">No claim-level verdicts are recorded in this audit entry.</p>}</section>
    <section className="surface audit-text-panel">
      <button className="audit-text-toggle" onClick={() => setShowText((value) => !value)} aria-expanded={showText}>
        <span>
          <FileText size={17} /> Recorded question and answer</span>
        <ChevronDown className={showText ? 'rotate-180' : ''} size={17} />
      </button>{showText ? <div className="audit-prompt-content">
        <div>
          <span className="micro-label">Question</span>
          <p>{record.question || 'No question text stored.'}</p>
        </div>
        <div>
          <span className="micro-label">Answer</span>
          <p>{record.answer || 'No answer text stored.'}</p>
        </div>
      </div> : <p className="audit-text-hint">Hidden by default because audit text may contain sensitive conversation content.</p>}</section>
    <p className="privacy-footnote">
      <Fingerprint size={14} /> Token usage appears only when the configured provider reported actual usage. {record.prompt_tokens === null || record.completion_tokens === null ? 'This record has no reported token counts.' : `Recorded token counts: ${record.prompt_tokens.toLocaleString()} prompt and ${record.completion_tokens.toLocaleString()} completion.`}</p>
  </div>;
}

function ProviderInfoDialog() {
  const [open, setOpen] = useState(false);
  const dialogRef = useRef<HTMLElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const listener = () => setOpen(true);
    window.addEventListener('nova-provider-info', listener);
    return () => window.removeEventListener('nova-provider-info', listener);
  }, []);
  useEffect(() => {
    if (!open) return;
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closeButtonRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setOpen(false);
        return;
      }
      if (event.key !== 'Tab') return;
      const focusable = dialogRef.current?.querySelectorAll<HTMLElement>(
        'button:not([disabled]), a[href], input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
      );
      const visible = Array.from(focusable ?? []).filter((element) => element.getClientRects().length > 0);
      if (!visible.length) {
        event.preventDefault();
        return;
      }
      const first = visible[0];
      const last = visible[visible.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('keydown', onKey);
      previousFocus?.focus();
    };
  }, [open]);
  if (!open) return null;
  return <div className="dialog-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) setOpen(false); }}>
    <section ref={dialogRef} className="provider-dialog" role="dialog" aria-modal="true" aria-labelledby="provider-dialog-title">
      <button ref={closeButtonRef} className="icon-button dialog-close" onClick={() => setOpen(false)} aria-label="Close provider details">
        <X size={18} />
      </button>
      <div className="dialog-symbol">
        <Sparkles size={22} />
      </div>
      <p className="section-eyebrow">Provider transparency</p>
      <h2 id="provider-dialog-title">Know what is—and isn't—configured.</h2>
      <p>The default development integration in this repository is <code>FakeLLM</code>, a deterministic testing double. It is not a real language model.</p>
      <p>The public health endpoint reports service liveness only; it does not identify the active model provider. This interface therefore avoids claiming that a real model generated a response.</p>
      <div className="dialog-note">
        <ShieldCheck size={16} />
        <span>Hosted deployments should use a trusted, authenticated same-origin proxy. Do not put deployment API keys into frontend environment variables, browser storage, or client code.</span>
      </div>
      <button className="button button-primary dialog-done" onClick={() => setOpen(false)}>Got it</button>
    </section>
  </div>;
}
