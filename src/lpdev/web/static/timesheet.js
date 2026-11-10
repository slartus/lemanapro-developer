import {
  buildSubmitEntries,
  cellState,
  cellValue,
  dayTotals,
  formatActivity,
  formatHours,
  groupEntriesByDay,
  hiddenDays,
  isEditableDay,
  isSafeUrl,
  isUncertain,
  isValidIssueKey,
  parseHours,
  retryEntries,
  rowTotals,
  splitRows,
  stepMinutes,
  stepOf,
  summarizeResults,
  visibleDays,
  workedOn,
} from '/static/timesheet-model.js';

const WEEKDAYS = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'];
const SOURCE_LABELS = {
  push: 'push',
  mr: 'MR',
  review: 'ревью',
  comment: 'комментарий',
  status: 'статус',
  in_progress: 'в работе',
  manual: 'вручную',
};
const SOURCE_TITLES = {
  push: 'Были push в ветки задачи',
  mr: 'Работа с MR задачи',
  review: 'Ревью MR задачи',
  comment: 'Комментарии в задаче',
  status: 'Вы меняли статус задачи',
  in_progress: 'Задача была у вас в работе на этой неделе',
  manual: 'Добавлена вручную',
};
const KIND_LABELS = { holiday: 'праздник', weekend: 'выходной' };
const SUBMISSION_TITLES = {
  failed: 'Прошлое списание не удалось — можно повторить',
  unknown: 'Статус неизвестен — проверяется при обновлении',
  pending: 'Статус неизвестен — проверяется при обновлении',
};
const SUBMIT_DISABLED_TITLE = 'Запись в Tracker выключена (tracker.submit_enabled)';
const JSON_HEADERS = { 'Content-Type': 'application/json', 'X-Requested-With': 'lpdev' };

const $ = (id) => document.getElementById(id);
const state = {
  data: null,
  week: new URLSearchParams(location.search).get('week'),
  showWeekend: false,
  showQuiet: false,
  rowOrder: [],
  opening: false,
  saveErrors: new Map(),
  loadSeq: 0,
  loadError: null,
  inputs: new Map(),
  saving: 0,
  saveChain: Promise.resolve(),
  menuDate: null,
  menuAnchor: null,
  menuClosedAt: 0,
  rendering: false,
  dialog: { sending: false, changed: false },
};

function el(tag, { className, text, attrs } = {}, children = []) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  Object.entries(attrs ?? {}).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== false) node.setAttribute(key, value === true ? '' : value);
  });
  node.append(...children.filter(Boolean));
  return node;
}

function button(text, { className, title, attrs, onClick } = {}) {
  const node = el('button', { className, text, attrs: { type: 'button', title, ...attrs } });
  if (onClick) node.addEventListener('click', onClick);
  return node;
}

function setStatus(text, kind) {
  const status = $('ts-status');
  status.textContent = text;
  status.className = `small ${kind || 'muted'}`;
}

function plural(count, forms) {
  const mod10 = count % 10;
  const mod100 = count % 100;
  if (mod10 === 1 && mod100 !== 11) return forms[0];
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return forms[1];
  return forms[2];
}

function parseDate(iso) {
  const [year, month, day] = iso.split('-').map(Number);
  return new Date(Date.UTC(year, month - 1, day));
}

const shortDateFormat = new Intl.DateTimeFormat('ru-RU', { day: '2-digit', month: '2-digit', timeZone: 'UTC' });
const longDateFormat = new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'long', timeZone: 'UTC' });

const shortDate = (iso) => shortDateFormat.format(parseDate(iso));
const dayIndex = (date) => state.data.days.findIndex((day) => day.date === date);
const dayLabel = (date) => `${WEEKDAYS[dayIndex(date)] ?? ''} ${shortDate(date)}`.trim();

function weekLabel(data) {
  const number = String(data.week).split('-W')[1] ?? data.week;
  const first = data.days[0]?.date;
  const last = data.days.at(-1)?.date;
  if (!first || !last) return `Неделя ${number}`;
  const year = last.slice(0, 4);
  return `Неделя ${Number(number)} · ${longDateFormat.format(parseDate(first))} – ${longDateFormat.format(parseDate(last))} ${year}`;
}

function errorDetail(body, status) {
  const detail = body?.detail;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) return detail.map((item) => item?.msg ?? String(item)).join('; ');
  return `HTTP ${status}`;
}

async function api(method, url, payload) {
  const options = { method, cache: 'no-store' };
  if (method !== 'GET') {
    options.headers = JSON_HEADERS;
    if (payload !== undefined) options.body = JSON.stringify(payload);
  }
  const response = await fetch(url, options);
  let body = null;
  try {
    body = await response.json();
  } catch (error) {
    body = null;
  }
  if (!response.ok) {
    const failure = new Error(errorDetail(body, response.status));
    failure.status = response.status;
    throw failure;
  }
  return body;
}

function setLoading(on, text) {
  const wrap = $('ts-wrap');
  const indicator = $('ts-loading');
  if (!on) {
    wrap.removeAttribute('aria-busy');
    indicator.hidden = true;
    return;
  }
  const hasTable = Boolean(state.data) && !state.loadError;
  wrap.setAttribute('aria-busy', 'true');
  indicator.classList.toggle('over', hasTable);
  $('ts-loading-text').textContent = text;
  indicator.hidden = false;
}

