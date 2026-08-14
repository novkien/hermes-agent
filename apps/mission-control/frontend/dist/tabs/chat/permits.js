// Pending permits, surfaced where the operator is actually talking to the agent.
//
// A permit is a decision an agent is blocked on. The adapter's ledger is NOT
// session-scoped — a permit row carries an issue, a fingerprint and a severity,
// but no session_id — so this deliberately does not pretend a permit belongs to
// the open thread. It is a banner: "something is waiting on you", decidable in
// place, with a way through to the Permits tab for the full record.
//
// The shaping and the decision body come from the Permits tab's own pure
// helpers rather than a second copy of those rules.

import { el, clear } from '../../ui.js';
import { icon } from '../../icons.js';
import { listFrom } from '../../pure/data-shape.js';
import { decisionBody, isApproved, normalizePermit } from '../permits.js';

// Mirrors the Permits tab's own "needs a decision" filter.
const OPEN_STATUSES = new Set(['pending_approval', 'open']);

export function pendingPermits(envelope) {
  return listFrom(envelope, ['permits'])
    .map(normalizePermit)
    .filter((row) => OPEN_STATUSES.has(row.status) && !isApproved(row.approved));
}

export function createPermitBanner({ api, profile, onNavigate }) {
  const node = el('div', { class: 'chat-permits', role: 'status' });
  node.hidden = true;

  async function refresh() {
    const response = await api.get('/api/adapter/permits?limit=25', { profile }).catch(() => null);
    const rows = response ? pendingPermits(response.data) : [];
    clear(node);
    node.hidden = rows.length === 0;
    if (!rows.length) return;

    node.append(el('div', { class: 'chat-permits-head' }, [
      icon('permits', { size: 12 }),
      el('span', { text: `${rows.length} permit${rows.length === 1 ? '' : 's'} waiting on you` }),
      el('button', {
        class: 'chat-permits-link', type: 'button',
        onclick: () => onNavigate && onNavigate('permits', {}),
      }, ['Open Permits']),
    ]));

    for (const row of rows.slice(0, 3)) node.append(permitCard(row));
  }

  function permitCard(row) {
    const card = el('div', { class: `chat-permit chat-permit-${row.severity || 'normal'}` });
    card.append(el('div', { class: 'chat-permit-title', text: row.title || row.permit_id }));
    if (row.problem_summary) {
      card.append(el('div', { class: 'chat-permit-note', text: String(row.problem_summary).slice(0, 220) }));
    }

    const actions = el('div', { class: 'chat-permit-actions' });
    const note = el('input', {
      class: 'input chat-permit-input', type: 'text',
      placeholder: 'Note (optional)', 'aria-label': 'Decision note',
    });
    actions.append(note);
    actions.append(decisionButton('Approve', 'ok', () => decide(row, {
      status: 'approved', approved: true, approval_note: note.value,
    })));
    actions.append(decisionButton('Reject', 'danger', () => decide(row, {
      status: 'rejected', approved: false, approval_note: note.value,
    })));
    card.append(actions);
    return card;
  }

  function decisionButton(label, tone, onClick) {
    const button = el('button', { class: `btn btn-sm chat-permit-btn is-${tone}`, type: 'button' }, [label]);
    button.addEventListener('click', async () => {
      button.disabled = true;
      await onClick();
      button.disabled = false;
    });
    return button;
  }

  async function decide(row, diff) {
    await api.post(
      `/api/permits/${encodeURIComponent(row.permit_id)}/decision`,
      decisionBody(diff),
      { profile },
    ).catch(() => null);
    await refresh();
  }

  return { node, refresh };
}
