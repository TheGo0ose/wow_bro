import { expect, test } from '@playwright/test';
import {
  REAL_API_URL,
  USE_REAL_API,
  setupOtpApiMock,
} from './helpers/otpApiMock.js';

/**
 * Хелпер посимвольного ввода 6-значного кода в отдельные поля ввода ("Цифра 1" .. "Цифра 6").
 */
async function fillSixDigitCode(page, code) {
  for (let i = 0; i < code.length; i += 1) {
    await page.getByRole('textbox', { name: `Цифра ${i + 1}` }).fill(code[i]);
  }
}

test.describe('Регистрация через OTP Shield (E2E)', () => {
  test('Happy Path: ввод имени и номера +996, получение кода, автоотправка 6 цифр и сохранение сессии в localStorage (200)', async ({
    page,
  }) => {
    const validCode = process.env.REAL_OTP_CODE || '012345';

    const { capturedRequests } = await setupOtpApiMock(page, {
      sendScenario: '200',
      verifyScenario: 'auto',
      validCode,
      expiresIn: 120,
    });

    await page.goto('./');

    // Проверяем бейдж статуса OTP Shield (GET /health)
    await expect(page.getByRole('button', { name: 'OTP Shield: ONLINE' })).toBeVisible();

    // 1. Вводим имя и номер телефона по маске +996 XXX XX-XX-XX
    await page.getByPlaceholder('Например, Айбек').fill('Айбек');
    const phoneInput = page.getByPlaceholder('770 73-00-00');
    await phoneInput.fill('770730000');
    await expect(phoneInput).toHaveValue('770 73-00-00');

    // 2. Нажимаем «Получить код по SMS»
    const requestCodeButton = page.getByRole('button', {
      name: 'Получить код по SMS',
    });
    await expect(requestCodeButton).toBeEnabled();
    await requestCodeButton.click();

    // 3. Проверяем Экран 2: подсказка номера, 6 ячеек и два независимых таймера (TTL 120с + повтор 30с)
    await expect(
      page.getByRole('heading', { name: 'Подтверждение номера' })
    ).toBeVisible();
    await expect(page.getByText('+996 770 73-00-00')).toBeVisible();
    await expect(page.getByText('Код действует ещё:')).toBeVisible();
    await expect(
      page.getByRole('button', { name: /Отправить повторно \(00:[23]\d\)/ })
    ).toBeDisabled();

    // 4. Вводим правильный 6-значный код (6-я цифра автоматически отправляет POST /otp/verify)
    await fillSixDigitCode(page, validCode);

    // 5. Проверяем Экран 3: аккаунт подтверждён
    await expect(
      page.getByRole('heading', { name: 'Аккаунт подтверждён' })
    ).toBeVisible();
    await expect(page.getByText('996770730000', { exact: true })).toBeVisible();

    // Проверяем, что сессия сохранилась в localStorage и переживает перезагрузку страницы
    await page.reload();
    await expect(
      page.getByRole('heading', { name: 'Аккаунт подтверждён' })
    ).toBeVisible();

    // В режиме моков проверяем строгий формат контракта (msisdn без "+", только цифры)
    if (!USE_REAL_API) {
      expect(capturedRequests).toEqual([
        {
          endpoint: '/otp/send',
          body: { msisdn: '996770730000' },
        },
        {
          endpoint: '/otp/verify',
          body: { msisdn: '996770730000', code: validCode },
        },
      ]);
    } else {
      test.info().annotations.push({
        type: 'real-api',
        description: `Запросы отправлены на реальный OTP Shield: ${REAL_API_URL}`,
      });
    }
  });

  test('Негативный сценарий: Rate Limit (429 RATE_LIMITED_SEND_10M) показывает баннер задержки и блокирует отправку', async ({
    page,
  }) => {
    test.skip(
      USE_REAL_API,
      'Симуляция 429 выполняется только с моками (USE_REAL_API=false)'
    );

    await setupOtpApiMock(page, {
      sendScenario: '429',
      sendDetail: 'RATE_LIMITED_SEND_10M',
      retryAfter: 45,
    });

    await page.goto('./');

    await page.getByPlaceholder('Например, Айбек').fill('Айбек');
    await page.getByPlaceholder('770 73-00-00').fill('770730000');
    await page.getByRole('button', { name: 'Получить код по SMS' }).click();

    await expect(page.getByText('Прогрессивная задержка')).toBeVisible();
    await expect(
      page.getByText(/Исчерпан лимит 3 SMS за 10 минут\. Повтор возможен через 4[3-5] сек/)
    ).toBeVisible();
    await expect(page.getByText(/^00:4[3-5]$/)).toBeVisible();

    const throttledButton = page.getByRole('button', { name: /Подождите/ });
    await expect(throttledButton).toBeDisabled();
  });

  test('Негативный сценарий: 401 OTP_FAILED показывает attempts_remaining, а 423 MSISDN_LOCKED блокирует ввод', async ({
    page,
  }) => {
    test.skip(
      USE_REAL_API,
      'Проверка фиксированных ошибок 401 и 423 выполняется только с моками'
    );

    await setupOtpApiMock(page, {
      sendScenario: '200',
      verifyScenario: 'auto',
      verifyDetail: 'OTP_FAILED',
      attemptsRemaining: 4,
      validCode: '012345',
    });

    await page.goto('./');

    await page.getByPlaceholder('Например, Айбек').fill('Айбек');
    await page.getByPlaceholder('770 73-00-00').fill('770730000');
    await page.getByRole('button', { name: 'Получить код по SMS' }).click();

    // Отправляем неверный код -> ожидаем 401 OTP_FAILED с attempts_remaining: 4
    await fillSixDigitCode(page, '999999');
    await expect(
      page.getByText('Неверный код. Осталось попыток: 4')
    ).toBeVisible();

    // Переключаем мок /otp/verify на 423 MSISDN_LOCKED
    await page.unroute('**/otp/verify');
    await page.route('**/otp/verify', async (route) => {
      await route.fulfill({
        status: 423,
        contentType: 'application/json',
        headers: { 'Retry-After': '900' },
        body: JSON.stringify({
          detail: 'MSISDN_LOCKED',
          retry_after: 900,
        }),
      });
    });

    // Нажимаем «Подтвердить» -> получаем 423 и блокировку ячеек ввода
    await page.getByRole('button', { name: 'Подтвердить' }).click();
    await expect(page.getByText('Номер заблокирован (423)')).toBeVisible();
    await expect(
      page.getByText(/Номер заблокирован из-за 5 ошибок\. Повторите через (900|899|898) сек/)
    ).toBeVisible();
    await expect(page.getByRole('textbox', { name: 'Цифра 1' })).toBeDisabled();
  });
});