async function load(week = state.week, { busy } = {}) {
  const seq = ++state.loadSeq;
  const query = week ? `?week=${encodeURIComponent(week)}` : '';
  const switching = !state.data || week !== state.data.week;
  if (busy || switching) setLoading(true, switching && state.data ? 'Загружаю неделю…' : busy ? 'Обновляю…' : 'Загружаю неделю…');
  try {
    const data = await api('GET', `/api/timesheet${query}`);
    if (seq !== state.loadSeq) return;
    if (data.week !== state.data?.week) {
      state.saveErrors.clear();
      state.showWeekend = false;
      state.showQuiet = false;
    }
    state.data = data;
    state.week = data.week;
    state.loadError = null;
    const url = new URL(location.href);
    url.searchParams.set('week', data.week);
    history.replaceState(null, '', url);
  } catch (error) {
    if (seq !== state.loadSeq) return;
    state.loadError = error.message || String(error);
  }
  setLoading(false);
  render();
}

function trackerState() {
  const tracker = state.data?.tracker ?? {};
  return { enabled: Boolean(tracker.enabled), submitEnabled: Boolean(tracker.enabled && tracker.submit_enabled) };
}

function applySubmitAvailability(node, entriesCount) {
  const tracker = trackerState();
  node.hidden = !tracker.enabled;
  if (!tracker.submitEnabled) {
    node.disabled = true;
    node.title = SUBMIT_DISABLED_TITLE;
  } else if (entriesCount === 0) {
    node.disabled = true;
    node.title = 'Нечего списывать';
  } else {
    node.disabled = false;
    node.title = '';
  }
}

function renderBanners() {
  const banners = [];
  if (state.loadError) {
    banners.push(
      el('div', { className: 'flash err', attrs: { role: 'alert' } }, [
        el('span', { text: `Не удалось загрузить таймшит: ${state.loadError} ` }),
        button('Повторить', { onClick: () => load(state.week, { busy: true }) }),
      ]),
    );
  }
  const data = state.data;
  if (data) {
    if (data.calendar_ok === false) {
      banners.push(
        el('div', { className: 'flash warn', text: 'Производственный календарь на год не загружен, нормы — 8 ч Пн–Пт' }),
      );
    }
    if (!data.tracker?.enabled) {
      banners.push(el('div', { className: 'flash warn', text: 'Tracker не настроен — см. README, раздел «Таймшит»' }));
    }
    (Array.isArray(data.warnings) ? data.warnings : [])
      .filter((warning) => typeof warning === 'string' && warning)
      .forEach((warning) => banners.push(el('div', { className: 'flash warn', text: warning })));
    if (data.tracker?.error) {
      banners.push(el('div', { className: 'flash err', attrs: { role: 'alert' }, text: `Tracker: ${data.tracker.error}` }));
    }
  }
  $('banners').replaceChildren(...banners);
}

function renderToolbar() {
  const data = state.data;
  $('week-label').textContent = data ? weekLabel(data) : '…';
  $('prev-week').disabled = !data?.prev_week;
  $('next-week').disabled = !data?.next_week;
  $('current-week').disabled = !data;
  const toggle = $('weekend-toggle');
  const hidden = data ? hiddenDays(data, { showWeekend: false }) : [];
  toggle.hidden = hidden.length === 0;
  toggle.setAttribute('aria-pressed', String(state.showWeekend));
  toggle.textContent = state.showWeekend ? 'Скрыть Сб/Вс' : `Показать ${hidden.map((day) => WEEKDAYS[dayIndex(day.date)]).join('/')}`;
  $('reset-week').disabled = !data;
  const step = formatHours(stepOf(data));
  $('step-hint').textContent = `Ввод: часы с шагом ${step}, ↑/↓ — ±${step}, Enter — вниз, Esc — откат`;
  refreshSubmitButtons();
}

function refreshSubmitButtons() {
  const data = state.data;
  applySubmitAvailability($('submit-week'), data ? buildSubmitEntries(data, { type: 'week' }).length : 0);
  const menu = $('day-menu');
  if (data && state.menuDate && menu.matches(':popover-open')) updateDayMenu(state.menuDate);
}

function normText(day) {
  if (day.kind === 'holiday' || day.kind === 'weekend') {
    return day.norm > 0 ? `${formatHours(day.norm)} ч` : KIND_LABELS[day.kind];
  }
  return day.kind === 'short' ? `${formatHours(day.norm)} ч · сокр.` : `${formatHours(day.norm)} ч`;
}

function renderHead(days) {
  const cells = days.map((day) => {
    const index = dayIndex(day.date);
    const isToday = day.date === state.data.today;
    const menuButton = button('⋯', {
      className: 'day-menu-btn',
      title: `Действия: ${dayLabel(day.date)}`,
      attrs: { 'aria-haspopup': 'menu', 'aria-expanded': 'false', 'aria-label': `Действия: ${dayLabel(day.date)}` },
      onClick: (event) => openDayMenu(event.currentTarget, day.date),
    });
    return el('th', { className: `day${isToday ? ' today' : ''}`, attrs: { scope: 'col', 'aria-current': isToday ? 'date' : null } }, [
      el('div', { className: 'day-head' }, [
        el('div', {}, [
          el('span', { className: 'day-name', text: `${WEEKDAYS[index]} ${shortDate(day.date)}` }),
          el('span', { className: `day-norm ${day.kind}`, text: normText(day) }),
        ]),
        menuButton,
      ]),
    ]);
  });
  $('ts-head').replaceChildren(
    el('tr', {}, [
      el('th', { className: 'task', text: 'Задача', attrs: { scope: 'col' } }),
      ...cells,
      el('th', { className: 'sum', text: 'Итого', attrs: { scope: 'col' } }),
    ]),
  );
}

