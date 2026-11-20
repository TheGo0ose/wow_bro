export const COUNTRY_PRESETS = {
  RU: {
    code: '+7',
    label: 'RU / KZ',
    maxDigits: 10,
    placeholder: '(999) 123-45-67',
    demoDigits: '9991234567',
  },
  KG: {
    code: '+996',
    label: 'KG',
    maxDigits: 9,
    placeholder: '(555) 123-456',
    demoDigits: '555123456',
  },
};

/**
 * Formats raw subscriber digits according to the selected country mask.
 */
export function formatMaskedPhone(countryKey, rawDigits) {
  const digits = rawDigits.replace(/\D/g, '').slice(0, COUNTRY_PRESETS[countryKey].maxDigits);
  if (!digits) return '';

  if (countryKey === 'KG') {
    const p1 = digits.slice(0, 3);
    const p2 = digits.slice(3, 6);
    const p3 = digits.slice(6, 9);
    let out = `(${p1}`;
    if (p1.length === 3 && digits.length > 3) out += `) ${p2}`;
    else if (p1.length === 3) out += ')';
    if (p3) out += `-${p3}`;
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
 * Builds canonical E.164 MSISDN string for API logs and requests.
 */
export function buildMsisdn(countryKey, rawDigits) {
  const preset = COUNTRY_PRESETS[countryKey];
  const digits = rawDigits.replace(/\D/g, '').slice(0, preset.maxDigits);
  if (!digits) {
    return `${preset.code}${preset.demoDigits}`;
  }
  return `${preset.code}${digits}`;
}

/**
 * Formats seconds into MM:SS string.
 */
export function formatTimer(totalSeconds) {
  const safe = Math.max(0, Math.floor(totalSeconds));
  const mins = Math.floor(safe / 60);
  const secs = safe % 60;
  return `${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
}

/**
 * Formats current time as HH:MM:SS for terminal log timestamps.
 */
export function formatLogTime(date = new Date()) {
  return date.toTimeString().slice(0, 8);
}
