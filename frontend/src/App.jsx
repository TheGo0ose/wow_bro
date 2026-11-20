import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Activity,
  Check,
  Globe,
  RotateCcw,
  Settings2,
  Shield,
  ShieldCheck,
} from 'lucide-react';
import RegistrationPanel from './components/RegistrationPanel';
import BackendDemoPanel from './components/BackendDemoPanel';
import {
  checkOtpHealth,
  getApiBaseUrl,
  resetTestLimits,
  saveApiBaseUrl,
  sendOtp,
  verifyOtp,
} from './api/client';
import {
  clearActiveSession,
  findAccountConflict,
  findAccountForLogin,
  hashPassword,
  loadAccounts,
  loadActiveSession,
  loadPendingRegistration,
  removeAccountById,
  saveActiveSession,
  savePendingRegistration,
  upsertAccount,
} from './utils/authStore';
import { buildMsisdn, formatLogTime, isValidMsisdn } from './utils/phoneMask';

const DEFAULT_OTP_TTL = 120;
const PROGRESSIVE_COOLDOWNS = [30, 60, 120];
const DEFAULT_LOCKOUT_SECONDS = 15 * 60;

export default function App() {
  // Настройка URL OTP Shield (VITE_OTP_API_URL=http://172.20.10.3:8000)
  const [apiBaseUrl, setApiBaseUrl] = useState(() => getApiBaseUrl());
  const [isEditingApi, setIsEditingApi] = useState(false);
  const [apiDraft, setApiDraft] = useState(apiBaseUrl);

  // Статус GET /health
  const [healthState, setHealthState] = useState({
    status: 'idle',
    detail: 'Статус защиты OTP',
  });
  const [resetNotice, setResetNotice] = useState('');

  // Реальные аккаунты и активная сессия
  const [accounts, setAccounts] = useState(() => loadAccounts());
  const [activeUser, setActiveUser] = useState(() => loadActiveSession());

  // Режим формы: регистрация / вход
  const [authMode, setAuthMode] = useState('register'); // 'register' | 'login'
  const [step, setStep] = useState(() => (loadActiveSession() ? 'success' : 'form'));

  // Поля формы регистрации
  const [username, setUsername] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [countryKey, setCountryKey] = useState('KG');
  const [phoneDigits, setPhoneDigits] = useState('');

  // Поля формы входа
  const [loginIdentifier, setLoginIdentifier] = useState('');
  const [loginPassword, setLoginPassword] = useState('');

  // Снимок данных пользователя, ожидающего подтверждения OTP (сохраняется в ref + sessionStorage,
  // чтобы логин НИКОГДА не сбрасывался на этапе ввода кода подтверждения!)
  const [pendingAuth, setPendingAuth] = useState(() => loadPendingRegistration());
  const pendingAuthRef = useRef(pendingAuth);

  const updatePendingAuth = useCallback((nextDraft) => {
    pendingAuthRef.current = nextDraft;
    setPendingAuth(nextDraft);
    savePendingRegistration(nextDraft);
  }, []);

  // OTP состояние и таймеры
  const [otpDigits, setOtpDigits] = useState(['', '', '', '', '', '']);
  const [isLoading, setIsLoading] = useState(false);
  const [otpTtlTotal, setOtpTtlTotal] = useState(DEFAULT_OTP_TTL);
  const [otpCountdown, setOtpCountdown] = useState(DEFAULT_OTP_TTL);
  const [resendCooldown, setResendCooldown] = useState(0);
  const [sendAttemptIndex, setSendAttemptIndex] = useState(0);
  const [lockoutSeconds, setLockoutSeconds] = useState(0);

  // Ошибки и анимация shake
  const [errorStatus, setErrorStatus] = useState(null);
  const [errorDetail, setErrorDetail] = useState(null);
  const [errorMessage, setErrorMessage] = useState('');
  const [shakeKey, setShakeKey] = useState(0);

  // Живой журнал реальных HTTP-запросов к бэкенду
  const [httpLogs, setHttpLogs] = useState([]);

  const recordHttpLog = useCallback((res) => {
    const responsePreview = res.data
      ? JSON.stringify(res.data)
      : res.error || '';
    setHttpLogs((prev) => [
      {
        id: `${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
        time: formatLogTime(),
        method: res.method,
        path: res.path,
        status: res.status,
        latencyMs: res.latencyMs,
        requestBody: res.requestBody,
        responsePreview,
      },
      ...prev.slice(0, 39),
    ]);
  }, []);

  const handleCheckHealth = useCallback(async () => {
    setHealthState({ status: 'checking', detail: 'Проверка /health...' });
    const res = await checkOtpHealth();
    recordHttpLog(res);
    if (res.ok) {
      setHealthState({ status: 'online', detail: 'OTP Shield: ONLINE' });
    } else {
      setHealthState({ status: 'offline', detail: 'OTP Shield: OFFLINE' });
    }
  }, [recordHttpLog]);

  useEffect(() => {
    handleCheckHealth();
  }, [handleCheckHealth]);

  const handleTestReset = async () => {
    const res = await resetTestLimits();
    recordHttpLog(res);
    if (res.ok) {
      setLockoutSeconds(0);
      setResendCooldown(0);
      setSendAttemptIndex(0);
      setErrorStatus(null);
      setErrorDetail(null);
      setErrorMessage('');
      setResetNotice('Лимиты сброшены (/_test/reset)');
    } else {
      setResetNotice('Не удалось вызвать /_test/reset');
    }
    setTimeout(() => setResetNotice(''), 3000);
  };

  // Таймер 1: TTL жизни кода (120 сек)
  useEffect(() => {
    if (step !== 'otp' || otpCountdown <= 0 || lockoutSeconds > 0) return;
    const timer = setInterval(() => {
      setOtpCountdown((prev) => {
        if (prev <= 1) {
          setErrorStatus(410);
          setErrorDetail('OTP_EXPIRED');
          setErrorMessage(
            'Срок действия кода истёк (2 минуты). Нажмите «Отправить повторно»'
          );
          return 0;
        }
        return prev - 1;
      });
    }, 1000);
    return () => clearInterval(timer);
  }, [step, otpCountdown, lockoutSeconds]);

  // Таймер 2: Cooldown повторной отправки (30 → 60 → 120 сек)
  useEffect(() => {
    if (resendCooldown <= 0) return;
    const timer = setInterval(() => {
      setResendCooldown((prev) => {
        if (prev <= 1) {
          if (errorStatus === 429) {
            setErrorStatus(null);
            setErrorDetail(null);
            setErrorMessage('');
          }
          return 0;
        }
        return prev - 1;
      });
    }, 1000);
    return () => clearInterval(timer);
  }, [resendCooldown, errorStatus]);

  // Таймер 3: Блокировка номера 423 (MSISDN_LOCKED)
  useEffect(() => {
    if (lockoutSeconds <= 0) return;
    const timer = setInterval(() => {
      setLockoutSeconds((prev) => {
        if (prev <= 1) {
          setErrorStatus(null);
          setErrorDetail(null);
          setErrorMessage('');
          return 0;
        }
        return prev - 1;
      });
    }, 1000);
    return () => clearInterval(timer);
  }, [lockoutSeconds]);

  const handleSaveApiUrl = (e) => {
    e.preventDefault();
    const saved = saveApiBaseUrl(apiDraft);
    setApiBaseUrl(saved);
    setApiDraft(saved);
    setIsEditingApi(false);
    handleCheckHealth();
  };

  // Общая отправка POST /otp/send на OTP Shield
  const dispatchOtpSend = async (draft) => {
    setIsLoading(true);
    setErrorStatus(null);
    setErrorDetail(null);
    setErrorMessage('');

    const res = await sendOtp(draft.msisdn);
    recordHttpLog(res);
    setIsLoading(false);

    if (res.ok) {
      const ttl = res.expiresIn || DEFAULT_OTP_TTL;
      const fallbackCooldown =
        PROGRESSIVE_COOLDOWNS[
          Math.min(sendAttemptIndex, PROGRESSIVE_COOLDOWNS.length - 1)
        ];
      updatePendingAuth(draft);
      setOtpDigits(['', '', '', '', '', '']);
      setOtpTtlTotal(ttl);
      setOtpCountdown(ttl);
      setResendCooldown(res.retryAfter || fallbackCooldown);
      setSendAttemptIndex((idx) => idx + 1);
      setStep('otp');
      return;
    }

    if (res.status === 429) {
      setResendCooldown(res.retryAfter || 30);
      setErrorStatus(429);
      setErrorDetail(res.detail || 'RATE_LIMITED_COOLDOWN');
      setErrorMessage(res.error);
      return;
    }

    if (res.status === 423) {
      setLockoutSeconds(res.retryAfter || DEFAULT_LOCKOUT_SECONDS);
      setErrorStatus(423);
      setErrorDetail(res.detail || 'MSISDN_LOCKED');
      setErrorMessage(res.error);
      return;
    }

    setErrorStatus(res.status);
    setErrorDetail(res.detail);
    setErrorMessage(res.error);
  };

  // Шаг 1A: Реальная регистрация (обязательные логин, номер, пароль + проверка дубликатов)
  const handleSubmitRegister = async () => {
    if (isLoading || lockoutSeconds > 0 || resendCooldown > 0) return;

    const cleanUsername = username.trim();
    const msisdn = buildMsisdn(countryKey, phoneDigits);

    if (cleanUsername.length < 2) {
      setErrorStatus(422);
      setErrorDetail('VALIDATION_ERROR');
      setErrorMessage('Укажите логин или имя пользователя (минимум 2 символа).');
      return;
    }
    if (!isValidMsisdn(msisdn)) {
      setErrorStatus(422);
      setErrorDetail('VALIDATION_ERROR');
      setErrorMessage('Проверьте формат номера (7–15 цифр) или кода (6 цифр)');
      return;
    }
    if (password.length < 6) {
      setErrorStatus(422);
      setErrorDetail('VALIDATION_ERROR');
      setErrorMessage('Пароль обязателен (минимум 6 символов).');
      return;
    }
    if (password !== confirmPassword) {
      setErrorStatus(422);
      setErrorDetail('VALIDATION_ERROR');
      setErrorMessage('Пароли не совпадают.');
      return;
    }

    const conflict = findAccountConflict({ username: cleanUsername, msisdn });
    if (conflict) {
      setErrorStatus(409);
      setErrorDetail('ACCOUNT_EXISTS');
      setErrorMessage(conflict);
      return;
    }

    const passwordHash = await hashPassword(password);
    const draft = {
      flow: 'register',
      username: cleanUsername,
      email: email.trim() || null,
      msisdn,
      passwordHash,
    };

    await dispatchOtpSend(draft);
  };

  // Шаг 1B: Реальный вход (проверка логина/телефона + хэша пароля → 2FA через OTP Shield)
  const handleSubmitLogin = async () => {
    if (isLoading || lockoutSeconds > 0 || resendCooldown > 0) return;

    const account = findAccountForLogin(loginIdentifier);
    if (!account) {
      setErrorStatus(404);
      setErrorDetail('USER_NOT_FOUND');
      setErrorMessage(
        'Аккаунт не найден. Проверьте логин/телефон или сначала пройдите регистрацию.'
      );
      return;
    }

    const inputHash = await hashPassword(loginPassword);
    if (inputHash !== account.passwordHash) {
      setErrorStatus(401);
      setErrorDetail('INVALID_PASSWORD');
      setErrorMessage('Неверный пароль для указанного аккаунта.');
      return;
    }

    const draft = {
      flow: 'login',
      accountId: account.id,
      username: account.username,
      email: account.email || null,
      msisdn: account.msisdn,
      passwordHash: account.passwordHash,
      registeredAt: account.registeredAt,
    };

    await dispatchOtpSend(draft);
  };

  // Повторная отправка SMS на шаге 2
  const handleResendOtp = async () => {
    if (isLoading || lockoutSeconds > 0 || resendCooldown > 0) return;
    const draft = pendingAuthRef.current;
    if (!draft?.msisdn) return;
    await dispatchOtpSend(draft);
  };

  // Шаг 2: Подтверждение 6-значного кода через POST /otp/verify
  const handleVerifyCode = useCallback(
    async (explicitCode) => {
      if (isLoading || lockoutSeconds > 0 || otpCountdown <= 0) return;
      const draft = pendingAuthRef.current || loadPendingRegistration();
      const msisdn = draft?.msisdn || buildMsisdn(countryKey, phoneDigits);
      const code =
        typeof explicitCode === 'string' ? explicitCode : otpDigits.join('');
      if (code.length !== 6) return;

      setIsLoading(true);
      setErrorStatus(null);
      setErrorDetail(null);
      setErrorMessage('');

      const res = await verifyOtp(msisdn, code);
      recordHttpLog(res);
      setIsLoading(false);

      if (res.ok) {
        const resolvedUsername =
          (draft?.username || username || '').trim();
        const userRecord = {
          id: draft?.accountId || `usr_${Date.now()}`,
          username: resolvedUsername,
          email: draft?.email ?? (email.trim() || null),
          msisdn,
          passwordHash: draft?.passwordHash || '',
          registeredAt: draft?.registeredAt || new Date().toISOString(),
        };

        const updatedAccounts = upsertAccount(userRecord);
        setAccounts(updatedAccounts);
        saveActiveSession(userRecord);
        setActiveUser(userRecord);
        updatePendingAuth(null);
        setPassword('');
        setConfirmPassword('');
        setLoginPassword('');
        setStep('success');
        return;
      }

      if (res.status === 401) {
        setErrorStatus(401);
        setErrorDetail(res.detail || 'OTP_FAILED');
        setErrorMessage(res.error);
        setShakeKey((k) => k + 1);
        return;
      }

      if (res.status === 410) {
        setOtpCountdown(0);
        setErrorStatus(410);
        setErrorDetail(res.detail || 'OTP_EXPIRED');
        setErrorMessage(res.error);
        return;
      }

      if (res.status === 423) {
        setLockoutSeconds(res.retryAfter || DEFAULT_LOCKOUT_SECONDS);
        setErrorStatus(423);
        setErrorDetail(res.detail || 'MSISDN_LOCKED');
        setErrorMessage(res.error);
        return;
      }

      if (res.status === 429) {
        setResendCooldown(res.retryAfter || 30);
        setErrorStatus(429);
        setErrorDetail(res.detail || 'RATE_LIMITED_IP');
        setErrorMessage(res.error);
        return;
      }

      setErrorStatus(res.status);
      setErrorDetail(res.detail);
      setErrorMessage(res.error);
    },
    [
      isLoading,
      lockoutSeconds,
      otpCountdown,
      countryKey,
      phoneDigits,
      otpDigits,
      username,
      email,
      recordHttpLog,
      updatePendingAuth,
    ]
  );

  const handleLogout = () => {
    clearActiveSession();
    updatePendingAuth(null);
    setActiveUser(null);
    setStep('form');
    setUsername('');
    setEmail('');
    setPassword('');
    setConfirmPassword('');
    setPhoneDigits('');
    setLoginPassword('');
    setOtpDigits(['', '', '', '', '', '']);
    setOtpTtlTotal(DEFAULT_OTP_TTL);
    setOtpCountdown(DEFAULT_OTP_TTL);
    setResendCooldown(0);
    setSendAttemptIndex(0);
    setLockoutSeconds(0);
    setErrorStatus(null);
    setErrorDetail(null);
    setErrorMessage('');
  };

  const handleDeleteAccount = (id) => {
    const next = removeAccountById(id);
    setAccounts(next);
    if (activeUser?.id === id) {
      handleLogout();
    }
  };

  const handleSelectAccountForLogin = (account) => {
    clearActiveSession();
    updatePendingAuth(null);
    setActiveUser(null);
    setAuthMode('login');
    setStep('form');
    setLoginIdentifier(account.username);
    setLoginPassword('');
    setErrorStatus(null);
    setErrorDetail(null);
    setErrorMessage('');
  };

  return (
    <div className="min-h-screen bg-[radial-gradient(ellipse_at_top_left,_#362a40_0%,_#181120_42%,_#060508_100%)] text-zinc-100 flex flex-col">
      {/* Header */}
      <header className="border-b border-white/10 bg-black/25 backdrop-blur-md">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-4 px-4 py-4 sm:px-6 lg:px-8">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-2xl border border-fuchsia-400/30 bg-gradient-to-br from-fuchsia-500/25 to-violet-600/20">
              <Shield className="h-5 w-5 text-fuchsia-300" />
            </div>
            <div>
              <h1 className="text-lg font-bold tracking-tight text-white">
                OTP Shield · Демонстрация бэкенда
              </h1>
              <p className="text-xs text-zinc-400">
                Реальная регистрация и 2FA-вход с защитой от brute-force и спама
              </p>
            </div>
          </div>

          {/* Управление подключением к OTP Shield */}
          <div className="flex flex-wrap items-center gap-2">
            {resetNotice && (
              <span className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-2.5 py-1 text-xs text-emerald-300 animate-fade-in-soft">
                {resetNotice}
              </span>
            )}

            <button
              type="button"
              onClick={handleCheckHealth}
              className={`inline-flex items-center gap-1.5 rounded-xl border px-3 py-1.5 text-xs font-medium transition cursor-pointer ${
                healthState.status === 'online'
                  ? 'border-emerald-500/40 bg-emerald-500/15 text-emerald-300'
                  : healthState.status === 'offline'
                    ? 'border-red-500/40 bg-red-500/15 text-red-300'
                    : 'border-white/10 bg-white/5 text-zinc-300 hover:bg-white/10'
              }`}
              title="Проверить GET /health на OTP-шлюзе"
            >
              {healthState.status === 'checking' ? (
                <Activity className="h-3.5 w-3.5 animate-spin text-fuchsia-300" />
              ) : (
                <ShieldCheck className="h-3.5 w-3.5" />
              )}
              <span>{healthState.detail}</span>
            </button>

            <button
              type="button"
              onClick={handleTestReset}
              className="inline-flex items-center gap-1 rounded-xl border border-white/10 bg-white/5 px-2.5 py-1.5 text-xs text-zinc-400 transition hover:bg-white/10 hover:text-white cursor-pointer"
              title="POST /_test/reset?flush=true — сбросить блокировки на сервере"
            >
              <RotateCcw className="h-3.5 w-3.5" />
              <span className="hidden sm:inline">Reset Demo</span>
            </button>

            {isEditingApi ? (
              <form onSubmit={handleSaveApiUrl} className="flex items-center gap-2">
                <input
                  type="url"
                  value={apiDraft}
                  onChange={(e) => setApiDraft(e.target.value)}
                  placeholder="http://172.20.10.3:8000"
                  className="w-56 rounded-xl border border-fuchsia-400/40 bg-black/50 px-3 py-1.5 font-mono text-xs text-white focus:outline-none"
                />
                <button
                  type="submit"
                  className="inline-flex items-center gap-1 rounded-xl border border-emerald-500/40 bg-emerald-500/15 px-3 py-1.5 text-xs font-medium text-emerald-300 hover:bg-emerald-500/25 cursor-pointer"
                >
                  <Check className="h-3.5 w-3.5" />
                  OK
                </button>
              </form>
            ) : (
              <button
                type="button"
                onClick={() => {
                  setApiDraft(apiBaseUrl);
                  setIsEditingApi(true);
                }}
                className="inline-flex items-center gap-2 rounded-xl border border-white/10 bg-white/5 px-3 py-1.5 text-xs text-zinc-300 transition hover:bg-white/10 cursor-pointer"
                title="Изменить базовый URL OTP Shield (VITE_OTP_API_URL)"
              >
                <Globe className="h-3.5 w-3.5 text-fuchsia-300" />
                <span className="font-mono">{apiBaseUrl}</span>
                <Settings2 className="h-3.5 w-3.5 text-zinc-400" />
              </button>
            )}
          </div>
        </div>
      </header>

      {/* Main Content */}
      <main className="mx-auto flex w-full max-w-7xl flex-1 items-center px-4 py-6 sm:px-6 lg:px-8">
        <div className="grid w-full grid-cols-1 gap-6 lg:grid-cols-12 lg:items-stretch">
          <div className="lg:col-span-5 flex flex-col">
            <RegistrationPanel
              authMode={authMode}
              onChangeAuthMode={(mode) => {
                setAuthMode(mode);
                setErrorStatus(null);
                setErrorDetail(null);
                setErrorMessage('');
              }}
              username={username}
              onChangeUsername={setUsername}
              email={email}
              onChangeEmail={setEmail}
              password={password}
              onChangePassword={setPassword}
              confirmPassword={confirmPassword}
              onChangeConfirmPassword={setConfirmPassword}
              countryKey={countryKey}
              onChangeCountry={(next) => {
                setCountryKey(next);
                setPhoneDigits('');
                setSendAttemptIndex(0);
              }}
              phoneDigits={phoneDigits}
              onChangePhoneDigits={setPhoneDigits}
              loginIdentifier={loginIdentifier}
              onChangeLoginIdentifier={setLoginIdentifier}
              loginPassword={loginPassword}
              onChangeLoginPassword={setLoginPassword}
              step={step}
              pendingAuth={pendingAuth}
              otpDigits={otpDigits}
              onChangeOtpDigits={setOtpDigits}
              otpCountdown={otpCountdown}
              otpTtlTotal={otpTtlTotal}
              resendCooldown={resendCooldown}
              lockoutSeconds={lockoutSeconds}
              errorStatus={errorStatus}
              errorDetail={errorDetail}
              errorMessage={errorMessage}
              shakeKey={shakeKey}
              isLoading={isLoading}
              activeUser={activeUser}
              onSubmitRegister={handleSubmitRegister}
              onSubmitLogin={handleSubmitLogin}
              onResendOtp={handleResendOtp}
              onVerifyCode={handleVerifyCode}
              onBackToForm={() => {
                setStep('form');
                setErrorStatus(null);
                setErrorDetail(null);
                setErrorMessage('');
              }}
              onLogout={handleLogout}
            />
          </div>

          <div className="lg:col-span-7 flex flex-col">
            <BackendDemoPanel
              apiBaseUrl={apiBaseUrl}
              accounts={accounts}
              activeUser={activeUser}
              httpLogs={httpLogs}
              onClearLogs={() => setHttpLogs([])}
              onDeleteAccount={handleDeleteAccount}
              onSelectAccountForLogin={handleSelectAccountForLogin}
            />
          </div>
        </div>
      </main>
    </div>
  );
}
