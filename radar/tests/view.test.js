import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { failureUrl, isHttpsUrl, isUnderBaseUrl } from '../src/links.js';
import {
  SECTION_IDS,
  collectNotificationEvents,
  deriveView,
  hasActivePipelines,
  sectionsToSkip,
  tokenProblems,
} from '../src/view.js';

const BASE_URL = 'https://gitlab.example.com';

describe('deriveView', () => {
  it('counts unique MRs waiting for me and my failed pipelines', () => {
    const { counts } = deriveView({
      sections: {
        reviewing: [
          { key: 'a', approvedByMe: false, draft: false },
          { key: 'b', approvedByMe: true, draft: false },
          { key: 'c', approvedByMe: false, draft: true },
        ],
        mentioned: [{ key: 'd' }],
        awaitingApproval: [{ key: 'e' }],
        threads: [{ key: 'a' }, { key: 'f' }],
        authored: [
          { key: 'f', pipeline: { status: 'failed', onlyIgnoredFailures: false } },
          { key: 'g', pipeline: { status: 'failed', onlyIgnoredFailures: true } },
          { key: 'h', pipeline: { status: 'success' } },
          { key: 'i', pipeline: null },
        ],
      },
    });
    assert.deepEqual(counts, { waiting: 4, failedCi: 1 });
  });

  it('fills missing sections with empty lists', () => {
    const { sections, counts } = deriveView({ sections: { reviewing: [] } });
    assert.deepEqual(Object.keys(sections), SECTION_IDS);
    assert.ok(Object.values(sections).every((items) => items.length === 0));
    assert.deepEqual(counts, { waiting: 0, failedCi: 0 });
  });

  it('does not count possible MRs towards the badge', () => {
    const { counts } = deriveView({
      sections: {
        reviewing: [{ key: 'a', approvedByMe: false, draft: false }],
        possibleMrs: [{ key: 'branch:10:x' }, { key: 'branch:10:y' }],
      },
    });
    assert.deepEqual(counts, { waiting: 1, failedCi: 0 });
  });

  it('excludes hidden sections from counts but keeps them in the view', () => {
    const view = deriveView(
      { sections: { reviewing: [{ key: 'a', approvedByMe: false, draft: false }] } },
      {},
      Date.now(),
      ['reviewing'],
    );
    assert.equal(view.sections.reviewing.length, 1);
    assert.deepEqual(view.counts, { waiting: 0, failedCi: 0 });
  });
});

describe('sectionsToSkip', () => {
  it('skips hidden sections when notifications are off', () => {
    assert.deepEqual(sectionsToSkip({ hiddenSections: ['mentioned', 'authored'] }), ['mentioned', 'authored']);
  });

  it('keeps a hidden section whose notifications are still enabled', () => {
    const settings = { hiddenSections: ['mentioned'], notifications: true, mutedNotificationTypes: [] };
    assert.deepEqual(sectionsToSkip(settings), []);
  });

  it('skips a hidden section whose notification types are all muted', () => {
    const settings = { hiddenSections: ['mentioned'], notifications: true, mutedNotificationTypes: ['mention'] };
    assert.deepEqual(sectionsToSkip(settings), ['mentioned']);
  });

  it('always skips hidden sections without notifications, e.g. possible MRs', () => {
    const settings = { hiddenSections: ['possibleMrs'], notifications: true, mutedNotificationTypes: [] };
    assert.deepEqual(sectionsToSkip(settings), ['possibleMrs']);
  });
});

describe('collectNotificationEvents', () => {
  it('does not flood with events when a section was skipped before', () => {
    const base = { baseUrl: BASE_URL, username: 'me', filters: null };
    const previous = { ...base, skippedSections: ['reviewing'], sections: { reviewing: [] } };
    const next = {
      ...base,
      skippedSections: [],
      sections: {
        reviewing: [{ key: '10!1', kind: 'mr', approvedByMe: false, draft: false, myReviewState: null }],
      },
    };
    assert.deepEqual(collectNotificationEvents(previous, next), []);
  });
});

