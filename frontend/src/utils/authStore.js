const ACCOUNTS_KEY = 'otp_shield_accounts_v1';
const SESSION_KEY = 'otp_shield_active_session_v1';
const PENDING_REG_KEY = 'otp_shield_pending_reg_v1';

/**
 * Хэширует пароль (SHA-256 через Web Crypto на HTTPS/localhost,
 * и детерминированный fallback на обычном HTTP по локальной сети хакатона).
 */
export async function hashPassword(password) {
  const value = String(password || '');
  if (typeof window !== 'undefined' && window.crypto?.subtle) {
    try {
      const encoded = new TextEncoder().encode(`otp-shield:${value}`);
      const digest = await window.crypto.subtle.digest('SHA-256', encoded);
      return Array.from(new Uint8Array(digest))
        .map((b) => b.toString(16).padStart(2, '0'))
        .join('');
    } catch {
      // Fall through to pure-JS hash on non-secure HTTP origins
    }
  }

  let h1 = 0xdeadbeef ^ value.length;
  let h2 = 0x41c6ce57 ^ value.length;
  for (let i = 0; i < value.length; i += 1) {
    const ch = value.charCodeAt(i);
    h1 = Math.imul(h1 ^ ch, 2654435761);
    h2 = Math.imul(h2 ^ ch, 1597334677);
  }
  h1 =
    Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^
    Math.imul(h2 ^ (h2 >>> 13), 3266489909);
  h2 =
    Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^
    Math.imul(h1 ^ (h1 >>> 13), 3266489909);
  return (
    (h2 >>> 0).toString(16).padStart(8, '0') +
    (h1 >>> 0).toString(16).padStart(8, '0')
  );
}

export function loadAccounts() {
  try {
    const raw = localStorage.getItem(ACCOUNTS_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

export function saveAccounts(accounts) {
  try {
    localStorage.setItem(ACCOUNTS_KEY, JSON.stringify(accounts));
  } catch {
    // Ignore storage quota errors
  }
}

export function upsertAccount(account) {
  const existing = loadAccounts();
  const filtered = existing.filter(
    (item) =>
      item.msisdn !== account.msisdn &&
      item.username.toLowerCase() !== account.username.toLowerCase()
  );
  const next = [account, ...filtered];
  saveAccounts(next);
  return next;
}

export function removeAccountById(id) {
  const next = loadAccounts().filter((item) => item.id !== id);
  saveAccounts(next);
  return next;
}

export function findAccountConflict({ username, msisdn }) {
  const accounts = loadAccounts();
  const cleanName = username.trim().toLowerCase();
  const byName = accounts.find((a) => a.username.toLowerCase() === cleanName);
  if (byName) {
    return `Логин «${username.trim()}» уже зарегистрирован. Выберите другой логин или войдите.`;
  }
  const byPhone = accounts.find((a) => a.msisdn === msisdn);
  if (byPhone) {
    return `Номер ${msisdn} уже привязан к аккаунту «${byPhone.username}». Перейдите во вкладку «Вход».`;
  }
  return null;
}

export function findAccountForLogin(identifier) {
  const trimmed = identifier.trim();
  if (!trimmed) return null;
  const lower = trimmed.toLowerCase();
  const digits = trimmed.replace(/\D/g, '');
  const accounts = loadAccounts();
  return (
    accounts.find(
      (a) =>
        a.username.toLowerCase() === lower ||
        (digits.length >= 7 && a.msisdn === digits) ||
        (a.email && a.email.toLowerCase() === lower)
    ) || null
  );
}

export function loadActiveSession() {
  try {
    const raw = localStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return parsed && parsed.username && parsed.msisdn ? parsed : null;
  } catch {
    return null;
  }
}

export function saveActiveSession(user) {
  try {
    localStorage.setItem(SESSION_KEY, JSON.stringify(user));
  } catch {
    // Ignore
  }
}

export function clearActiveSession() {
  try {
    localStorage.removeItem(SESSION_KEY);
  } catch {
    // Ignore
  }
}

export function loadPendingRegistration() {
  try {
    const raw = sessionStorage.getItem(PENDING_REG_KEY);
    if (!raw) return null;
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

export function savePendingRegistration(draft) {
  try {
    if (draft) {
      sessionStorage.setItem(PENDING_REG_KEY, JSON.stringify(draft));
    } else {
      sessionStorage.removeItem(PENDING_REG_KEY);
    }
  } catch {
    // Ignore
  }
}
