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

test.describe('OTP-авторизация (E2E)', () => {
  test('Happy Path: ввод телефона, получение кода, ввод 6 цифр и успешная верификация (200)', async ({
    page,
  }) => {
    const validCode = process.env.REAL_OTP_CODE || '482910';

    // При USE_REAL_API=false перехватываем POST /otp/send и POST /otp/verify и отдаём 200 OK.
    // При USE_REAL_API=true мок отключается, и запросы идут на REAL_API_URL.
    const { capturedRequests } = await setupOtpApiMock(page, {
      sendScenario: '200',
      verifyScenario: 'auto',
      validCode,
    });

    await page.goto('/');

    // 1. Вводим номер телефона с помощью современного локатора по placeholder
    const phoneInput = page.getByPlaceholder('(999) 123-45-67');
    await phoneInput.fill('9991234567');
    await expect(phoneInput).toHaveValue('(999) 123-45-67');

    // 2. Нажимаем кнопку "Получить код"
    const requestCodeButton = page.getByRole('button', { name: 'Получить код' });
    await expect(requestCodeButton).toBeEnabled();
    await requestCodeButton.click();

    // 3. Проверяем, что появилась форма для ввода 6-значного кода и таймер на 120 секунд
    await expect(
      page.getByRole('heading', { name: 'Подтверждение номера' })
    ).toBeVisible();
    await expect(page.getByText('Время действия кода:')).toBeVisible();
    await expect(page.getByRole('textbox', { name: 'Цифра 1' })).toBeVisible();
    await expect(page.getByRole('textbox', { name: 'Цифра 6' })).toBeVisible();

    // 4. Вводим правильный 6-значный код и нажимаем "Подтвердить"
    await fillSixDigitCode(page, validCode);
    const confirmButton = page.getByRole('button', { name: 'Подтвердить' });
    await expect(confirmButton).toBeEnabled();
    await confirmButton.click();

    // 5. Проверяем экран успешной верификации
    await expect(
      page.getByRole('heading', { name: 'Доступ разрешен' })
    ).toBeVisible();
    await expect(page.getByText('+79991234567', { exact: true })).toBeVisible();
    await expect(page.getByText('Авторизован: +79991234567')).toBeVisible();

    // В режиме моков дополнительно убеждаемся, что фронтенд отправил корректные payload'ы
    if (!USE_REAL_API) {
      expect(capturedRequests).toEqual([
        {
          endpoint: '/otp/send',
          body: { msisdn: '+79991234567', mode: 'register' },
        },
        {
          endpoint: '/otp/verify',
          body: { msisdn: '+79991234567', code: validCode },
        },
      ]);
    } else {
      test.info().annotations.push({
        type: 'real-api',
        description: `Запросы отправлены на реальный бэкенд: ${REAL_API_URL}`,
      });
    }
  });

  test('Негативный сценарий: Rate Limit (429) показывает уведомление "Прогрессивная задержка" и блокирует повторную отправку', async ({
    page,
  }) => {
    // Пропускаем тест при USE_REAL_API=true, чтобы не спамить реальный боевой сервер
    test.skip(
      USE_REAL_API,
      'Симуляция 429 выполняется только с моками (USE_REAL_API=false)'
    );

    // Настраиваем заглушку так, чтобы POST /otp/send сразу вернул 429 с задержкой 45 секунд
    await setupOtpApiMock(page, {
      sendScenario: '429',
      retryAfter: 45,
    });

    await page.goto('/');

    // Вводим корректный номер телефона и нажимаем "Получить код"
    await page.getByPlaceholder('(999) 123-45-67').fill('9991234567');
    await page.getByRole('button', { name: 'Получить код' }).click();

    // Проверяем, что UI отобразил жёлтое уведомление "Прогрессивная задержка" и таймер ~00:45
    await expect(page.getByText('Прогрессивная задержка')).toBeVisible();
    await expect(page.getByText(/^00:4[3-5]$/)).toBeVisible();

    // Кнопка отправки кода должна быть заблокирована на время задержки
    const throttledButton = page.getByRole('button', { name: /Подождите/ });
    await expect(throttledButton).toBeDisabled();
  });

  test('Негативный сценарий: неверный код (401) показывает ошибку, затем верный код (200) пропускает', async ({
    page,
  }) => {
    test.skip(
      USE_REAL_API,
      'Проверка фиксированной ошибки 401 выполняется только с моками'
    );

    // В режиме verifyScenario='auto' код '000000' даст 401, а '482910' даст 200
    await setupOtpApiMock(page, {
      sendScenario: '200',
      verifyScenario: 'auto',
      validCode: '482910',
    });

    await page.goto('/');

    await page.getByPlaceholder('(999) 123-45-67').fill('9991234567');
    await page.getByRole('button', { name: 'Получить код' }).click();

    // Сначала отправляем неверный код -> ожидаем сообщение об ошибке (401)
    await fillSixDigitCode(page, '000000');
    await page.getByRole('button', { name: 'Подтвердить' }).click();
    await expect(page.getByText('Неверный код подтверждения.')).toBeVisible();

    // Затем вводим правильный код -> ожидаем успешный переход
    await fillSixDigitCode(page, '482910');
    await page.getByRole('button', { name: 'Подтвердить' }).click();
    await expect(
      page.getByRole('heading', { name: 'Доступ разрешен' })
    ).toBeVisible();
  });
});