function displayTitle(row) {
  const title = row.title ?? '';
  return title.startsWith(row.key) ? title.slice(row.key.length).replace(/^[\s:·—-]+/, '') : title;
}

function activityLine(activity) {
  const parts = activity.flatMap((item, index) => [
    index ? ', ' : null,
    el('span', { text: item.text, attrs: { title: item.title } }),
  ]);
  return el('span', { className: 'activity', attrs: { title: activity.map((item) => item.title).join('\n') } }, parts);
}

function taskCell(row) {
  const key = isSafeUrl(row.url)
    ? el('a', { className: 'task-key', text: row.key, attrs: { href: row.url, target: '_blank', rel: 'noopener noreferrer' } })
    : el('span', { className: 'task-key', text: row.key });
  const sources = (row.sources ?? []).map((source) =>
    el('span', { className: `src ${source}`, text: SOURCE_LABELS[source] ?? source, attrs: { title: SOURCE_TITLES[source] } }),
  );
  const activity = formatActivity(row.activity);
  const status = row.status?.display
    ? el('span', { className: 'issue-status', text: row.status.display, attrs: { title: `Статус в Tracker: ${row.status.key}` } })
    : null;
  return el('th', { className: 'task', attrs: { scope: 'row' } }, [
    status ? el('div', { className: 'task-head' }, [key, status]) : key,
    displayTitle(row) ? el('div', { className: 'task-title', text: displayTitle(row), attrs: { title: row.title } }) : null,
    sources.length || activity.length ? el('div', { className: 'sources' }, [...sources, activity.length ? activityLine(activity) : null]) : null,
  ]);
}

function inputText(cell) {
  if (cell?.edited != null) return formatHours(cell.edited);
  const value = cellValue(cell);
  return value ? formatHours(value) : '';
}

function loggedLine(cell) {
  const logged = cell?.logged ?? 0;
  return logged > 0
    ? el('span', { className: 'logged-line', text: `✓ ${formatHours(logged)}`, attrs: { title: 'Уже списано в Tracker' } })
    : el('span', { className: 'logged-line none', text: '✓ 0', attrs: { 'aria-hidden': 'true' } });
}

function dataCell(row, day) {
  const cell = row.cells?.[day.date];
  const stateName = cellState(cell, day, state.data.today);
  const editable = isEditableDay(day, state.data.today);
  const isToday = day.date === state.data.today;
  const label = `${row.key}, ${dayLabel(day.date)}: часов к списанию`;
  const step = formatHours(stepOf(state.data));
  const input = el('input', {
    className: 'cell-input',
    attrs: {
      type: 'text',
      inputmode: 'decimal',
      autocomplete: 'off',
      'aria-label': label,
      placeholder: '·',
      disabled: !editable,
      title: editable ? null : 'День недоступен для списания',
    },
  });
  input.value = inputText(cell);
  input.dataset.key = row.key;
  input.dataset.date = day.date;
  input.dataset.original = input.value;
  const saveError = state.saveErrors.get(`${row.key}|${day.date}`);
  if (saveError) {
    input.classList.add('invalid');
    input.setAttribute('aria-invalid', 'true');
    input.title = saveError;
  }
  if (editable) {
    bindInput(input);
    state.inputs.set(`${row.key}|${day.date}`, input);
  }

  const box = el('div', { className: 'cell-box' });
  if (editable) {
    box.append(
      button('−', {
        className: 'step-btn minus',
        attrs: { 'aria-label': `${label}: −${step}`, tabindex: '-1' },
        onClick: () => stepAndSave(input, -1),
      }),
    );
  }
  box.append(loggedLine(cell), input);
  if (editable) {
    box.append(
      button('+', {
        className: 'step-btn plus',
        attrs: { 'aria-label': `${label}: +${step}`, tabindex: '-1' },
        onClick: () => stepAndSave(input, 1),
      }),
    );
  }
  const actions = el('span', { className: 'cell-actions' });
  if (editable && cell?.edited != null) {
    actions.append(
      button('↺', {
        className: 'reset-btn',
        title: `Вернуть предложение (${formatHours(cell.suggested ?? 0)} ч)`,
        attrs: { 'aria-label': `${row.key}, ${dayLabel(day.date)}: вернуть предложение` },
        onClick: () => save({ key: row.key, date: day.date, minutes: null }),
      }),
    );
  }
  const tracker = trackerState();
  if (editable && tracker.enabled && !isUncertain(cell)) {
    const suggested = cellValue(cell) > 0;
    actions.append(
      button(suggested ? '✓' : '+', {
        className: suggested ? 'submit-cell-btn' : 'submit-cell-btn extra',
        title: tracker.submitEnabled
          ? suggested ? `Списать ${formatHours(cellValue(cell))} ч` : 'Списать время на задачу вручную'
          : SUBMIT_DISABLED_TITLE,
        attrs: {
          'aria-label': `${row.key}, ${dayLabel(day.date)}: ${suggested ? 'списать' : 'списать вручную'}`,
          disabled: !tracker.submitEnabled,
        },
        onClick: () => openSubmitDialog({ type: 'cell', key: row.key, date: day.date }),
      }),
    );
  }
  if (cell?.submission && SUBMISSION_TITLES[cell.submission]) {
    actions.append(
      el('span', {
        className: 'flag',
        text: cell.submission === 'pending' ? '…' : '!',
        attrs: { title: SUBMISSION_TITLES[cell.submission], 'aria-label': SUBMISSION_TITLES[cell.submission], role: 'img' },
      }),
    );
  }
  box.append(actions);
  const uncertain = isUncertain(cell);
  return el('td', { className: `day st-${stateName}${isToday ? ' today' : ''}` }, [
    el('div', { className: 'cell' }, [
      box,
      uncertain
        ? button('Обновить', {
            className: 'refresh-btn',
            title: SUBMISSION_TITLES[cell.submission],
            attrs: { 'aria-label': `${row.key}, ${dayLabel(day.date)}: ${SUBMISSION_TITLES[cell.submission]}. Обновить` },
            onClick: () => load(state.week, { busy: true }),
          })
        : null,
    ]),
  ]);
}

