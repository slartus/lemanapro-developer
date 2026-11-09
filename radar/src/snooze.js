const HOUR_MS = 60 * 60 * 1000;
const MORNING_HOUR = 9;
const SUNDAY = 0;
const MONDAY = 1;
const SATURDAY = 6;

const SNOOZE_OPTIONS = {
  hour: { label: 'на 1 ч' },
  tomorrow: { label: 'до утра', title: 'До 9:00 следующего рабочего дня' },
  monday: { label: 'до понедельника', title: 'До 9:00 ближайшего понедельника' },
  untilChange: { label: 'до изменений' },
  forever: { label: 'навсегда', title: 'Скрыть навсегда' },
};

export function snoozeOptions(sectionId) {
  return sectionId === 'possibleMrs'
    ? { ...SNOOZE_OPTIONS }
    : Object.fromEntries(Object.entries(SNOOZE_OPTIONS).filter(([option]) => option !== 'forever'));
}

const isWeekend = (date) => date.getDay() === SATURDAY || date.getDay() === SUNDAY;

function nextMorning(now, accept) {
  const date = new Date(now);
  do {
    date.setDate(date.getDate() + 1);
  } while (!accept(date));
  date.setHours(MORNING_HOUR, 0, 0, 0);
  return date;
}

export function createSnooze(option, item, now = Date.now()) {
  if (option === 'hour') {
    return { until: new Date(now + HOUR_MS).toISOString() };
  }
  if (option === 'tomorrow') {
    return { until: nextMorning(now, (date) => !isWeekend(date)).toISOString() };
  }
  if (option === 'monday') {
    return { until: nextMorning(now, (date) => date.getDay() === MONDAY).toISOString() };
  }
  if (option === 'untilChange') {
    return { updatedAt: item.updatedAt };
  }
  if (option === 'forever') {
    return { forever: true };
  }
  throw new Error(`Unknown snooze option: ${option}`);
}

export function isSnoozeActive(snooze, item, now = Date.now()) {
  if (!snooze) {
    return false;
  }
  if (snooze.forever) {
    return true;
  }
  if (snooze.until) {
    return now < Date.parse(snooze.until);
  }
  if (snooze.updatedAt) {
    return Date.parse(item.updatedAt) <= Date.parse(snooze.updatedAt);
  }
  return false;
}

export function nextSnoozeExpiry(snoozed, now = Date.now()) {
  const expiries = Object.values(snoozed ?? {})
    .filter((snooze) => snooze?.until)
    .map((snooze) => Date.parse(snooze.until))
    .filter((time) => time > now);
  return expiries.length ? Math.min(...expiries) : null;
}

export function pruneSnoozed(snoozed, itemsByKey, now = Date.now()) {
  return Object.fromEntries(
    Object.entries(snoozed ?? {}).filter(
      ([key, snooze]) => snooze?.forever || (itemsByKey.has(key) && isSnoozeActive(snooze, itemsByKey.get(key), now)),
    ),
  );
}
