import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Activity,
  ExternalLink,
  Globe,
  RotateCcw,
  Shield,
  ShieldCheck,
} from 'lucide-react';
import RegistrationPanel from './components/RegistrationPanel';
import CelebrationPage from './components/CelebrationPage';
import {
  API_BASE_URL,
  checkOtpHealth,
  resetTestLimits,
  sendOtp,
  verifyOtp,
} from './api/client';
import {
  clearActiveSession,
  findAccountConflict,
  hashPassword,
  loadActiveSession,
  loadPendingRegistration,
  saveActiveSession,
  savePendingRegistration,
  upsertAccount,
} from './utils/authStore';
import { buildMsisdn, isValidMsisdn } from './utils/phoneMask';

const DEFAULT_OTP_TTL = 120;
const PROGRESSIVE_COOLDOWNS = [30, 60, 120];
const DEFAULT_LOCKOUT_SECONDS = 15 * 60;

export default function App() {
  const [healthState, setHealthState] = useState({
    status: 'idle',
    detail: 'Статус защиты OTP',
  });
  const [resetNotice, setResetNotice] = useState('');

  const [activeUser, setActiveUser] = useState(() => loadActiveSession());
  const [step, setStep] = useState(() => (loadActiveSession() ? 'celebration' : 'form'));

  // Поля формы регистрации
  const [username, setUsername] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [countryKey, setCountryKey] = useState('KG');
  const [phoneDigits, setPhoneDigits] = useState('');

  // Снимок данных пользователя на шаге ввода OTP (гарантирует сохранение логина)
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

  const handleCheckHealth = useCallback(async () => {
    setHealthState({ status: 'checking', detail: 'Проверка /health...' });
    const res = await checkOtpHealth();
    if (res.ok) {
      setHealthState({ status: 'online', detail: 'OTP Shield: ONLINE' });
    } else {
      setHealthState({ status: 'offline', detail: 'OTP Shield: OFFLINE' });
    }
  }, []);

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
      setResetNotice('Лимиты сброшены');
    } else {
      setResetNotice('Ошибка /_test/reset');
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

  // Таймер 3: Блокировка номера 423
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

  const dispatchOtpSend = async (draft) => {
    setIsLoading(true);
    setErrorStatus(null);
    setErrorDetail(null);
    setErrorMessage('');

    const res = await sendOtp(draft.msisdn);
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
    await dispatchOtpSend({
      username: cleanUsername,
      email: email.trim() || null,
      msisdn,
      passwordHash,
    });
  };

  const handleResendOtp = async () => {
    if (isLoading || lockoutSeconds > 0 || resendCooldown > 0) return;
    const draft = pendingAuthRef.current;
    if (!draft?.msisdn) return;
    await dispatchOtpSend(draft);
  };

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
      setIsLoading(false);

      if (res.ok) {
        const userRecord = {
          id: `usr_${Date.now()}`,
          username: (draft?.username || username || '').trim(),
          email: draft?.email ?? (email.trim() || null),
          msisdn,
          passwordHash: draft?.passwordHash || '',
          registeredAt: new Date().toISOString(),
        };

        upsertAccount(userRecord);
        saveActiveSession(userRecord);
        setActiveUser(userRecord);
        updatePendingAuth(null);
        setPassword('');
        setConfirmPassword('');
        setStep('celebration');
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
      updatePendingAuth,
    ]
  );

  const handleRegisterAnother = () => {
    clearActiveSession();
    updatePendingAuth(null);
    setActiveUser(null);
    setStep('form');
    setUsername('');
    setEmail('');
    setPassword('');
    setConfirmPassword('');
    setPhoneDigits('');
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

  return (
    <div className="min-h-screen bg-[radial-gradient(ellipse_at_top,_#362a40_0%,_#181120_48%,_#060508_100%)] text-zinc-100 flex flex-col">
      {/* Компактная шапка */}
      <header className="border-b border-white/10 bg-black/25 backdrop-blur-md">
        <div className="mx-auto flex max-w-5xl flex-wrap items-center justify-between gap-3 px-4 py-3.5 sm:px-6">
          <div className="flex items-center gap-2.5">
            <div className="flex h-9 w-9 items-center justify-center rounded-xl border border-fuchsia-400/30 bg-gradient-to-br from-fuchsia-500/25 to-violet-600/20">
              <Shield className="h-4.5 w-4.5 text-fuchsia-300" />
            </div>
            <div>
              <h1 className="text-base font-bold tracking-tight text-white">
                OTP Shield
              </h1>
              <p className="text-[11px] text-zinc-400">
                Регистрация с SMS-подтверждением
              </p>
            </div>
          </div>

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
              title="POST /_test/reset?flush=true — сбросить лимиты"
            >
              <RotateCcw className="h-3.5 w-3.5" />
              <span className="hidden sm:inline">Reset</span>
            </button>

            <a
              href={API_BASE_URL}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1.5 rounded-xl border border-white/10 bg-white/5 px-2.5 py-1.5 text-xs text-zinc-300 transition hover:bg-white/10 hover:text-white"
              title="Открыть бэкенд OTP Shield"
            >
              <Globe className="h-3.5 w-3.5 text-fuchsia-300" />
              <span className="hidden md:inline font-mono">
                {API_BASE_URL.replace(/^https?:\/\//, '')}
              </span>
              <ExternalLink className="h-3 w-3 text-zinc-400" />
            </a>
          </div>
        </div>
      </header>

      {/* Центрированный экран регистрации или страница поздравления */}
      <main className="mx-auto flex w-full max-w-5xl flex-1 items-center justify-center px-4 py-8 sm:px-6">
        {step === 'celebration' && activeUser ? (
          <CelebrationPage
            user={activeUser}
            onRegisterAnother={handleRegisterAnother}
          />
        ) : (
          <RegistrationPanel
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
            onSubmitRegister={handleSubmitRegister}
            onResendOtp={handleResendOtp}
            onVerifyCode={handleVerifyCode}
            onBackToForm={() => {
              setStep('form');
              setErrorStatus(null);
              setErrorDetail(null);
              setErrorMessage('');
            }}
          />
        )}
      </main>
    </div>
  );
}
