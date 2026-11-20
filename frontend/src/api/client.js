import { normalizeMsisdn } from '../utils/phoneMask';

const STORAGE_KEY = 'external_api_base_url';
const DEFAULT_API_BASE_URL =
  import.meta.env.VITE_OTP_API_URL ||
  import.meta.env.VITE_API_BASE_URL ||
  'http://172.20.10.3:8000';

const REQUEST_TIMEOUT_MS = 5000;

export function getApiBaseUrl() {
  try {
    return localStorage.getItem(STORAGE_KEY) || DEFAULT_API_BASE_URL;
  } catch {
    return DEFAULT_API_BASE_URL;
  }
}

export function saveApiBaseUrl(url) {
  const normalized = url.trim().replace(/\/+$/, '');
  try {
    if (normalized) {
      localStorage.setItem(STORAGE_KEY, normalized);
    } else {
      localStorage.removeItem(STORAGE_KEY);
    }
  } catch {
    // Ignore storage errors
  }
  return normalized || DEFAULT_API_BASE_URL;
}

/**
 * Человекочитаемая расшифровка кодов detail от OTP Shield (ТЗ 6).
 */
export function resolveOtpErrorMessage({
  status,
  detail,
  attemptsRemaining,
  retryAfter,
  fallbackError,
}) {
  if (status === 401) {
    if (detail === 'OTP_INVALID_OR_USED') {
      return 'Код уже использован или не запрашивался. Запросите новый код';
    }
    if (detail === 'OTP_FAILED' || attemptsRemaining !== undefined) {
      const rem = attemptsRemaining !== undefined ? attemptsRemaining : '—';
      return `Неверный код. Осталось попыток: ${rem}`;
    }
    return fallbackError || 'Неверный код подтверждения.';
  }

  if (status === 410 || detail === 'OTP_EXPIRED') {
    return 'Срок действия кода истёк (2 минуты). Нажмите «Отправить повторно»';
  }

  if (status === 423 || detail === 'MSISDN_LOCKED') {
    const sec = retryAfter || 900;
    return `Номер заблокирован из-за 5 ошибок. Повторите через ${sec} сек`;
  }

  if (status === 429) {
    const sec = retryAfter || 30;
    if (detail === 'RATE_LIMITED_COOLDOWN') {
      return `Повторная отправка доступна через ${sec} сек`;
    }
    if (detail === 'RATE_LIMITED_SEND_10M') {
      return `Исчерпан лимит 3 SMS за 10 минут. Повтор возможен через ${sec} сек`;
    }
    if (detail === 'RATE_LIMITED_IP') {
      return `Слишком много попыток с вашего IP. Подождите ${sec} сек`;
    }
    return `Превышен лимит запросов. Подождите ${sec} сек`;
  }

  if (status === 422 || detail === 'VALIDATION_ERROR') {
    return 'Проверьте формат номера (7–15 цифр) или кода (6 цифр)';
  }

  return fallbackError || `Ошибка сервера (HTTP ${status})`;
}

async function requestJson(method, path, body, token) {
  const baseUrl = getApiBaseUrl();
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);

  const headers = {};
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
  }
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }

  let response;
  try {
    response = await fetch(`${baseUrl}${path}`, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    });
  } catch (err) {
    clearTimeout(timeoutId);
    const isTimeout = err?.name === 'AbortError';
    return {
      ok: false,
      status: 0,
      detail: isTimeout ? 'TIMEOUT' : 'NETWORK_ERROR',
      retryAfter: 0,
      attemptsRemaining: undefined,
      expiresIn: 120,
      data: null,
      error: isTimeout
        ? `Превышен таймаут ожидания (5 сек) от ${baseUrl}. Проверьте Wi-Fi сеть.`
        : `Не удалось подключиться к OTP Shield (${baseUrl}). Проверьте IP и соединение.`,
    };
  } finally {
    clearTimeout(timeoutId);
  }

  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }

  const retryHeader = Number(response.headers.get('Retry-After'));
  const retryBody = Number(data?.retry_after ?? data?.retryAfter ?? 0);
  const retryAfter =
    Number.isFinite(retryHeader) && retryHeader > 0
      ? retryHeader
      : Number.isFinite(retryBody) && retryBody > 0
        ? retryBody
        : 0;

  const detail =
    typeof data?.detail === 'string' ? data.detail : data?.code || null;
  const attemptsRemaining =
    typeof data?.attempts_remaining === 'number'
      ? data.attempts_remaining
      : undefined;
  const expiresIn =
    Number(data?.expires_in ?? data?.ttl_seconds ?? 120) || 120;

  const error = response.ok
    ? null
    : resolveOtpErrorMessage({
        status: response.status,
        detail,
        attemptsRemaining,
        retryAfter,
        fallbackError:
          typeof data?.detail === 'string' ? data.detail : data?.message,
      });

  return {
    ok: response.ok,
    status: response.status,
    detail,
    retryAfter,
    attemptsRemaining,
    expiresIn,
    data,
    error,
  };
}

/**
 * 1. POST /otp/send {"msisdn": "996770730000"}
 * Нормализует номер: оставляет строго только цифры (^\d{7,15}$).
 */
export function sendOtp(msisdn) {
  const cleanMsisdn = normalizeMsisdn(msisdn);
  return requestJson('POST', '/otp/send', { msisdn: cleanMsisdn });
}

/**
 * 2. POST /otp/verify {"msisdn": "996770730000", "code": "012345"}
 * Нормализует номер и 6-значный код перед отправкой.
 */
export function verifyOtp(msisdn, code) {
  const cleanMsisdn = normalizeMsisdn(msisdn);
  const cleanCode = String(code || '').replace(/\D/g, '').slice(0, 6);
  return requestJson('POST', '/otp/verify', {
    msisdn: cleanMsisdn,
    code: cleanCode,
  });
}

/**
 * GET /health — проверка доступности OTP Shield шлюза.
 */
export function checkOtpHealth() {
  return requestJson('GET', '/health');
}

/**
 * POST /_test/reset?flush=true — быстрый сброс лимитов/блокировок на демо.
 */
export function resetTestLimits() {
  return requestJson('POST', '/_test/reset?flush=true');
}

/**
 * Отправка сообщения через клиентскую панель.
 */
export function sendMessage({ recipient, text, channel, token }) {
  return requestJson(
    'POST',
    '/messages/send',
    {
      recipient,
      text,
      channel,
    },
    token
  );
}
