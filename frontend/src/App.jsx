import { useCallback, useEffect, useState } from 'react';
import {
  Activity,
  Check,
  Globe,
  MessageSquarePlus,
  RotateCcw,
  Settings2,
  ShieldCheck,
} from 'lucide-react';
import RegistrationPanel from './components/RegistrationPanel';
import MessageSenderPanel from './components/MessageSenderPanel';
import {
  checkOtpHealth,
  getApiBaseUrl,
  resetTestLimits,
  saveApiBaseUrl,
  sendOtp,
  verifyOtp,
} from './api/client';
import { buildMsisdn, isValidMsisdn } from './utils/phoneMask';

const USER_SESSION_STORAGE_KEY = 'otp_registered_user';
const DEFAULT_OTP_TTL = 120;
const PROGRESSIVE_COOLDOWNS = [30, 60, 120];
const DEFAULT_LOCKOUT_SECONDS = 15 * 60;

function loadStoredUser() {
  try {
    const raw = localStorage.getItem(USER_SESSION_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return parsed && parsed.msisdn ? parsed : null;
  } catch {
    return null;
  }
}

export default function App() {
  // Базовый URL OTP Shield (из .env: VITE_OTP_API_URL=http://172.20.10.3:8000)
  const [apiBaseUrl, setApiBaseUrl] = useState(() => getApiBaseUrl());
  const [isEditingApi, setIsEditingApi] = useState(false);
  const [apiDraft, setApiDraft] = useState(apiBaseUrl);

  // Статус GET /health
  const [healthState, setHealthState] = useState({
    status: 'idle', // 'idle' | 'checking' | 'online' | 'offline'
    detail: 'Статус защиты OTP',
  });
  const [resetNotice, setResetNotice] = useState('');

  // Зарегистрированный профиль в localStorage
  const [registeredUser, setRegisteredUser] = useState(() => loadStoredUser());

  // Поля формы регистрации (Экран 1)
  const [fullName, setFullName] = useState(() => loadStoredUser()?.name || '');
  const [email, setEmail] = useState(() => loadStoredUser()?.email || '');
  const [password, setPassword] = useState('');
  const [countryKey, setCountryKey] = useState('KG');
  const [phoneDigits, setPhoneDigits] = useState('');
  const [step, setStep] = useState(() => (loadStoredUser() ? 'success' : 'phone'));
  const [otpDigits, setOtpDigits] = useState(['', '', '', '', '', '']);
  const [isLoading, setIsLoading] = useState(false);

  // Два независимых таймера (TTL кода 120с + cooldown повторной отправки 30 → 60 → 120с) + блокировка 423
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

  const handleCheckHealth = useCallback(async () => {
    setHealthState({ status: 'checking', detail: 'Проверка /health...' });
    const res = await checkOtpHealth();
    if (res.ok) {
      setHealthState({ status: 'online', detail: 'OTP Shield: ONLINE' });
    } else {
      setHealthState({ status: 'offline', detail: 'OTP Shield: OFFLINE' });
    }
  }, []);

  // Проверяем доступность OTP Shield при открытии страницы
  useEffect(() => {
    handleCheckHealth();
  }, [handleCheckHealth]);

  const handleTestReset = async () => {
    const res = await resetTestLimits();
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

  // POST /otp/send {"msisdn": "996770730000"}
  const handleRequestCode = async () => {
    if (isLoading || lockoutSeconds > 0 || resendCooldown > 0) return;
    const msisdn = buildMsisdn(countryKey, phoneDigits);

    if (!fullName.trim()) {
      setErrorStatus(422);
      setErrorDetail('VALIDATION_ERROR');
      setErrorMessage('Введите имя или логин перед отправкой кода.');
      return;
    }

    if (!isValidMsisdn(msisdn)) {
      setErrorStatus(422);
      setErrorDetail('VALIDATION_ERROR');
      setErrorMessage('Проверьте формат номера (7–15 цифр) или кода (6 цифр)');
      return;
    }

    setIsLoading(true);
    setErrorStatus(null);
    setErrorDetail(null);
    setErrorMessage('');

    const res = await sendOtp(msisdn);
    setIsLoading(false);

    if (res.ok) {
      const ttl = res.expiresIn || DEFAULT_OTP_TTL;
      const fallbackCooldown =
        PROGRESSIVE_COOLDOWNS[
          Math.min(sendAttemptIndex, PROGRESSIVE_COOLDOWNS.length - 1)
        ];
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

  // POST /otp/verify {"msisdn": "996770730000", "code": "012345"}
  const handleVerifyCode = useCallback(
    async (explicitCode) => {
      if (isLoading || lockoutSeconds > 0 || otpCountdown <= 0) return;
      const msisdn = buildMsisdn(countryKey, phoneDigits);
      const code =
        typeof explicitCode === 'string' ? explicitCode : otpDigits.join('');
      if (code.length !== 6) return;

      setIsLoading(true);
      setErrorStatus(null);
      setErrorDetail(null);
      setErrorMessage('');

      const res = await verifyOtp(msisdn, code);
      setIsLoading(false);

      if (res.ok) {
        const userRecord = {
          name: fullName.trim() || 'Пользователь',
          email: email.trim() || null,
          msisdn,
          registeredAt: new Date().toISOString(),
        };
        try {
          localStorage.setItem(
            USER_SESSION_STORAGE_KEY,
            JSON.stringify(userRecord)
          );
        } catch {
          // Ignore storage error
        }
        setRegisteredUser(userRecord);
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
      fullName,
      email,
    ]
  );

  const handleResetSession = () => {
    try {
      localStorage.removeItem(USER_SESSION_STORAGE_KEY);
    } catch {
      // Ignore
    }
    setRegisteredUser(null);
    setStep('phone');
    setOtpDigits(['', '', '', '', '', '']);
    setOtpTtlTotal(DEFAULT_OTP_TTL);
    setOtpCountdown(DEFAULT_OTP_TTL);
    setResendCooldown(0);
    setSendAttemptIndex(0);
    setLockoutSeconds(0);
    setErrorStatus(null);
    setErrorDetail(null);
    setErrorMessage('');
    setPassword('');
  };

  return (
    <div className="min-h-screen bg-[radial-gradient(ellipse_at_top_left,_#362a40_0%,_#181120_42%,_#060508_100%)] text-zinc-100 flex flex-col">
      {/* Header */}
      <header className="border-b border-white/10 bg-black/25 backdrop-blur-md">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-4 px-4 py-4 sm:px-6 lg:px-8">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-2xl border border-fuchsia-400/30 bg-gradient-to-br from-fuchsia-500/25 to-violet-600/20">
              <MessageSquarePlus className="h-5 w-5 text-fuchsia-300" />
            </div>
            <div>
              <h1 className="text-lg font-bold tracking-tight text-white">
                PulseConnect × OTP Shield
              </h1>
              <p className="text-xs text-zinc-400">
                Клиентский сайт регистрации и отправки сообщений
              </p>
            </div>
          </div>

          {/* Управление подключением к OTP Shield (http://172.20.10.3:8000) */}
          <div className="flex flex-wrap items-center gap-2">
            {resetNotice && (
              <span className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-2.5 py-1 text-xs text-emerald-300 animate-fade-in-soft">
                {resetNotice}
              </span>
            )}

            {/* Бейдж "Статус защиты OTP" (GET /health) */}
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
              title="Нажмите для проверки GET /health на OTP-шлюзе"
            >
              {healthState.status === 'checking' ? (
                <Activity className="h-3.5 w-3.5 animate-spin text-fuchsia-300" />
              ) : (
                <ShieldCheck className="h-3.5 w-3.5" />
              )}
              <span>{healthState.detail}</span>
            </button>

            {/* Быстрый сброс лимитов для демо (POST /_test/reset?flush=true) */}
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
              fullName={fullName}
              onChangeFullName={setFullName}
              email={email}
              onChangeEmail={setEmail}
              password={password}
              onChangePassword={setPassword}
              countryKey={countryKey}
              onChangeCountry={(next) => {
                setCountryKey(next);
                setPhoneDigits('');
                setSendAttemptIndex(0);
              }}
              phoneDigits={phoneDigits}
              onChangePhoneDigits={setPhoneDigits}
              step={step}
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
              registeredUser={registeredUser}
              onRequestCode={handleRequestCode}
              onVerifyCode={handleVerifyCode}
              onBackToPhone={() => {
                setStep('phone');
                setErrorStatus(null);
                setErrorDetail(null);
                setErrorMessage('');
              }}
              onResetSession={handleResetSession}
            />
          </div>

          <div className="lg:col-span-7 flex flex-col">
            <MessageSenderPanel
              authToken={null}
              sessionMsisdn={registeredUser?.msisdn || null}
            />
          </div>
        </div>
      </main>
    </div>
  );
}