function sumCell(totals) {
  return el('td', { className: 'sum' }, [
    el('span', { text: formatHours(totals.total) }),
    totals.logged > 0 ? el('span', { className: 'sum-sub', text: `✓ ${formatHours(totals.logged)}` }) : null,
  ]);
}

function unkeyedRow(days) {
  const unkeyed = state.data.unkeyed;
  if (!unkeyed) return null;
  const items = (unkeyed.items ?? []).map((item) =>
    el('li', {}, [
      isSafeUrl(item.url)
        ? el('a', { text: item.title || item.url, attrs: { href: item.url, target: '_blank', rel: 'noopener noreferrer' } })
        : el('span', { text: item.title ?? '' }),
    ]),
  );
  let total = 0;
  const cells = days.map((day) => {
    const minutes = unkeyed.cells?.[day.date]?.suggested ?? 0;
    total += minutes;
    return el('td', { className: `day${day.date === state.data.today ? ' today' : ''}` }, [
      el('span', { className: 'unkeyed-value', text: minutes ? formatHours(minutes) : '' }),
    ]);
  });
  return el('tr', { className: 'unkeyed', attrs: { title: 'Активность без ключа задачи — не списывается и не входит в итог' } }, [
    el('th', { className: 'task', attrs: { scope: 'row' } }, [
      el('details', {}, [
        el('summary', { text: `Без задачи${items.length ? ` (${items.length})` : ''}` }),
        items.length ? el('ul', {}, items) : el('div', { className: 'small', text: 'Нет деталей' }),
      ]),
    ]),
    ...cells,
    el('td', { className: 'sum' }, [el('span', { className: 'unkeyed-value', text: total ? formatHours(total) : '' })]),
  ]);
}

function unkeyedNote(total) {
  if (total.under <= 0 || total.unkeyed <= 0) return '';
  return `недобор ${formatHours(total.under)}, из них ${formatHours(Math.min(total.unkeyed, total.under))} ч — без задачи`;
}

function totalText(day, total) {
  if (day.norm > 0) return `${formatHours(total.total)} / ${formatHours(day.norm)}`;
  const label = KIND_LABELS[day.kind] ?? '';
  return label ? `${formatHours(total.total)} · ${label}` : formatHours(total.total);
}

function renderFoot(days) {
  const totals = new Map(dayTotals(state.data).map((total) => [total.date, total]));
  let weekTotal = 0;
  let weekNorm = 0;
  const cells = days.map((day) => {
    const total = totals.get(day.date);
    weekTotal += total.total;
    weekNorm += total.norm;
    const over = total.over > 0;
    return el(
      'td',
      {
        className: `sum${over ? ' over' : ''}${day.date === state.data.today ? ' today' : ''}`,
        attrs: { title: `Списано ${formatHours(total.logged)} ч + к списанию ${formatHours(total.planned)} ч` },
      },
      [
        el('span', { className: 'total', text: totalText(day, total) }),
        over ? el('span', { className: 'over-tag', text: `+${formatHours(total.over)}`, attrs: { 'aria-label': `перебор ${formatHours(total.over)} ч` } }) : null,
        total.logged > 0 ? el('span', { className: 'sum-sub', text: `✓ ${formatHours(total.logged)}` }) : null,
        unkeyedNote(total) ? el('span', { className: 'under-note', text: unkeyedNote(total) }) : null,
      ],
    );
  });
  hiddenDays(state.data, { showWeekend: state.showWeekend }).forEach((day) => {
    const total = totals.get(day.date);
    weekTotal += total.total;
    weekNorm += total.norm;
  });
  $('ts-foot').replaceChildren(
    el('tr', {}, [
      el('th', { className: 'task', text: 'Итого за день', attrs: { scope: 'row' } }),
      ...cells,
      el('td', { className: `sum${weekTotal > weekNorm ? ' over' : ''}` }, [
        el('span', { className: 'total', text: `${formatHours(weekTotal)} / ${formatHours(weekNorm)}` }),
      ]),
    ]),
  );
}

function quietToggleRow(count, span) {
  if (count === 0) return null;
  const toggle = button(
    state.showQuiet ? `Скрыть ${count} ${plural(count, ['задачу', 'задачи', 'задач'])} без часов` : `Ещё ${count} ${plural(count, ['задача', 'задачи', 'задач'])} с активностью без часов`,
    {
      className: 'quiet-btn',
      title: 'Ревью, комментарии и прочая активность, по которой не предложено часов',
      attrs: { 'aria-expanded': String(state.showQuiet) },
      onClick: () => {
        state.showQuiet = !state.showQuiet;
        renderTable();
      },
    },
  );
  return el('tr', { className: 'quiet-toggle' }, [el('td', { attrs: { colspan: String(span) } }, [toggle])]);
}

