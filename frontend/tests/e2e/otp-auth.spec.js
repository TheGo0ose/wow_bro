import { expect, test } from '@playwright/test';
import {
  REAL_API_URL,
  USE_REAL_API,
  setupOtpApiMock,
} from './helpers/otpApiMock.js';

async function fillSixDigitCode(page, code) {
  for (let i = 0; i < code.length; i += 1) {
    await page.getByRole('textbox', { name: `Цифра ${i + 1}` }).fill(code[i]);
  }
}

async function fillRegistrationForm(
  page,
  { username = 'AibekDev', phone = '770730000', password = 'secret123' } = {}
) {
  await page.getByPlaceholder('Например, Айбек').fill(username);
  await page.getByPlaceholder('770 73-00-00').fill(phone);
  await page.getByPlaceholder('Мин. 6 символов').fill(password);
  await page.getByPlaceholder('Повторите пароль').fill(password);
}

test.describe('Регистрация с OTP Shield и страницей поздравления (E2E)', () => {
  test('Happy Path: регистрация с обязательным паролем -> OTP -> страница поздравления с конфетти', async ({
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

    // Проверяем статус OTP Shield (GET /health)
    await expect(
      page.getByRole('button', { name: 'OTP Shield: ONLINE' })
    ).toBeVisible();

    // Без пароля кнопка заблокирована
    await page.getByPlaceholder('Например, Айбек').fill('AibekDev');
    await page.getByPlaceholder('770 73-00-00').fill('770730000');
    const requestCodeButton = page.getByRole('button', {
      name: 'Получить код по SMS',
    });
    await expect(requestCodeButton).toBeDisabled();

    // Вводим пароль и подтверждение
    await page.getByPlaceholder('Мин. 6 символов').fill('secret123');
    await page.getByPlaceholder('Повторите пароль').fill('secret123');
    await expect(requestCodeButton).toBeEnabled();
    await requestCodeButton.click();

    // На шаге подтверждения OTP имя сохраняется
    await expect(
      page.getByRole('heading', { name: 'Подтверждение номера' })
    ).toBeVisible();
    await expect(page.getByText('AibekDev', { exact: true }).first()).toBeVisible();
    await expect(page.getByText('+996 770 73-00-00')).toBeVisible();

    // Вводим 6-значный код (автоотправка на 6-й цифре)
    await fillSixDigitCode(page, validCode);

    // Проверяем страницу с поздравлением и canvas конфетти
    await expect(
      page.getByRole('heading', { name: 'Поздравляем, AibekDev!' })
    ).toBeVisible();
    await expect(page.getByTestId('confetti-canvas')).toBeVisible();
    await expect(
      page.getByRole('button', { name: 'Ещё конфетти!' })
    ).toBeVisible();

    // Сессия сохраняется после перезагрузки
    await page.reload();
    await expect(
      page.getByRole('heading', { name: 'Поздравляем, AibekDev!' })
    ).toBeVisible();

    // Кнопка "Новая регистрация" возвращает на форму
    await page.getByRole('button', { name: 'Новая регистрация' }).click();
    await expect(
      page.getByRole('heading', { name: 'Регистрация' })
    ).toBeVisible();

    if (!USE_REAL_API) {
      expect(capturedRequests).toEqual([
        { endpoint: '/otp/send', body: { msisdn: '996770730000' } },
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
    await fillRegistrationForm(page);
    await page.getByRole('button', { name: 'Получить код по SMS' }).click();

    await expect(page.getByText('Прогрессивная задержка')).toBeVisible();
    await expect(
      page.getByText(
        /Исчерпан лимит 3 SMS за 10 минут\. Повтор возможен через 4[3-5] сек/
      )
    ).toBeVisible();
    await expect(page.getByRole('button', { name: /Подождите/ })).toBeDisabled();
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
    await fillRegistrationForm(page);
    await page.getByRole('button', { name: 'Получить код по SMS' }).click();

    await fillSixDigitCode(page, '999999');
    await expect(
      page.getByText('Неверный код. Осталось попыток: 4')
    ).toBeVisible();

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

    await page.getByRole('button', { name: 'Подтвердить' }).click();
    await expect(page.getByText('Номер заблокирован (423)')).toBeVisible();
    await expect(page.getByRole('textbox', { name: 'Цифра 1' })).toBeDisabled();
  });
});
