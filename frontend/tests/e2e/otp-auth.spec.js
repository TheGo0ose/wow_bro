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

/**
 * Заполняет обязательные поля формы регистрации (логин, телефон, пароль + повтор пароля).
 */
async function fillRegistrationForm(
  page,
  { username = 'AibekDev', phone = '770730000', password = 'secret123' } = {}
) {
  await page.getByPlaceholder('Например, Айбек').fill(username);
  await page.getByPlaceholder('770 73-00-00').fill(phone);
  await page.getByPlaceholder('Мин. 6 символов').fill(password);
  await page.getByPlaceholder('Повторите пароль').fill(password);
}

test.describe('Реальная регистрация и 2FA-вход через OTP Shield (E2E)', () => {
  test('Happy Path: обязательный пароль, сохранение логина на шаге OTP и после перезагрузки + вход по паролю', async ({
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
    await expect(
      page.getByRole('button', { name: 'OTP Shield: ONLINE' })
    ).toBeVisible();

    // 1. Без пароля кнопка регистрации должна быть заблокирована
    await page.getByPlaceholder('Например, Айбек').fill('AibekDev');
    await page.getByPlaceholder('770 73-00-00').fill('770730000');
    const requestCodeButton = page.getByRole('button', {
      name: 'Получить код по SMS',
    });
    await expect(requestCodeButton).toBeDisabled();

    // 2. Вводим обязательный пароль и его подтверждение — кнопка активируется
    await page.getByPlaceholder('Мин. 6 символов').fill('secret123');
    await page.getByPlaceholder('Повторите пароль').fill('secret123');
    await expect(requestCodeButton).toBeEnabled();
    await requestCodeButton.click();

    // 3. На экране подтверждения OTP логин AibekDev должен отображаться и не сбрасываться
    await expect(
      page.getByRole('heading', { name: 'Подтверждение номера' })
    ).toBeVisible();
    await expect(page.getByText('AibekDev', { exact: true }).first()).toBeVisible();
    await expect(page.getByText('+996 770 73-00-00')).toBeVisible();
    await expect(
      page.getByRole('button', { name: /Отправить повторно \(00:[23]\d\)/ })
    ).toBeDisabled();

    // 4. Вводим 6-значный код (6-я цифра автоматически вызывает POST /otp/verify)
    await fillSixDigitCode(page, validCode);

    // 5. Проверяем Экран 3: аккаунт подтверждён, имя точно AibekDev (не «Пользователь»)
    await expect(
      page.getByRole('heading', { name: 'Аккаунт подтверждён' })
    ).toBeVisible();
    await expect(page.getByText('Добро пожаловать, AibekDev!')).toBeVisible();
    await expect(page.getByText('996770730000', { exact: true })).toBeVisible();
    await expect(
      page.getByRole('heading', { name: 'Зарегистрированные пользователи (1)' })
    ).toBeVisible();

    // 6. Проверяем сохранение сессии после перезагрузки страницы
    await page.reload();
    await expect(
      page.getByRole('heading', { name: 'Аккаунт подтверждён' })
    ).toBeVisible();
    await expect(page.getByText('Добро пожаловать, AibekDev!')).toBeVisible();

    // 7. Проверяем реальный выход и повторный вход по логину + паролю + OTP
    await page
      .getByRole('button', { name: 'Выйти / Зарегистрировать новый номер' })
      .click();
    await page.getByRole('button', { name: 'Вход' }).click();
    await page.getByPlaceholder('Айбек или 996770730000').fill('AibekDev');
    await page.getByPlaceholder('Введите пароль').fill('wrongpass');
    await page.getByRole('button', { name: 'Войти и получить SMS-код' }).click();
    await expect(
      page.getByText('Неверный пароль для указанного аккаунта.')
    ).toBeVisible();

    // Вводим правильный пароль -> получаем OTP -> подтверждаем
    await page.getByPlaceholder('Введите пароль').fill('secret123');
    await page.getByRole('button', { name: 'Войти и получить SMS-код' }).click();
    await expect(
      page.getByRole('heading', { name: 'Подтверждение номера' })
    ).toBeVisible();
    await fillSixDigitCode(page, validCode);
    await expect(page.getByText('Добро пожаловать, AibekDev!')).toBeVisible();

    if (!USE_REAL_API) {
      expect(capturedRequests.slice(0, 2)).toEqual([
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
    await expect(
      page.getByText(
        /Номер заблокирован из-за 5 ошибок\. Повторите через (900|899|898) сек/
      )
    ).toBeVisible();
    await expect(page.getByRole('textbox', { name: 'Цифра 1' })).toBeDisabled();
  });
});