function captureFocus() {
  const active = document.activeElement;
  if (!active?.classList?.contains('cell-input')) return null;
  return {
    key: active.dataset.key,
    date: active.dataset.date,
    value: active.value,
    dirty: active.value !== active.dataset.original,
    invalid: active.classList.contains('invalid'),
    selectionStart: active.selectionStart,
    selectionEnd: active.selectionEnd,
  };
}

function restoreFocus(snapshot) {
  if (!snapshot) return;
  const input = state.inputs.get(`${snapshot.key}|${snapshot.date}`);
  if (!input) return;
  if (snapshot.dirty) {
    input.value = snapshot.value;
    input.classList.toggle('invalid', snapshot.invalid);
  }
  input.focus({ preventScroll: true });
  try {
    input.setSelectionRange(snapshot.selectionStart, snapshot.selectionEnd);
  } catch (error) {
    input.select();
  }
}

function renderTable() {
  const data = state.data;
  if (!data) return;
  const snapshot = captureFocus();
  state.rendering = true;
  state.inputs = new Map();
  const days = visibleDays(data, { showWeekend: state.showWeekend });
  renderHead(days);
  const { active, quiet } = splitRows(data);
  const shownQuiet = state.showQuiet ? quiet : [];
  state.rowOrder = [...active, ...shownQuiet].map((row) => row.key);
  const rowNode = (row, className) =>
    el('tr', { className }, [taskCell(row), ...days.map((day) => dataCell(row, day)), sumCell(rowTotals(row))]);
  $('ts-body').replaceChildren(
    ...active.map((row) => rowNode(row)),
    unkeyedRow(days) ?? '',
    quietToggleRow(quiet.length, days.length + 2) ?? '',
    ...shownQuiet.map((row) => rowNode(row, 'quiet')),
  );
  $('ts-empty').hidden = (data.rows ?? []).length > 0 || Boolean(data.unkeyed);
  renderFoot(days);
  state.rendering = false;
  restoreFocus(snapshot);
}

function render() {
  renderBanners();
  renderToolbar();
  renderTable();
}

function saveErrorText() {
  const [first] = state.saveErrors.values();
  if (!first) return '';
  const more = state.saveErrors.size > 1 ? ` (и ещё ${state.saveErrors.size - 1})` : '';
  return `${first}${more}`;
}

function save(payload) {
  const cellKey = `${payload.key}|${payload.date}`;
  state.saving += 1;
  setStatus('Сохраняю…');
  state.saveChain = state.saveChain
    .then(() => api('PUT', '/api/timesheet/cell', payload))
    .then(
      () => {
        state.saveErrors.delete(cellKey);
        if (state.saveErrors.size) setStatus(saveErrorText(), 'err');
        else setStatus('Сохранено', 'ok');
      },
      (error) => {
        state.saveErrors.set(cellKey, `Не сохранилось (${payload.key}, ${shortDate(payload.date)}): ${error.message}`);
        setStatus(saveErrorText(), 'err');
      },
    )
    .finally(() => {
      state.saving -= 1;
      if (state.saving === 0) return load();
    });
  return state.saveChain;
}

function commitInput(input) {
  const minutes = parseHours(input.value, stepOf(state.data));
  if (minutes === null) {
    input.classList.add('invalid');
    input.setAttribute('aria-invalid', 'true');
    setStatus(`Не понял «${input.value}» — введите часы, например 1.5 (Esc — откат)`, 'err');
    return;
  }
  input.classList.remove('invalid');
  input.removeAttribute('aria-invalid');
  const original = input.dataset.original;
  const normalized = minutes === 0 && original === '' ? '' : formatHours(minutes);
  input.value = normalized;
  if (normalized === original) return;
  input.dataset.original = normalized;
  save({ key: input.dataset.key, date: input.dataset.date, minutes });
}

function columnNeighbour(input, direction) {
  const keys = state.rowOrder;
  let index = keys.indexOf(input.dataset.key) + direction;
  while (index >= 0 && index < keys.length) {
    const next = state.inputs.get(`${keys[index]}|${input.dataset.date}`);
    if (next) return next;
    index += direction;
  }
  return null;
}

function steppedValue(input, direction) {
  const step = stepOf(state.data);
  const base = parseHours(input.value, step) ?? parseHours(input.dataset.original, step) ?? 0;
  return formatHours(stepMinutes(base, direction, step));
}

function stepAndSave(input, direction) {
  input.value = steppedValue(input, direction);
  commitInput(input);
}

function bindInput(input) {
  input.addEventListener('focus', () => input.select());
  input.addEventListener('blur', () => {
    if (!state.rendering) commitInput(input);
  });
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      const next = columnNeighbour(input, event.shiftKey ? -1 : 1);
      if (next) next.focus();
      else commitInput(input);
    } else if (event.key === 'Escape') {
      event.preventDefault();
      input.value = input.dataset.original;
      input.classList.remove('invalid');
      input.removeAttribute('aria-invalid');
      input.removeAttribute('title');
      state.saveErrors.delete(`${input.dataset.key}|${input.dataset.date}`);
      setStatus(saveErrorText(), state.saveErrors.size ? 'err' : undefined);
      input.select();
    } else if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
      event.preventDefault();
      input.value = steppedValue(input, event.key === 'ArrowUp' ? 1 : -1);
      input.classList.remove('invalid');
    }
  });
}

