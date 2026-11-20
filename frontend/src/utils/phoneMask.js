export const COUNTRY_PRESETS = {
  KG: {
    code: '+996',
    prefixDigits: '996',
    label: 'KG',
    maxDigits: 9,
    placeholder: '770 73-00-00',
    demoDigits: '770730000',
  },
  RU: {
    code: '+7',
    prefixDigits: '7',
    label: 'RU / KZ',
    maxDigits: 10,
    placeholder: '(999) 123-45-67',
    demoDigits: '9991234567',
  },
};

/**
 * Форматирует введённые цифры номера по маске выбранной страны.
 * Для KG (+996): XXX XX-XX-XX (например, 770 73-00-00)
 * Для RU (+7): (XXX) XXX-XX-XX
 */
export function formatMaskedPhone(countryKey, rawDigits) {
  const preset = COUNTRY_PRESETS[countryKey] || COUNTRY_PRESETS.KG;
  const digits = rawDigits.replace(/\D/g, '').slice(0, preset.maxDigits);
  if (!digits) return '';

  if (countryKey === 'KG') {
    const p1 = digits.slice(0, 3);
    const p2 = digits.slice(3, 5);
    const p3 = digits.slice(5, 7);
    const p4 = digits.slice(7, 9);
    let out = p1;
    if (p2) out += ` ${p2}`;
    if (p3) out += `-${p3}`;
    if (p4) out += `-${p4}`;
    return out;
  }

  // RU / +7: (XXX) XXX-XX-XX
  const p1 = digits.slice(0, 3);
  const p2 = digits.slice(3, 6);
  const p3 = digits.slice(6, 8);
  const p4 = digits.slice(8, 10);
  let out = `(${p1}`;
  if (p1.length === 3 && digits.length > 3) out += `) ${p2}`;
  else if (p1.length === 3) out += ')';
  if (p3) out += `-${p3}`;
  if (p4) out += `-${p4}`;
  return out;
}

/**
 * Нормализует телефон строго в строку из 7–15 цифр без "+" и пробелов
 * (контракт app/api/otp.py: ^\d{7,15}$, например "996770730000").
 */
export function normalizeMsisdn(input) {
  return String(input || '').replace(/\D/g, '');
}

/**
 * Собирает нормализованный MSISDN (только цифры, без '+') из кода страны и цифр абонента.
 * Пример: ('KG', '770730000') -> '996770730000'
 */
export function buildMsisdn(countryKey, rawDigits) {
  const preset = COUNTRY_PRESETS[countryKey] || COUNTRY_PRESETS.KG;
  const digits = rawDigits.replace(/\D/g, '').slice(0, preset.maxDigits);
  return normalizeMsisdn(`${preset.prefixDigits}${digits}`);
}

/**
 * Форматирует нормализованный msisdn ("996770730000") в читаемый вид "+996 770 73-00-00".
 */
export function formatDisplayMsisdn(msisdn) {
  const clean = normalizeMsisdn(msisdn);
  if (clean.startsWith('996') && clean.length === 12) {
    const sub = clean.slice(3);
    return `+996 ${formatMaskedPhone('KG', sub)}`;
  }
  if (clean.startsWith('7') && clean.length === 11) {
    const sub = clean.slice(1);
    return `+7 ${formatMaskedPhone('RU', sub)}`;
  }
  return clean ? `+${clean}` : '';
}

/**
 * Проверяет соответствие номера контракту бэкенда: строго 7–15 цифр (^\d{7,15}$).
 */
export function isValidMsisdn(msisdn) {
  return /^\d{7,15}$/.test(msisdn);
}

/**
 * Форматирует секунды в MM:SS.
 */
export function formatTimer(totalSeconds) {
  const safe = Math.max(0, Math.floor(totalSeconds));
  const mins = Math.floor(safe / 60);
  const secs = safe % 60;
  return `${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
}

/**
 * Форматирует текущее время в HH:MM:SS.
 */
export function formatLogTime(date = new Date()) {
  return date.toTimeString().slice(0, 8);
}
