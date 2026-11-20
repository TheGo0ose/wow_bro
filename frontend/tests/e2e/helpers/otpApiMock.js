/**
 * Переключатель режима работы тестов:
 * - false (по умолчанию): запросы перехватываются через page.route() и эмулируются локально.
 * - true: page.route() НЕ применяется — запросы уходят напрямую на OTP Shield (REAL_API_URL).
 */
export const USE_REAL_API = process.env.USE_REAL_API === 'true';

/**
 * URL реального боевого OTP Shield.
 * Укажите его через переменную окружения REAL_API_URL (по умолчанию: http://172.20.10.3:8000).
 */
export const REAL_API_URL =
  process.env.REAL_API_URL || 'http://172.20.10.3:8000';

/**
 * Хелпер настройки сетевого слоя для E2E-тестов OTP Shield (ТЗ 6).
 * Контракт бэкенда:
 *   POST /otp/send   {"msisdn": "996770730000"} -> 200 | 422 | 423 | 429
 *   POST /otp/verify {"msisdn": "996770730000", "code": "012345"} -> 200 | 401 | 410 | 422 | 423 | 429
 *   GET  /health -> 200
 */
export async function setupOtpApiMock(page, options = {}) {
  const {
    sendScenario = '200',
    sendDetail = 'RATE_LIMITED_COOLDOWN',
    verifyScenario = 'auto',
    verifyDetail = 'OTP_FAILED',
    validCode = '012345',
    attemptsRemaining = 4,
    retryAfter = 45,
    expiresIn = 120,
  } = options;

  const capturedRequests = [];

  // Если включён флаг реального API — НЕ перехватываем запросы, прописываем REAL_API_URL
  if (USE_REAL_API) {
    await page.addInitScript((apiUrl) => {
      window.localStorage.setItem('external_api_base_url', apiUrl);
    }, REAL_API_URL);

    return { capturedRequests };
  }

  // Перехват GET /health (бейдж "Статус защиты OTP")
  await page.route('**/health', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ status: 'ok' }),
    });
  });

  // Перехват POST /otp/send
  await page.route('**/otp/send', async (route) => {
    const request = route.request();
    if (request.method() !== 'POST') {
      await route.continue();
      return;
    }

    const body = request.postDataJSON();
    capturedRequests.push({ endpoint: '/otp/send', body });

    if (sendScenario === '429') {
      await route.fulfill({
        status: 429,
        contentType: 'application/json',
        headers: {
          'Retry-After': String(retryAfter),
        },
        body: JSON.stringify({
          detail: sendDetail,
          retry_after: retryAfter,
        }),
      });
      return;
    }

    if (sendScenario === '423') {
      await route.fulfill({
        status: 423,
        contentType: 'application/json',
        headers: {
          'Retry-After': '900',
        },
        body: JSON.stringify({
          detail: 'MSISDN_LOCKED',
          retry_after: 900,
        }),
      });
      return;
    }

    if (sendScenario === '422') {
      await route.fulfill({
        status: 422,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'VALIDATION_ERROR' }),
      });
      return;
    }

    // 200 OK — OTP отправлен, возвращаем expires_in=120
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        status: 'sent',
        msisdn: body?.msisdn,
        expires_in: expiresIn,
      }),
    });
  });

  // Перехват POST /otp/verify
  await page.route('**/otp/verify', async (route) => {
    const request = route.request();
    if (request.method() !== 'POST') {
      await route.continue();
      return;
    }

    const body = request.postDataJSON();
    capturedRequests.push({ endpoint: '/otp/verify', body });

    const resolvedStatus =
      verifyScenario === 'auto'
        ? body?.code === validCode
          ? '200'
          : '401'
        : verifyScenario;

    if (resolvedStatus === '401') {
      await route.fulfill({
        status: 401,
        contentType: 'application/json',
        body: JSON.stringify({
          detail: verifyDetail,
          ...(verifyDetail === 'OTP_FAILED'
            ? { attempts_remaining: attemptsRemaining }
            : {}),
        }),
      });
      return;
    }

    if (resolvedStatus === '410') {
      await route.fulfill({
        status: 410,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'OTP_EXPIRED' }),
      });
      return;
    }

    if (resolvedStatus === '423') {
      await route.fulfill({
        status: 423,
        contentType: 'application/json',
        headers: { 'Retry-After': '900' },
        body: JSON.stringify({
          detail: 'MSISDN_LOCKED',
          retry_after: 900,
        }),
      });
      return;
    }

    if (resolvedStatus === '429') {
      await route.fulfill({
        status: 429,
        contentType: 'application/json',
        headers: { 'Retry-After': String(retryAfter) },
        body: JSON.stringify({
          detail: 'RATE_LIMITED_IP',
          retry_after: retryAfter,
        }),
      });
      return;
    }

    // 200 OK — успешная верификация
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        verified: true,
        msisdn: body?.msisdn,
      }),
    });
  });

  return { capturedRequests };
}