function openDayMenu(anchor, date) {
  const menu = $('day-menu');
  if (state.menuDate === date && (menu.matches(':popover-open') || performance.now() - state.menuClosedAt < 250)) {
    if (menu.matches(':popover-open')) menu.hidePopover();
    return;
  }
  state.menuDate = date;
  state.menuAnchor = anchor;
  updateDayMenu(date);
  menu.setAttribute('aria-label', `Действия: ${dayLabel(date)}`);
  menu.showPopover();
  const rect = anchor.getBoundingClientRect();
  const width = menu.offsetWidth;
  menu.style.top = `${Math.min(rect.bottom + 4, window.innerHeight - menu.offsetHeight - 8)}px`;
  menu.style.left = `${Math.max(8, Math.min(rect.right - width, window.innerWidth - width - 8))}px`;
  anchor.setAttribute('aria-expanded', 'true');
  menu.querySelector('button:not([hidden]):not(:disabled)')?.focus();
}

function updateDayMenu(date) {
  const day = state.data.days.find((item) => item.date === date);
  const submitItem = $('day-menu').querySelector('[data-action="submit-day"]');
  applySubmitAvailability(submitItem, buildSubmitEntries(state.data, { type: 'day', date }).length);
  if (!isEditableDay(day, state.data.today)) {
    submitItem.disabled = true;
    submitItem.title = 'День недоступен для списания';
  }
}

async function resetCells(date) {
  const question = date
    ? `Сбросить ручные правки за ${dayLabel(date)} и вернуть предложения?`
    : 'Сбросить все ручные правки за неделю и вернуть предложения?';
  if (!window.confirm(question)) return;
  const params = new URLSearchParams({ week: state.data.week });
  if (date) params.set('date', date);
  setStatus('Сбрасываю…');
  try {
    await api('DELETE', `/api/timesheet/cells?${params}`);
    [...state.saveErrors.keys()]
      .filter((cellKey) => !date || cellKey.endsWith(`|${date}`))
      .forEach((cellKey) => state.saveErrors.delete(cellKey));
    setStatus('Правки сброшены', 'ok');
  } catch (error) {
    setStatus(`Не удалось сбросить: ${error.message}`, 'err');
  }
  await load();
}

async function addTask(event) {
  event.preventDefault();
  const input = $('add-key');
  const errorBox = $('add-error');
  const key = input.value.trim().toUpperCase();
  input.value = key;
  if (!isValidIssueKey(key)) {
    input.classList.add('invalid');
    input.setAttribute('aria-invalid', 'true');
    errorBox.textContent = 'Ключ вида SUPERAPP-123';
    input.focus();
    return;
  }
  if (state.data?.rows?.some((row) => row.key === key)) {
    errorBox.textContent = `${key} уже в таблице`;
    return;
  }
  input.classList.remove('invalid');
  input.removeAttribute('aria-invalid');
  errorBox.textContent = '';
  const submit = event.target.querySelector('button[type="submit"]');
  submit.disabled = true;
  try {
    await api('POST', '/api/timesheet/tasks', { key, week: state.data.week });
    input.value = '';
    setStatus(`${key} добавлена`, 'ok');
    await load();
  } catch (error) {
    errorBox.textContent = error.status === 422 ? error.message : `Не удалось добавить: ${error.message}`;
  } finally {
    submit.disabled = false;
  }
}

function dialogTitle(scope) {
  if (scope.type === 'cell') return `Списать ${scope.key} · ${dayLabel(scope.date)}`;
  if (scope.type === 'day') return `Списать ${dayLabel(scope.date)}`;
  return `Списать: ${weekLabel(state.data)}`;
}

function setDialog(title, bodyNodes, actions) {
  $('dlg-title').textContent = title;
  $('dlg-body').replaceChildren(...bodyNodes);
  $('dlg-actions').replaceChildren(...actions);
}

function closeDialog() {
  const dialog = $('submit-dialog');
  if (state.dialog.sending) return;
  dialog.close();
}

function dayWarning(group) {
  if (group.over > 0) return el('span', { className: 'dlg-warn', text: `перебор +${formatHours(group.over)} ч` });
  if (group.under > 0) {
    const note = group.unkeyed > 0 ? `, из них ${formatHours(Math.min(group.unkeyed, group.under))} ч — без задачи` : '';
    return el('span', { className: 'dlg-warn under', text: `недобор ${formatHours(group.under)} ч${note}` });
  }
  return null;
}

function statusWarning(row, date) {
  if (workedOn(row.activity, date) !== false) return null;
  const now = row.status?.display ? ` Сейчас: «${row.status.display}».` : '';
  return el('p', { className: 'dlg-status-warn', text: `В этот день задача не была у вас в работе.${now} Проверьте, что списываете на нужную задачу.` });
}