describe('hasActivePipelines', () => {
  const withAuthored = (...authored) => ({ sections: { authored } });

  it('is true while one of my pipelines runs or GitLab rechecks the merge status', () => {
    assert.equal(hasActivePipelines(withAuthored({ pipeline: { status: 'success' } }, { pipeline: { status: 'pending' } })), true);
    assert.equal(hasActivePipelines(withAuthored({ status: 'ci_still_running', pipeline: null })), true);
    assert.equal(hasActivePipelines(withAuthored({ status: 'checking', pipeline: null })), true);
  });

  it('is false when every pipeline has finished', () => {
    assert.equal(
      hasActivePipelines(withAuthored({ status: 'mergeable', pipeline: { status: 'success' } }, { pipeline: { status: 'failed' } })),
      false,
    );
    assert.equal(hasActivePipelines({ error: 'x' }), false);
  });
});

describe('links', () => {
  it('accepts only https URLs', () => {
    assert.equal(isHttpsUrl('https://ci.example.com/job/1'), true);
    assert.equal(isHttpsUrl('http://ci.example.com/job/1'), false);
    assert.equal(isHttpsUrl('javascript:alert(1)'), false);
    assert.equal(isHttpsUrl('not a url'), false);
    assert.equal(isHttpsUrl(undefined), false);
  });

  it('keeps GitLab links inside the configured instance', () => {
    assert.equal(isUnderBaseUrl(`${BASE_URL}/group/project`, BASE_URL), true);
    assert.equal(isUnderBaseUrl(`${BASE_URL}.evil.com/x`, BASE_URL), false);
    assert.equal(isUnderBaseUrl(null, BASE_URL), false);
  });

  it('resolves failure links by kind', () => {
    assert.equal(failureUrl({ kind: 'job', webUrl: `${BASE_URL}/-/jobs/1` }, BASE_URL), `${BASE_URL}/-/jobs/1`);
    assert.equal(failureUrl({ kind: 'job', webUrl: 'https://evil.com/x' }, BASE_URL), BASE_URL);
    assert.equal(failureUrl({ kind: 'external', webUrl: 'https://jenkins.example.com/1' }, BASE_URL), 'https://jenkins.example.com/1');
    assert.equal(failureUrl({ kind: 'external', webUrl: 'http://jenkins.example.com/1' }, BASE_URL), null);
  });
});

describe('tokenProblems', () => {
  const NOW = Date.parse('2026-03-10T12:00:00Z');
  const texts = (token, dismissed) => tokenProblems(token, dismissed, NOW).map((problem) => problem.text);

  it('warns about expiry within a week, missing expiry and extra scopes', () => {
    assert.deepEqual(texts({ expiresAt: '2026-04-10', scopes: ['read_api'] }), []);
    assert.deepEqual(texts({ expiresAt: '2026-03-14', scopes: ['read_api'] }), ['Токен истекает через 3 дн.']);
    assert.deepEqual(texts({ expiresAt: '2026-03-11', scopes: ['read_api'] }), ['Токен истекает сегодня']);
    assert.deepEqual(texts({ expiresAt: '2026-03-10', scopes: ['read_api'] }), ['Срок действия токена истёк']);
    assert.deepEqual(texts({ expiresAt: null, scopes: ['read_api', 'write_repository', 'api'] }), [
      'Токен бессрочный — лучше пересоздать его со сроком действия',
      'У токена лишние права: api, write_repository — достаточно read_api',
    ]);
    assert.deepEqual(texts(null), []);
  });

  it('lets the no-expiry and extra-scopes warnings be dismissed', () => {
    const token = { expiresAt: null, scopes: ['read_api', 'write_repository', 'api'] };
    const [noExpiry, extraScopes] = tokenProblems(token, {}, NOW);
    assert.equal(noExpiry.dismissKey, 'noExpiry');
    assert.equal(extraScopes.dismissKey, 'api,write_repository');

    assert.deepEqual(texts(token, { noExpiry: 'noExpiry', extraScopes: 'api,write_repository' }), []);
    assert.deepEqual(texts(token, { extraScopes: 'api,write_repository' }), [
      'Токен бессрочный — лучше пересоздать его со сроком действия',
    ]);
    assert.deepEqual(texts({ ...token, scopes: ['read_api', 'api'] }, { noExpiry: 'noExpiry' }), [
      'У токена лишние права: api — достаточно read_api',
    ]);
    assert.deepEqual(texts({ ...token, scopes: ['read_api', 'api'] }, { extraScopes: 'api,write_repository' }), [
      'Токен бессрочный — лучше пересоздать его со сроком действия',
      'У токена лишние права: api — достаточно read_api',
    ]);
  });
});
