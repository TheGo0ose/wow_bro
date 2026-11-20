import { defineConfig, devices } from '@playwright/test';

/**
 * Конфигурация Playwright E2E-тестов для React OTP-клиента.
 *
 * Режимы запуска (из папки frontend/):
 * 1) С моками сети (по умолчанию):
 *    npx playwright test
 *
 * 2) Против реального боевого бэкенда:
 *    USE_REAL_API=true REAL_API_URL=https://api.your-otp-service.com npx playwright test
 *    (в PowerShell: $env:USE_REAL_API="true"; $env:REAL_API_URL="http://localhost:8000"; npx playwright test)
 */
export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: [['list']],
  use: {
    baseURL: process.env.E2E_BASE_URL || 'http://127.0.0.1:5173/wow_bro/',
    trace: 'on-first-retry',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  // Автоматически поднимаем Vite dev-сервер для фронтенда на время тестов
  webServer: {
    command: 'npx.cmd vite --port 5173 --host 127.0.0.1',
    url: 'http://127.0.0.1:5173/wow_bro/',
    reuseExistingServer: !process.env.CI,
    timeout: 30000,
  },
});