function cellConfirmView(scope) {
  const title = dialogTitle(scope);
  const row = state.data.rows.find((item) => item.key === scope.key);
  const day = state.data.days.find((item) => item.date === scope.date);
  const cell = row?.cells?.[scope.date];
  if (!row || !day || isUncertain(cell)) {
    setDialog(title, [el('p', { text: 'Списать нельзя: ячейка изменилась или ждёт сверки, обновите неделю.' })], [
      button('Закрыть', { onClick: closeDialog }),
    ]);
    return null;
  }
  const step = stepOf(state.data);
  const input = el('input', {
    className: 'dlg-hours',
    attrs: { id: 'dlg-hours', type: 'text', inputmode: 'decimal', autocomplete: 'off', 'aria-describedby': 'dlg-hours-error' },
  });
  const shown = state.inputs.get(`${scope.key}|${scope.date}`);
  const current = shown ? parseHours(shown.value, step) ?? 0 : cellValue(cell);
  input.value = current > 0 ? formatHours(current) : '';
  input.placeholder = formatHours(step);
  const error = el('p', { className: 'dlg-errors', attrs: { id: 'dlg-hours-error', 'aria-live': 'polite' } });
  const otherPlanned = dayTotals(state.data).find((total) => total.date === scope.date);
  const logged = cell?.logged ?? 0;
  const body = [
    displayTitle(row) ? el('p', { className: 'dlg-task', text: displayTitle(row) }) : null,
    el('label', { className: 'dlg-field', attrs: { for: 'dlg-hours' } }, [el('span', { text: 'Часы к списанию' }), input]),
    error,
    el('p', {
      className: 'dlg-note',
      text: `${logged > 0 ? `Уже списано на задачу в этот день: ${formatHours(logged)} ч. ` : ''}За день списано ${formatHours(day.logged)} из ${formatHours(day.norm)} ч.`,
    }),
    otherPlanned && otherPlanned.over > 0 ? el('p', { className: 'dlg-note', text: `В таблице на этот день перебор +${formatHours(otherPlanned.over)} ч` }) : null,
    statusWarning(row, scope.date),
  ].filter(Boolean);
  const submit = () => {
    const minutes = parseHours(input.value, step);
    if (minutes == null || minutes <= 0) {
      error.textContent = `Укажите часы больше нуля с шагом ${formatHours(step)}`;
      input.setAttribute('aria-invalid', 'true');
      input.focus();
      return;
    }
    if (day.logged + minutes > day.norm) {
      error.textContent = `Вместе с уже списанным выйдет ${formatHours(day.logged + minutes)} ч — больше нормы дня ${formatHours(day.norm)} ч`;
      input.setAttribute('aria-invalid', 'true');
      input.focus();
      return;
    }
    send(title, [{ key: scope.key, date: scope.date, minutes }]);
  };
  input.addEventListener('input', () => {
    error.textContent = '';
    input.removeAttribute('aria-invalid');
  });
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      submit();
    }
  });
  setDialog(title, body, [button('Отменить', { onClick: closeDialog }), button('Отправить', { className: 'primary', onClick: submit })]);
  return input;
}

function confirmView(scope) {
  if (scope.type === 'cell') return cellConfirmView(scope);
  const entries = buildSubmitEntries(state.data, scope);
  const title = dialogTitle(scope);
  if (entries.length === 0) {
    setDialog(title, [el('p', { text: 'Нечего списывать: в доступных днях нет часов к списанию.' })], [
      button('Закрыть', { onClick: closeDialog }),
    ]);
    return;
  }
  const groups = groupEntriesByDay(state.data, entries);
  const body = [
    el('p', { className: 'dlg-note', text: 'Часы будут добавлены к уже списанному в Tracker, а не заменят его.' }),
    ...groups.flatMap((group) => {
      const day = state.data.days.find((item) => item.date === group.date);
      const totalLine = day.norm > 0
        ? `Итог дня: ${formatHours(group.total)} из ${formatHours(group.norm)} ч`
        : `Итог дня: ${formatHours(group.total)} ч · ${KIND_LABELS[day.kind] ?? 'норма 0'}`;
      return [
        el('h3', { text: `${dayLabel(group.date)} — ${formatHours(group.minutes)} ч` }),
        el(
          'ul',
          {},
          group.entries.map((entry) =>
            el('li', { text: `${entry.key}${entry.title ? ` · ${entry.title}` : ''} — ${formatHours(entry.minutes)} ч` }),
          ),
        ),
        el('div', { className: 'dlg-day-total' }, [el('span', { text: totalLine }), dayWarning(group)]),
        group.logged > 0
          ? el('p', { className: 'dlg-note', text: `Уже списано ${formatHours(group.logged)} ч — новые часы будут добавлены к ним` })
          : null,
      ].filter(Boolean);
    }),
  ];
  const confirmButton = button(`Списать ${entries.length} ${plural(entries.length, ['запись', 'записи', 'записей'])}`, {
    className: 'primary',
    onClick: () => send(title, entries),
  });
  setDialog(title, body, [button('Отмена', { onClick: closeDialog }), confirmButton]);
  return confirmButton;
}

async function send(title, entries) {
  state.dialog.sending = true;
  setDialog(title, [el('p', { text: `Отправляю ${entries.length} ${plural(entries.length, ['запись', 'записи', 'записей'])} в Tracker…` })], [
    button('Отмена', { attrs: { disabled: true } }),
    button('Отправка…', { className: 'primary', attrs: { disabled: true } }),
  ]);
  let results = null;
  let failure = null;
  try {
    const body = await api('POST', '/api/timesheet/submit', { entries });
    results = body?.results ?? [];
  } catch (error) {
    failure = error;
  }
  state.dialog.sending = false;
  state.dialog.changed = true;
  const single = entries.length === 1 && results?.length === 1 ? results[0] : null;
  if (single && (single.status === 'done' || single.status === 'skipped')) {
    const verb = single.status === 'done' ? 'Списано' : 'Уже было списано';
    $('submit-dialog').close();
    setStatus(`${verb}: ${single.key} · ${dayLabel(single.date)} · ${formatHours(single.minutes)} ч`);
    return;
  }
  if (failure) {
    setDialog(title, [el('p', { className: 'dlg-errors', text: `Не удалось отправить: ${failure.message}` })], [
      button('Закрыть', { onClick: closeDialog }),
      button('Повторить', { className: 'primary', onClick: () => send(title, entries) }),
    ]);
  } else {
    resultView(title, results);
  }
  const actions = $('dlg-actions');
  (actions.querySelector('button.primary') ?? actions.querySelector('button'))?.focus();
  load();
}

