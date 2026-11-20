const STORAGE_KEY = 'external_api_base_url';
const DEFAULT_API_BASE_URL =
  import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000';

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
    // Ignore storage quota / privacy mode errors
  }
  return normalized || DEFAULT_API_BASE_URL;
}

async function postJson(path, body, token) {
  const baseUrl = getApiBaseUrl();
  const headers = {
    'Content-Type': 'application/json',
  };
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }

  let response;
  try {
    response = await fetch(`${baseUrl}${path}`, {
      method: 'POST',
      headers,
      body: JSON.stringify(body),
    });
  } catch {
    return {
      ok: false,
      status: 0,
      retryAfter: 0,
      data: null,
      error: `Не удалось подключиться к внешнему серверу (${baseUrl}). Проверьте URL и CORS.`,
    };
  }

  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }

  const retryHeader = Number(response.headers.get('Retry-After'));
  const retryBody = Number(data?.retry_after ?? data?.retryAfter ?? 0);
  const retryAfter = Number.isFinite(retryHeader) && retryHeader > 0
    ? retryHeader
    : Number.isFinite(retryBody) && retryBody > 0
      ? retryBody
      : 0;

  return {
    ok: response.ok,
    status: response.status,
    retryAfter,
    data,
    error: response.ok
      ? null
      : data?.detail || data?.message || data?.error || `HTTP ${response.status}`,
  };
}

export function requestOtpCode({ msisdn, mode, fullName }) {
  const payload = { msisdn, mode };
  if (mode === 'register' && fullName) {
    payload.name = fullName;
  }
  return postJson('/otp/send', payload);
}

export function verifyOtpCode({ msisdn, code }) {
  return postJson('/otp/verify', { msisdn, code });
}

export function sendMessage({ recipient, text, channel, token }) {
  return postJson(
    '/messages/send',
    {
      recipient,
      text,
      channel,
    },
    token
  );
}
