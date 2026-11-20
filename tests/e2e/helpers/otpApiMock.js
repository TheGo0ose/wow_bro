/**
 * Переключатель режима работы тестов:
 * - false (по умолчанию): запросы перехватываются через page.route() и эмулируются локально.
 * - true: page.route() НЕ применяется — все запросы уходят на реальный боевой бэкенд.
 */
export const USE_REAL_API = process.env.USE_REAL_API === 'true';

/**
 * URL реального боевого OTP-сервиса.
 * Укажите его через переменную окружения REAL_API_URL (или замените значение по умолчанию ниже).
 * Пример: REAL_API_URL=https://otp-prod.example.com
 */
export const REAL_API_URL = process.env.REAL_API_URL || 'http://localhost:8000';

/**
 * Хелпер настройки сетевого слоя для E2E-тестов OTP-сервиса.
 *
 * @param {import('@playwright/test').Page} page - Страница Playwright
 * @param {object} [options] - Конфигурация подставных ответов (используется только при USE_REAL_API=false)
 * @param {'200'|'429'|'423'} [options.sendScenario='200'] - Какой статус вернуть на POST /otp/send
 * @param {'auto'|'200'|'401'|'410'|'423'|'429'} [options.verifyScenario='auto'] - Какой статус вернуть на POST /otp/verify
 * @param {string} [options.validCode='482910'] - Код, при вводе которого в режиме 'auto' сервер вернёт 200 OK
 * @param {number} [options.retryAfter=45] - Значение Retry-After / retry_after для ответов 429 и 423 (в секундах)
 * @param {number} [options.ttlSeconds=120] - Время жизни OTP-кода для ответа 200 на /otp/send
 * @returns {Promise<{ capturedRequests: Array<{ endpoint: string, body: any }> }>}
 */
export async function setupOtpApiMock(page, options = {}) {
  const {
    sendScenario = '200',
    verifyScenario = 'auto',
    validCode = '482910',
    retryAfter = 45,
    ttlSeconds = 120,
  } = options;

  const capturedRequests = [];

  // Если включён флаг реального API:
  // 1) Прописываем адрес реального сервера в localStorage фронтенда (src/api/client.js читает его перед fetch).
  // 2) НЕ регистрируем page.route() — запросы пойдут напрямую на реальный бэкенд.
  if (USE_REAL_API) {
    await page.addInitScript((apiUrl) => {
      window.localStorage.setItem('external_api_base_url', apiUrl);
    }, REAL_API_URL);

    return { capturedRequests };
  }

  // Перехват запроса на отправку одноразового пароля: POST /otp/send
  await page.route('**/otp/send', async (route) => {
    const request = route.request();
    if (request.method() !== 'POST') {
      await route.continue();
      return;
    }

    const body = request.postDataJSON();
    capturedRequests.push({ endpoint: '/otp/send', body });

    // Подставляем ответ 429 Rate Limit ("Прогрессивная задержка")
    if (sendScenario === '429') {
      await route.fulfill({
        status: 429,
        contentType: 'application/json',
        headers: {
          'Retry-After': String(retryAfter),
        },
        body: JSON.stringify({
          detail: 'Превышен лимит запросов на отправку кода.',
          retry_after: retryAfter,
        }),
      });
      return;
    }

    // Подставляем ответ 423 Locked (временная блокировка номера)
    if (sendScenario === '423') {
      await route.fulfill({
        status: 423,
        contentType: 'application/json',
        body: JSON.stringify({
          detail: 'Номер временно заблокирован из-за подозрительной активности.',
          retry_after: 900,
        }),
      });
      return;
    }

    // Подставляем успешный ответ 200 OK — OTP отправлен, TTL 120 секунд
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        status: 'sent',
        msisdn: body?.msisdn,
        ttl_seconds: ttlSeconds,
      }),
    });
  });

  // Перехват запроса на проверку одноразового пароля: POST /otp/verify
  await page.route('**/otp/verify', async (route) => {
    const request = route.request();
    if (request.method() !== 'POST') {
      await route.continue();
      return;
    }

    const body = request.postDataJSON();
    capturedRequests.push({ endpoint: '/otp/verify', body });

    // Определяем итоговый сценарий ответа: явный или автоматический (по совпадению кода)
    const resolvedStatus =
      verifyScenario === 'auto'
        ? body?.code === validCode
          ? '200'
          : '401'
        : verifyScenario;

    // 401 Unauthorized — неверный код
    if (resolvedStatus === '401') {
      await route.fulfill({
        status: 401,
        contentType: 'application/json',
        body: JSON.stringify({
          detail: 'Неверный код подтверждения.',
        }),
      });
      return;
    }

    // 410 Gone — срок действия кода истёк
    if (resolvedStatus === '410') {
      await route.fulfill({
        status: 410,
        contentType: 'application/json',
        body: JSON.stringify({
          detail: 'Срок действия OTP-кода истёк.',
        }),
      });
      return;
    }

    // 429 Too Many Requests — превышен лимит попыток проверки
    if (resolvedStatus === '429') {
      await route.fulfill({
        status: 429,
        contentType: 'application/json',
        headers: {
          'Retry-After': String(retryAfter),
        },
        body: JSON.stringify({
          detail: 'Слишком много попыток ввода кода.',
          retry_after: retryAfter,
        }),
      });
      return;
    }

    // 423 Locked — блокировка номера
    if (resolvedStatus === '423') {
      await route.fulfill({
        status: 423,
        contentType: 'application/json',
        body: JSON.stringify({
          detail: 'Номер заблокирован на 15 минут.',
          retry_after: 900,
        }),
      });
      return;
    }

    // 200 OK — успешная верификация, выдаём токен сессии
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        verified: true,
        access_token: 'mock-jwt-token-otp-verified',
      }),
    });
  });

  return { capturedRequests };
}