function resultView(title, results) {
  const summary = summarizeResults(results);
  const errorsCount = summary.failed + summary.unknown;
  const body = [
    el('p', {
      className: 'dlg-summary',
      text: `Успешно ${summary.done}, пропущено ${summary.skipped}, ошибок ${errorsCount}`,
    }),
  ];
  if (summary.errors.length) {
    body.push(
      el(
        'ul',
        { className: 'dlg-errors' },
        summary.errors.map((result) =>
          el('li', {
            text: `${result.key} · ${dayLabel(result.date)} · ${formatHours(result.minutes)} ч — ${
              result.status === 'unknown' ? 'статус неизвестен, проверьте в Tracker' : 'ошибка'
            }${result.error ? `: ${result.error}` : ''}`,
          }),
        ),
      ),
    );
  }
  if (summary.unknown > 0) {
    body.push(
      el('p', {
        className: 'dlg-note',
        text: 'Записи со статусом «неизвестен» не повторяются автоматически, чтобы не списать дважды.',
      }),
    );
  }
  const retry = retryEntries(results);
  const actions = [button('Закрыть', { onClick: closeDialog })];
  if (retry.length) {
    actions.push(button(`Повторить ошибочные (${retry.length})`, { className: 'primary', onClick: () => send(title, retry) }));
  }
  setDialog(title, body, actions);
}

async function openSubmitDialog(scope) {
  if (!state.data || !trackerState().submitEnabled || state.opening) return;
  const menu = $('day-menu');
  if (menu.matches(':popover-open')) menu.hidePopover();
  if (document.activeElement?.classList?.contains('cell-input')) document.activeElement.blur();
  state.opening = true;
  try {
    if (scope.type === 'cell') {
      await state.saveChain;
      if (state.saveErrors.has(`${scope.key}|${scope.date}`)) {
        setStatus(`${saveErrorText()} — исправьте или сбросьте правку перед списанием`, 'err');
        return;
      }
    } else {
      setStatus('Сохраняю правки и обновляю неделю…');
      await state.saveChain;
      if (state.saveErrors.size) {
        setStatus(`${saveErrorText()} — исправьте или сбросьте правку перед списанием`, 'err');
        return;
      }
      await load();
    }
  } finally {
    state.opening = false;
  }
  if (state.loadError || !state.data || !trackerState().submitEnabled) {
    setStatus('Не удалось обновить неделю перед списанием', 'err');
    return;
  }
  setStatus('');
  const dialog = $('submit-dialog');
  state.dialog = { sending: false, changed: false };
  const confirmButton = confirmView(scope);
  dialog.showModal();
  (confirmButton ?? $('dlg-actions').querySelector('button'))?.focus();
  if (confirmButton?.tagName === 'INPUT') confirmButton.select();
}

function init() {
  $('prev-week').addEventListener('click', () => state.data?.prev_week && load(state.data.prev_week));
  $('next-week').addEventListener('click', () => state.data?.next_week && load(state.data.next_week));
  $('current-week').addEventListener('click', () => load(null));
  $('weekend-toggle').addEventListener('click', () => {
    state.showWeekend = !state.showWeekend;
    render();
  });
  $('reset-week').addEventListener('click', () => state.data && resetCells(null));
  $('submit-week').addEventListener('click', () => openSubmitDialog({ type: 'week' }));
  $('add-task').addEventListener('submit', addTask);
  $('add-key').addEventListener('input', () => {
    $('add-key').classList.remove('invalid');
    $('add-key').removeAttribute('aria-invalid');
    $('add-error').textContent = '';
  });

  const menu = $('day-menu');
  menu.addEventListener('toggle', (event) => {
    if (event.newState === 'closed') {
      state.menuClosedAt = performance.now();
      state.menuAnchor?.setAttribute('aria-expanded', 'false');
    }
  });
  menu.addEventListener('click', (event) => {
    const action = event.target.closest('button')?.dataset.action;
    const date = state.menuDate;
    if (!action || !date) return;
    if (menu.matches(':popover-open')) menu.hidePopover();
    if (action === 'submit-day') openSubmitDialog({ type: 'day', date });
    if (action === 'reset-day') resetCells(date);
  });
  window.addEventListener('resize', () => menu.matches(':popover-open') && menu.hidePopover());

  const dialog = $('submit-dialog');
  dialog.addEventListener('cancel', (event) => {
    if (state.dialog.sending) event.preventDefault();
  });
  dialog.addEventListener('close', () => {
    if (state.dialog.changed) load(state.week, { busy: true });
  });

  window.addEventListener('beforeunload', (event) => {
    if (state.saving > 0 || state.dialog.sending) event.preventDefault();
  });

  load();
}

init();
