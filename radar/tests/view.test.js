import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { failureUrl, isHttpsUrl, isUnderBaseUrl } from '../src/links.js';
import {
  SECTION_IDS,
  actionableKeys,
  deriveView,
  hasActivePipelines,
  sectionsToSkip,
  tokenProblems,
  withStableStatuses,
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
  it('skips hidden sections', () => {
    assert.deepEqual(sectionsToSkip({ hiddenSections: ['mentioned', 'authored'] }), ['mentioned', 'authored']);
  });

  it('skips nothing when no section is hidden', () => {
    assert.deepEqual(sectionsToSkip({}), []);
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

describe('withStableStatuses', () => {
  const mrItem = (key, extra = {}) => ({ key, approvedByMe: false, draft: false, ...extra });
  const dashboard = (sections, extra = {}) => ({
    baseUrl: BASE_URL,
    username: 'me',
    filters: null,
    sections: { ...Object.fromEntries(SECTION_IDS.map((id) => [id, []])), ...sections },
    ...extra,
  });

  it('keeps my last known review state while GitLab does not report it', () => {
    const previous = dashboard({
      reviewing: [mrItem('1', { kind: 'mr', myReviewState: 'reviewed' }), mrItem('2', { kind: 'mr', myReviewState: null })],
    });
    const stored = JSON.parse(JSON.stringify(previous));
    assert.equal(withStableStatuses(stored, dashboard({ reviewing: [mrItem('1', { kind: 'mr' })] })).sections.reviewing[0].myReviewState, 'reviewed');
    const unknown = withStableStatuses(previous, dashboard({ reviewing: [mrItem('1', { kind: 'mr' }), mrItem('2', { kind: 'mr' })] }));
    assert.deepEqual(unknown.sections.reviewing.map((item) => item.myReviewState), ['reviewed', null]);
    const reset = withStableStatuses(
      unknown,
      dashboard({ reviewing: [mrItem('1', { kind: 'mr', myReviewState: null }), mrItem('2', { kind: 'mr', myReviewState: null })] }),
    );
    assert.deepEqual(reset.sections.reviewing.map((item) => item.myReviewState), [null, null]);
  });

  it('keeps the stable merge status while GitLab rechecks it', () => {
    const first = dashboard({ authored: [mrItem('1', { status: 'mergeable' })] });
    const checking = withStableStatuses(first, dashboard({ authored: [mrItem('1', { status: 'checking' })] }));
    assert.equal(checking.sections.authored[0].status, 'mergeable');
    const legacy = dashboard({ authored: [mrItem('1', { status: 'can_be_merged' })] });
    const second = withStableStatuses(legacy, dashboard({ authored: [mrItem('1', { status: 'cannot_be_merged_recheck' })] }));
    const third = withStableStatuses(second, dashboard({ authored: [mrItem('1', { status: 'unchecked' })] }));
    assert.equal(third.sections.authored[0].status, 'can_be_merged');
  });

  it('keeps a transient status when nothing stable is known or the source changed', () => {
    const previous = dashboard({ authored: [mrItem('1', { status: 'unchecked' })] });
    const next = withStableStatuses(
      previous,
      dashboard({ authored: [mrItem('1', { status: 'checking' }), mrItem('2', { status: 'checking' })] }),
    );
    assert.deepEqual(next.sections.authored.map((item) => item.status), ['checking', 'checking']);
    const stable = dashboard({ authored: [mrItem('1', { status: 'mergeable' })] });
    const checking = dashboard({ authored: [mrItem('1', { status: 'checking' })] });
    [{ username: 'other' }, { baseUrl: 'https://other.example.com' }, { filters: { selectedProjectIds: [1] } }].forEach(
      (source) => {
        assert.equal(withStableStatuses({ ...stable, ...source }, checking).sections.authored[0].status, 'checking');
      },
    );
  });
});

describe('actionableKeys', () => {
  it('returns the keys behind the badge counts', () => {
    const sections = {
      reviewing: [{ key: 'a' }, { key: 'b', approvedByMe: true }],
      assigned: [],
      mentioned: [{ key: 'c' }],
      awaitingApproval: [{ key: 'd', myReviewState: 'requested_changes' }, { key: 'e' }],
      threads: [{ key: 'a' }],
      authored: [{ key: 'f', pipeline: { status: 'failed' } }, { key: 'g', pipeline: { status: 'success' } }],
    };
    const { waiting, failedCi } = actionableKeys(sections);
    assert.deepEqual([...waiting].sort(), ['a', 'c', 'e']);
    assert.deepEqual([...failedCi], ['f']);
  });
});

describe('tokenProblems with allowed scopes', () => {
  it('does not flag scopes the caller allows', () => {
    const token = { expiresAt: '2027-01-01T00:00:00Z', scopes: ['read_api', 'read_repository'] };
    const now = Date.parse('2026-11-09T00:00:00Z');
    assert.deepEqual(tokenProblems(token, {}, now, ['read_api', 'read_repository']), []);
    assert.match(tokenProblems({ ...token, scopes: ['api'] }, {}, now, ['read_api', 'read_repository'])[0].text, /достаточно read_api, read_repository/);
  });
});
