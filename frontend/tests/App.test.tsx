import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import App from '../src/App';

const healthMock = vi.fn();
const ingestMock = vi.fn();
const askMock = vi.fn();
const verifyMock = vi.fn();
const auditMock = vi.fn();

vi.mock('../src/api', () => ({
  api: {
    health: (...args: unknown[]) => healthMock(...args),
    ingest: (...args: unknown[]) => ingestMock(...args),
    ask: (...args: unknown[]) => askMock(...args),
    verify: (...args: unknown[]) => verifyMock(...args),
    audit: (...args: unknown[]) => auditMock(...args),
  },
}));

beforeEach(() => {
  healthMock.mockResolvedValue({ status: 'ok' });
  ingestMock.mockReset(); askMock.mockReset(); verifyMock.mockReset(); auditMock.mockReset();
});

describe('workspace workflows', () => {
  it('does not display invented metrics on the overview page', async () => {
    render(<App />);
    expect(screen.getByRole('heading', { name: /keep answers anchored to evidence/i })).toBeInTheDocument();
    expect(within(screen.getByRole('navigation', { name: 'Primary navigation' })).getByRole('button', { name: /ingest conversation/i })).toBeInTheDocument();
    expect(screen.queryByText(/memories stored/i)).not.toBeInTheDocument();
  });

  it('renders all backend verdicts with explicit text labels', async () => {
    verifyMock.mockResolvedValue({
      action: 'qualify',
      final_answer: 'The decision may need qualification.',
      verdicts: [
        { claim: 'PostgreSQL was selected.', label: 'supported', memory_ids: ['mem-1'], rationale: 'A source memory supports this.' },
        { claim: 'The launch was in April.', label: 'outdated', memory_ids: ['mem-2'], rationale: 'A later memory superseded this date.' },
        { claim: 'The migration is finished.', label: 'contradicted', memory_ids: ['mem-3'], rationale: 'Evidence says work is still open.' },
        { claim: 'The budget is known.', label: 'unsupported', memory_ids: [], rationale: 'No evidence was found.' },
      ],
    });
    render(<App />);
    fireEvent.click(within(screen.getByRole('navigation', { name: 'Primary navigation' })).getByRole('button', { name: /verify an answer/i }));
    fireEvent.change(screen.getByLabelText(/original question/i), { target: { value: 'What did we decide?' } });
    fireEvent.change(screen.getByLabelText(/draft answer/i), { target: { value: 'A draft with multiple claims.' } });
    fireEvent.click(screen.getByRole('button', { name: /verify answer/i }));
    await waitFor(() => expect(screen.getByText('Supported')).toBeInTheDocument());
    expect(screen.getByText('Outdated')).toBeInTheDocument();
    expect(screen.getByText('Contradicted')).toBeInTheDocument();
    expect(screen.getByText('Unsupported')).toBeInTheDocument();
    expect(screen.getByText('mem-1')).toBeInTheDocument();
    expect(screen.getByText(/source quotations are not included/i)).toBeInTheDocument();
  });

  it('preserves the ingest form after a recoverable API error', async () => {
    ingestMock.mockRejectedValue(new Error('Service temporarily unavailable.'));
    render(<App />);
    fireEvent.click(within(screen.getByRole('navigation', { name: 'Primary navigation' })).getByRole('button', { name: /ingest conversation/i }));
    fireEvent.change(screen.getByLabelText(/message text/i), { target: { value: 'Please keep this text.' } });
    fireEvent.click(within(screen.getByRole('form', { name: 'Ingest conversation form' })).getByRole('button', { name: /ingest conversation/i }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(/temporarily unavailable/i));
    expect(screen.getByLabelText(/message text/i)).toHaveValue('Please keep this text.');
  });

  it('shows neutral feedback when an audit ID is not found', async () => {
    auditMock.mockRejectedValue(new Error('No audit record was found for that ID in your authorized workspace.'));
    render(<App />);
    fireEvent.click(within(screen.getByRole('navigation', { name: 'Primary navigation' })).getByRole('button', { name: /audit lookup/i }));
    fireEvent.change(screen.getByLabelText(/audit record id/i), { target: { value: 'unknown-id' } });
    fireEvent.click(within(screen.getByRole('form', { name: 'Audit lookup form' })).getByRole('button', { name: /look up record/i }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(/No audit record was found/i));
  });
});
