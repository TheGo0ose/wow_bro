import { useEffect, useRef, useState } from 'react';
import {
  AlertTriangle,
  ArrowLeft,
  Calendar,
  CheckCircle2,
  Clock,
  Eye,
  EyeOff,
  KeyRound,
  Loader2,
  Lock,
  LogIn,
  LogOut,
  Mail,
  RefreshCw,
  Send,
  ShieldAlert,
  ShieldCheck,
  Smartphone,
  Timer,
  User,
  UserPlus,
  WifiOff,
} from 'lucide-react';
import { resolveOtpErrorMessage } from '../api/client';
import {
  COUNTRY_PRESETS,
  buildMsisdn,
  formatDisplayMsisdn,
  formatMaskedPhone,
  formatTimer,
  isValidMsisdn,
} from '../utils/phoneMask';

const MIN_PASSWORD_LENGTH = 6;

export default function RegistrationPanel({
  authMode,
  onChangeAuthMode,
  // Поля регистрации
  username,
  onChangeUsername,
  email,
  onChangeEmail,
  password,
  onChangePassword,
  confirmPassword,
  onChangeConfirmPassword,
  countryKey,
  onChangeCountry,
  phoneDigits,
  onChangePhoneDigits,
  // Поля входа
  loginIdentifier,
  onChangeLoginIdentifier,
  loginPassword,
  onChangeLoginPassword,
  // Шаг и OTP
  step,
  pendingAuth,
  otpDigits,
  onChangeOtpDigits,
  otpCountdown,
  otpTtlTotal,
  resendCooldown,
  lockoutSeconds,
  errorStatus,
  errorDetail,
  errorMessage,
  shakeKey,
  isLoading,
  activeUser,
  onSubmitRegister,
  onSubmitLogin,
  onResendOtp,
  onVerifyCode,
  onBackToForm,
  onLogout,
}) {
  const inputRefs = useRef([]);
  const lastAutoSubmittedRef = useRef('');
  const [showPassword, setShowPassword] = useState(false);

  const preset = COUNTRY_PRESETS[countryKey] || COUNTRY_PRESETS.KG;
  const currentMsisdn = buildMsisdn(countryKey, phoneDigits);

  // Валидация регистрации (логин, телефон и пароль — строго обязательны!)
  const isUsernameValid = username.trim().length >= 2;
  const isPhoneComplete =
    phoneDigits.length === preset.maxDigits && isValidMsisdn(currentMsisdn);
  const isPasswordValid = password.length >= MIN_PASSWORD_LENGTH;
  const isConfirmMatch =
    confirmPassword.length > 0 && confirmPassword === password;
  const isRegisterReady =
    isUsernameValid && isPhoneComplete && isPasswordValid && isConfirmMatch;

  // Валидация входа
  const isLoginReady =
    loginIdentifier.trim().length >= 2 && loginPassword.length >= MIN_PASSWORD_LENGTH;

  const isResendBlocked = resendCooldown > 0;
  const isLocked = lockoutSeconds > 0;
  const isExpired = errorStatus === 410 || (step === 'otp' && otpCountdown === 0);
  const isOtpComplete = otpDigits.every((d) => d !== '');
  const formattedPhone = formatMaskedPhone(countryKey, phoneDigits);

  const liveRateLimitMessage =
    errorStatus === 429
      ? resolveOtpErrorMessage({
        status: 429,
        detail: errorDetail,
        retryAfter: resendCooldown,
        fallbackError: errorMessage,
      })
      : errorMessage;

  // Фокус на первую ячейку при переходе к экрану подтверждения OTP
  useEffect(() => {
    if (step === 'otp' && !isLocked) {
      lastAutoSubmittedRef.current = '';
      const timer = setTimeout(() => {
        inputRefs.current[0]?.focus();
      }, 80);
      return () => clearTimeout(timer);
    }
  }, [step, isLocked]);

  // Автоотправка POST /otp/verify при вводе 6-й цифры
  useEffect(() => {
    const joined = otpDigits.join('');
    if (
      step === 'otp' &&
      joined.length === 6 &&
      !isLocked &&
      !isExpired &&
      !isLoading &&
      lastAutoSubmittedRef.current !== joined
    ) {
      lastAutoSubmittedRef.current = joined;
      onVerifyCode(joined);
    } else if (joined.length < 6) {
      lastAutoSubmittedRef.current = '';
    }
  }, [otpDigits, step, isLocked, isExpired, isLoading, onVerifyCode]);

  const handlePhoneInputChange = (e) => {
    if (isLocked || isLoading) return;
    const raw = e.target.value.replace(/\D/g, '').slice(0, preset.maxDigits);
    onChangePhoneDigits(raw);
  };

  const handleOtpBoxChange = (index, value) => {
    if (isLocked || isExpired || isLoading) return;
    const digit = value.replace(/\D/g, '').slice(-1);
    const next = [...otpDigits];
    next[index] = digit;
    onChangeOtpDigits(next);

    if (digit && index < 5) {
      inputRefs.current[index + 1]?.focus();
    }
  };

  const handleOtpKeyDown = (index, e) => {
    if (isLocked || isLoading) return;
    if (e.key === 'Backspace') {
      if (otpDigits[index]) {
        const next = [...otpDigits];
        next[index] = '';
        onChangeOtpDigits(next);
      } else if (index > 0) {
        const next = [...otpDigits];
        next[index - 1] = '';
        onChangeOtpDigits(next);
        inputRefs.current[index - 1]?.focus();
      }
      e.preventDefault();
    } else if (e.key === 'ArrowLeft' && index > 0) {
      inputRefs.current[index - 1]?.focus();
    } else if (e.key === 'ArrowRight' && index < 5) {
      inputRefs.current[index + 1]?.focus();
    } else if (e.key === 'Enter' && isOtpComplete && !isExpired) {
      onVerifyCode(otpDigits.join(''));
    }
  };

  const handleOtpPaste = (e) => {
    if (isLocked || isExpired || isLoading) return;
    e.preventDefault();
    const pasted = e.clipboardData.getData('text').replace(/\D/g, '').slice(0, 6);
    if (!pasted) return;
    const next = Array.from({ length: 6 }, (_, i) => pasted[i] || '');
    onChangeOtpDigits(next);
    const focusIdx = Math.min(pasted.length, 5);
    inputRefs.current[focusIdx]?.focus();
  };

  const progressPercent = Math.max(
    0,
    Math.min(100, Math.round((otpCountdown / (otpTtlTotal || 120)) * 100))
  );

  // Данные пользователя на шаге подтверждения OTP (никогда не сбрасываются!)
  const otpTargetName = pendingAuth?.username || username.trim();
  const otpTargetMsisdn = pendingAuth?.msisdn || currentMsisdn;

  return (
    <section
      className={`relative flex flex-col justify-between rounded-3xl border p-6 sm:p-8 backdrop-blur-xl transition-all duration-500 overflow-hidden ${
        isLocked
          ? 'border-red-500/60 bg-gradient-to-br from-[#3b1520]/95 via-[#1c0c14]/95 to-[#0b0609]/98 shadow-[0_0_85px_-15px_rgba(239,68,68,0.45)]'
          : step === 'success'
            ? 'border-emerald-500/40 bg-gradient-to-br from-[#1f332c]/90 via-[#12161d]/95 to-[#09080d]/98 shadow-[0_0_70px_-20px_rgba(16,185,129,0.3)]'
            : 'border-white/10 bg-gradient-to-br from-[#362a40]/90 via-[#1b1523]/95 to-[#0b090f]/98 shadow-[0_25px_70px_-20px_rgba(0,0,0,0.85)]'
      }`}
    >
      <div
        className={`pointer-events-none absolute -top-24 -left-24 h-64 w-64 rounded-full blur-3xl transition-colors duration-500 ${
          isLocked
            ? 'bg-red-600/20'
            : step === 'success'
              ? 'bg-emerald-500/15'
              : 'bg-fuchsia-600/15'
        }`}
      />

      <div className="relative z-10">
        {/* Header + Switcher Регистрация / Вход */}
        <div className="flex flex-wrap items-center justify-between gap-3 mb-6">
          <div className="flex items-center gap-2.5">
            <div
              className={`flex h-9 w-9 items-center justify-center rounded-xl border transition-colors ${
                isLocked
                  ? 'border-red-500/40 bg-red-500/15 text-red-400'
                  : step === 'success'
                    ? 'border-emerald-500/40 bg-emerald-500/15 text-emerald-400'
                    : 'border-fuchsia-400/25 bg-fuchsia-500/10 text-fuchsia-300'
              }`}
            >
              {isLocked ? (
                <Lock className="h-4.5 w-4.5" />
              ) : step === 'success' ? (
                <ShieldCheck className="h-4.5 w-4.5" />
              ) : (
                <KeyRound className="h-4.5 w-4.5" />
              )}
            </div>
            <div>
              <span className="text-[11px] font-medium uppercase tracking-widest text-zinc-400">
                OTP Shield Auth
              </span>
              <h2 className="text-base font-semibold text-white leading-tight">
                {step === 'success'
                  ? 'Личный кабинет'
                  : authMode === 'register'
                    ? 'Регистрация аккаунта'
                    : 'Вход с 2FA защитой'}
              </h2>
            </div>
          </div>

          {step === 'form' ? (
            <div className="flex rounded-xl border border-white/10 bg-black/35 p-0.5 text-xs">
              <button
                type="button"
                disabled={isLoading}
                onClick={() => onChangeAuthMode('register')}
                className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 font-medium transition cursor-pointer ${
                  authMode === 'register'
                    ? 'bg-fuchsia-500/25 text-fuchsia-200 border border-fuchsia-400/30'
                    : 'text-zinc-400 hover:text-white'
                }`}
              >
                <UserPlus className="h-3.5 w-3.5" />
                Регистрация
              </button>
              <button
                type="button"
                disabled={isLoading}
                onClick={() => onChangeAuthMode('login')}
                className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 font-medium transition cursor-pointer ${
                  authMode === 'login'
                    ? 'bg-fuchsia-500/25 text-fuchsia-200 border border-fuchsia-400/30'
                    : 'text-zinc-400 hover:text-white'
                }`}
              >
                <LogIn className="h-3.5 w-3.5" />
                Вход
              </button>
            </div>
          ) : (
            <span
              className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 font-mono text-[11px] font-medium ${
                isLocked
                  ? 'border-red-500/40 bg-red-500/15 text-red-300'
                  : errorStatus === 429
                    ? 'border-amber-400/40 bg-amber-400/15 text-amber-200'
                    : step === 'success'
                      ? 'border-emerald-500/40 bg-emerald-500/15 text-emerald-300'
                      : 'border-white/10 bg-white/5 text-zinc-300'
              }`}
            >
              {isLocked
                ? 'MSISDN_LOCKED'
                : errorStatus === 429
                  ? errorDetail || 'RATE_LIMITED'
                  : step === 'success'
                    ? 'VERIFIED'
                    : '2FA SMS STEP'}
            </span>
          )}
        </div>

        {/* 429 Rate Limit Banner */}
        {errorStatus === 429 && isResendBlocked && !isLocked && step !== 'success' && (
          <div className="mb-5 flex items-start gap-3 rounded-2xl border border-amber-400/40 bg-amber-400/10 p-3.5 text-amber-200 animate-fade-in">
            <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-amber-400" />
            <div className="flex-1 text-xs sm:text-sm">
              <div className="flex items-center justify-between gap-2 font-semibold text-amber-300">
                <span>Прогрессивная задержка</span>
                <span className="font-mono text-xs rounded-md bg-amber-400/20 px-2 py-0.5 text-amber-200">
                  {formatTimer(resendCooldown)}
                </span>
              </div>
              <p className="mt-1 text-xs text-amber-200/90 leading-relaxed">
                {liveRateLimitMessage}
              </p>
            </div>
          </div>
        )}

        {/* 423 MSISDN_LOCKED Banner */}
        {isLocked && (
          <div className="mb-5 flex items-start gap-3 rounded-2xl border border-red-500/50 bg-red-500/15 p-4 text-red-100 animate-fade-in">
            <ShieldAlert className="mt-0.5 h-5 w-5 shrink-0 text-red-400 animate-pulse" />
            <div className="flex-1">
              <div className="flex items-center justify-between gap-2">
                <span className="text-sm font-semibold text-red-200">
                  Номер заблокирован (423)
                </span>
                <span className="inline-flex items-center gap-1 rounded-lg border border-red-400/40 bg-red-950/80 px-2.5 py-0.5 font-mono text-sm font-semibold text-red-300">
                  <Timer className="h-3.5 w-3.5" />
                  {formatTimer(lockoutSeconds)}
                </span>
              </div>
              <p className="mt-1.5 text-xs text-red-200/90 leading-relaxed">
                Номер заблокирован из-за 5 ошибок. Повторите через {lockoutSeconds} сек
              </p>
            </div>
          </div>
        )}
      </div>

      {/* Main Content */}
      <div className="relative z-10 my-auto py-2">
        {step === 'success' && activeUser ? (
          /* Экран 3 — Успешная регистрация / Личный кабинет */
          <div className="flex flex-col items-center text-center py-4 animate-fade-in">
            <div className="mb-4 flex h-20 w-20 items-center justify-center rounded-full border border-emerald-400/40 bg-emerald-500/15 shadow-[0_0_45px_rgba(16,185,129,0.4)] animate-scale-pop">
              <CheckCircle2 className="h-11 w-11 text-emerald-400" strokeWidth={2.2} />
            </div>

            <span className="mb-2 inline-flex items-center gap-1.5 rounded-full border border-emerald-500/30 bg-emerald-500/10 px-3 py-1 font-mono text-xs text-emerald-300">
              HTTP 200 OK · OTP VERIFIED
            </span>

            <h3 className="text-2xl sm:text-3xl font-bold tracking-tight text-white">
              Аккаунт подтверждён
            </h3>
            <p className="mt-1 text-sm text-emerald-300/90">
              Добро пожаловать, <span className="font-semibold text-white">{activeUser.username}</span>!
            </p>

            <div className="mt-5 w-full rounded-2xl border border-white/10 bg-black/35 p-4 text-left text-xs space-y-2.5">
              <div className="flex items-center justify-between">
                <span className="flex items-center gap-1.5 text-zinc-400">
                  <User className="h-3.5 w-3.5 text-fuchsia-300" />
                  Логин / Имя:
                </span>
                <span className="font-semibold text-white">
                  {activeUser.username}
                </span>
              </div>
              <div className="flex items-center justify-between">
                <span className="flex items-center gap-1.5 text-zinc-400">
                  <Smartphone className="h-3.5 w-3.5 text-emerald-400" />
                  Номер телефона:
                </span>
                <span className="font-mono font-medium text-emerald-300">
                  {formatDisplayMsisdn(activeUser.msisdn)}
                </span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-zinc-500 font-mono">msisdn:</span>
                <span className="font-mono text-zinc-300">
                  {activeUser.msisdn}
                </span>
              </div>
              <div className="flex items-center justify-between">
                <span className="flex items-center gap-1.5 text-zinc-400">
                  <Lock className="h-3.5 w-3.5 text-fuchsia-300" />
                  Защита пароля:
                </span>
                <span className="font-mono text-emerald-300">
                  SHA-256 · 2FA активна
                </span>
              </div>
              {activeUser.email && (
                <div className="flex items-center justify-between">
                  <span className="flex items-center gap-1.5 text-zinc-400">
                    <Mail className="h-3.5 w-3.5 text-violet-300" />
                    Email:
                  </span>
                  <span className="text-zinc-200">{activeUser.email}</span>
                </div>
              )}
              {activeUser.registeredAt && (
                <div className="flex items-center justify-between border-t border-white/10 pt-2">
                  <span className="flex items-center gap-1.5 text-zinc-500">
                    <Calendar className="h-3.5 w-3.5" />
                    Дата регистрации:
                  </span>
                  <span className="font-mono text-zinc-400">
                    {new Date(activeUser.registeredAt).toLocaleTimeString('ru-RU')}
                  </span>
                </div>
              )}
            </div>

            <button
              type="button"
              onClick={onLogout}
              className="mt-6 inline-flex w-full items-center justify-center gap-2 rounded-xl border border-white/15 bg-white/5 px-5 py-3 text-sm font-medium text-white transition hover:bg-white/10 hover:border-white/25 cursor-pointer"
            >
              <LogOut className="h-4 w-4" />
              Выйти / Зарегистрировать новый номер
            </button>
          </div>
        ) : step === 'form' && authMode === 'register' ? (
          /* Экран 1A — Регистрация (Имя, Телефон, Пароль + Подтверждение — ОБЯЗАТЕЛЬНЫ) */
          <div className="animate-fade-in">
            <div className="mb-5">
              <h3 className="text-2xl font-bold tracking-tight text-white">
                Создать аккаунт
              </h3>
              <p className="mt-1 text-sm text-zinc-400">
                Заполните логин, номер и пароль — далее подтвердите телефон через OTP Shield.
              </p>
            </div>

            <form
              onSubmit={(e) => {
                e.preventDefault();
                if (isRegisterReady) {
                  onSubmitRegister();
                }
              }}
              className="space-y-3.5"
            >
              {/* Логин / Имя пользователя */}
              <div>
                <label
                  htmlFor="reg-name"
                  className="mb-1.5 block text-xs font-medium uppercase tracking-wider text-zinc-400"
                >
                  Логин / Имя пользователя <span className="text-fuchsia-400">*</span>
                </label>
                <div className="flex items-center rounded-2xl border border-white/15 bg-black/40 px-3.5 py-2.5 transition-all focus-within:border-fuchsia-400/70 focus-within:ring-2 focus-within:ring-fuchsia-500/20">
                  <User className="mr-3 h-4.5 w-4.5 shrink-0 text-zinc-400" />
                  <input
                    id="reg-name"
                    name="username"
                    autoComplete="username"
                    type="text"
                    required
                    minLength={2}
                    disabled={isLocked || isLoading}
                    value={username}
                    onChange={(e) => onChangeUsername(e.target.value)}
                    placeholder="Например, Айбек"
                    className="w-full bg-transparent text-sm text-white placeholder:text-zinc-600 focus:outline-none"
                  />
                  {isUsernameValid && (
                    <CheckCircle2 className="ml-2 h-4 w-4 shrink-0 text-emerald-400" />
                  )}
                </div>
              </div>

              {/* Номер телефона */}
              <div>
                <div className="mb-1.5 flex items-center justify-between">
                  <label
                    htmlFor="phone-input"
                    className="text-xs font-medium uppercase tracking-wider text-zinc-400"
                  >
                    Номер телефона <span className="text-fuchsia-400">*</span>
                  </label>
                  <div className="flex items-center gap-1 rounded-lg border border-white/10 bg-black/30 p-0.5">
                    {Object.entries(COUNTRY_PRESETS).map(([key, item]) => (
                      <button
                        key={key}
                        type="button"
                        disabled={isLocked || isLoading}
                        onClick={() => onChangeCountry(key)}
                        className={`rounded-md px-2 py-0.5 text-[11px] font-medium transition cursor-pointer ${
                          countryKey === key
                            ? 'bg-fuchsia-500/25 text-fuchsia-200 border border-fuchsia-400/30'
                            : 'text-zinc-400 hover:text-zinc-200'
                        }`}
                      >
                        {item.code}
                      </button>
                    ))}
                  </div>
                </div>

                <div
                  className={`flex items-center rounded-2xl border bg-black/40 px-3.5 py-2.5 transition-all ${
                    isLocked
                      ? 'border-red-500/40 opacity-50 cursor-not-allowed'
                      : 'border-white/15 focus-within:border-fuchsia-400/70 focus-within:ring-2 focus-within:ring-fuchsia-500/20'
                  }`}
                >
                  <Smartphone className="mr-3 h-4.5 w-4.5 shrink-0 text-zinc-400" />
                  <span className="mr-2 select-none font-mono text-sm font-semibold text-fuchsia-300">
                    {preset.code}
                  </span>
                  <input
                    id="phone-input"
                    type="tel"
                    inputMode="numeric"
                    required
                    disabled={isLocked || isLoading}
                    value={formattedPhone}
                    onChange={handlePhoneInputChange}
                    placeholder={preset.placeholder}
                    className="w-full bg-transparent font-mono text-sm text-white placeholder:text-zinc-600 focus:outline-none disabled:cursor-not-allowed"
                  />
                  {isPhoneComplete && (
                    <CheckCircle2 className="ml-2 h-4 w-4 shrink-0 text-emerald-400" />
                  )}
                </div>
                <div className="mt-1 flex items-center justify-between text-[11px] text-zinc-500 font-mono">
                  <span>Формат: 7–15 цифр</span>
                  <span>msisdn: &quot;{currentMsisdn}&quot;</span>
                </div>
              </div>

              {/* Обязательный Пароль и Подтверждение пароля */}
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <div>
                  <label
                    htmlFor="reg-password"
                    className="mb-1.5 block text-xs font-medium uppercase tracking-wider text-zinc-400"
                  >
                    Пароль <span className="text-fuchsia-400">*</span>
                  </label>
                  <div className="flex items-center rounded-2xl border border-white/15 bg-black/40 px-3 py-2.5 transition-all focus-within:border-fuchsia-400/70 focus-within:ring-2 focus-within:ring-fuchsia-500/20">
                    <Lock className="mr-2 h-4 w-4 shrink-0 text-zinc-400" />
                    <input
                      id="reg-password"
                      name="new-password"
                      autoComplete="new-password"
                      type={showPassword ? 'text' : 'password'}
                      required
                      minLength={MIN_PASSWORD_LENGTH}
                      disabled={isLocked || isLoading}
                      value={password}
                      onChange={(e) => onChangePassword(e.target.value)}
                      placeholder="Мин. 6 символов"
                      className="w-full bg-transparent text-sm text-white placeholder:text-zinc-600 focus:outline-none"
                    />
                    <button
                      type="button"
                      onClick={() => setShowPassword((v) => !v)}
                      className="ml-1.5 text-zinc-500 hover:text-zinc-300 cursor-pointer"
                      aria-label={showPassword ? 'Скрыть пароль' : 'Показать пароль'}
                    >
                      {showPassword ? (
                        <EyeOff className="h-3.5 w-3.5" />
                      ) : (
                        <Eye className="h-3.5 w-3.5" />
                      )}
                    </button>
                  </div>
                </div>

                <div>
                  <label
                    htmlFor="reg-password-confirm"
                    className="mb-1.5 block text-xs font-medium uppercase tracking-wider text-zinc-400"
                  >
                    Повтор пароля <span className="text-fuchsia-400">*</span>
                  </label>
                  <div
                    className={`flex items-center rounded-2xl border bg-black/40 px-3 py-2.5 transition-all ${
                      confirmPassword.length > 0 && !isConfirmMatch
                        ? 'border-red-500/60 focus-within:border-red-400'
                        : 'border-white/15 focus-within:border-fuchsia-400/70 focus-within:ring-2 focus-within:ring-fuchsia-500/20'
                    }`}
                  >
                    <Lock className="mr-2 h-4 w-4 shrink-0 text-zinc-400" />
                    <input
                      id="reg-password-confirm"
                      name="confirm-password"
                      autoComplete="new-password"
                      type={showPassword ? 'text' : 'password'}
                      required
                      minLength={MIN_PASSWORD_LENGTH}
                      disabled={isLocked || isLoading}
                      value={confirmPassword}
                      onChange={(e) => onChangeConfirmPassword(e.target.value)}
                      placeholder="Повторите пароль"
                      className="w-full bg-transparent text-sm text-white placeholder:text-zinc-600 focus:outline-none"
                    />
                    {isConfirmMatch && (
                      <CheckCircle2 className="ml-1.5 h-4 w-4 shrink-0 text-emerald-400" />
                    )}
                  </div>
                </div>
              </div>

              {confirmPassword.length > 0 && !isConfirmMatch && (
                <p className="text-xs text-red-300">Пароли не совпадают.</p>
              )}

              {/* Опциональный Email */}
              <div>
                <label
                  htmlFor="reg-email"
                  className="mb-1.5 block text-xs font-medium uppercase tracking-wider text-zinc-500"
                >
                  Email (опционально)
                </label>
                <div className="flex items-center rounded-xl border border-white/10 bg-black/30 px-3 py-2 focus-within:border-fuchsia-400/50">
                  <Mail className="mr-2.5 h-3.5 w-3.5 shrink-0 text-zinc-500" />
                  <input
                    id="reg-email"
                    type="email"
                    disabled={isLocked || isLoading}
                    value={email}
                    onChange={(e) => onChangeEmail(e.target.value)}
                    placeholder="user@inai.kg"
                    className="w-full bg-transparent text-xs text-white placeholder:text-zinc-600 focus:outline-none"
                  />
                </div>
              </div>

              {errorMessage && !isLocked && errorStatus !== 429 && (
                <div className="flex items-start gap-2 rounded-xl border border-red-500/40 bg-red-500/10 px-3 py-2.5 text-xs text-red-200 animate-fade-in-soft">
                  {errorStatus === 0 ? (
                    <WifiOff className="mt-0.5 h-4 w-4 shrink-0 text-red-400" />
                  ) : (
                    <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-red-400" />
                  )}
                  <span>{errorMessage}</span>
                </div>
              )}

              <button
                type="submit"
                disabled={
                  isResendBlocked || isLocked || isLoading || !isRegisterReady
                }
                className={`group flex w-full items-center justify-center gap-2 rounded-2xl px-5 py-3.5 text-sm font-semibold transition-all duration-200 ${
                  isResendBlocked || isLocked || isLoading || !isRegisterReady
                  ? 'bg-zinc-800/80 text-zinc-500 border border-white/5 cursor-not-allowed'
                  : 'bg-gradient-to-r from-fuchsia-600 via-purple-600 to-violet-600 text-white shadow-lg shadow-purple-900/40 hover:brightness-110 active:scale-[0.99] cursor-pointer'
                  }`}
              >
                {isLoading ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <Send className="h-4 w-4 transition-transform group-hover:translate-x-0.5" />
                )}
                <span>
                  {isLoading
                    ? 'Отправка SMS...'
                    : isResendBlocked
                      ? `Подождите (${formatTimer(resendCooldown)})`
                      : isLocked
                        ? `Заблокировано (${formatTimer(lockoutSeconds)})`
                        : 'Получить код по SMS'}
                </span>
              </button>
            </form>
          </div>
        ) : step === 'form' && authMode === 'login' ? (
          /* Экран 1B — Вход в зарегистрированный аккаунт (Логин/Телефон + Пароль → OTP 2FA) */
          <div className="animate-fade-in">
            <div className="mb-5">
              <h3 className="text-2xl font-bold tracking-tight text-white">
                Вход в аккаунт
              </h3>
              <p className="mt-1 text-sm text-zinc-400">
                Введите логин (или телефон) и пароль — затем подтвердите вход по SMS через OTP Shield.
              </p>
            </div>

            <form
              onSubmit={(e) => {
                e.preventDefault();
                if (isLoginReady) {
                  onSubmitLogin();
                }
              }}
              className="space-y-4"
            >
              <div>
                <label
                  htmlFor="login-identifier"
                  className="mb-1.5 block text-xs font-medium uppercase tracking-wider text-zinc-400"
                >
                  Логин или номер телефона <span className="text-fuchsia-400">*</span>
                </label>
                <div className="flex items-center rounded-2xl border border-white/15 bg-black/40 px-3.5 py-3 transition-all focus-within:border-fuchsia-400/70 focus-within:ring-2 focus-within:ring-fuchsia-500/20">
                  <User className="mr-3 h-4.5 w-4.5 shrink-0 text-zinc-400" />
                  <input
                    id="login-identifier"
                    type="text"
                    required
                    disabled={isLoading}
                    value={loginIdentifier}
                    onChange={(e) => onChangeLoginIdentifier(e.target.value)}
                    placeholder="Айбек или 996770730000"
                    className="w-full bg-transparent text-sm text-white placeholder:text-zinc-600 focus:outline-none"
                  />
                </div>
              </div>

              <div>
                <label
                  htmlFor="login-password"
                  className="mb-1.5 block text-xs font-medium uppercase tracking-wider text-zinc-400"
                >
                  Пароль <span className="text-fuchsia-400">*</span>
                </label>
                <div className="flex items-center rounded-2xl border border-white/15 bg-black/40 px-3.5 py-3 transition-all focus-within:border-fuchsia-400/70 focus-within:ring-2 focus-within:ring-fuchsia-500/20">
                  <Lock className="mr-3 h-4.5 w-4.5 shrink-0 text-zinc-400" />
                  <input
                    id="login-password"
                    type={showPassword ? 'text' : 'password'}
                    required
                    disabled={isLoading}
                    value={loginPassword}
                    onChange={(e) => onChangeLoginPassword(e.target.value)}
                    placeholder="Введите пароль"
                    className="w-full bg-transparent text-sm text-white placeholder:text-zinc-600 focus:outline-none"
                  />
                  <button
                    type="button"
                    onClick={() => setShowPassword((v) => !v)}
                    className="ml-2 text-zinc-500 hover:text-zinc-300 cursor-pointer"
                    aria-label={showPassword ? 'Скрыть пароль' : 'Показать пароль'}
                  >
                    {showPassword ? (
                      <EyeOff className="h-4 w-4" />
                    ) : (
                      <Eye className="h-4 w-4" />
                    )}
                  </button>
                </div>
              </div>

              {errorMessage && !isLocked && errorStatus !== 429 && (
                <div className="flex items-start gap-2 rounded-xl border border-red-500/40 bg-red-500/10 px-3 py-2.5 text-xs text-red-200 animate-fade-in-soft">
                  {errorStatus === 0 ? (
                    <WifiOff className="mt-0.5 h-4 w-4 shrink-0 text-red-400" />
                  ) : (
                    <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-red-400" />
                  )}
                  <span>{errorMessage}</span>
                </div>
              )}

              <button
                type="submit"
                disabled={isResendBlocked || isLocked || isLoading || !isLoginReady}
                className={`group flex w-full items-center justify-center gap-2 rounded-2xl px-5 py-3.5 text-sm font-semibold transition-all duration-200 ${
                  isResendBlocked || isLocked || isLoading || !isLoginReady
                    ? 'bg-zinc-800/80 text-zinc-500 border border-white/5 cursor-not-allowed'
                    : 'bg-gradient-to-r from-fuchsia-600 via-purple-600 to-violet-600 text-white shadow-lg shadow-purple-900/40 hover:brightness-110 active:scale-[0.99] cursor-pointer'
                }`}
              >
                {isLoading ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <LogIn className="h-4 w-4" />
                )}
                <span>
                  {isLoading
                    ? 'Проверка и отправка SMS...'
                    : isResendBlocked
                      ? `Подождите (${formatTimer(resendCooldown)})`
                      : 'Войти и получить SMS-код'}
                </span>
              </button>
            </form>
          </div>
        ) : (
          /* Экран 2 — Подтверждение номера по OTP */
          <div className="animate-fade-in">
            <div className="mb-4 flex items-center justify-between gap-2">
              <button
                type="button"
                disabled={isLocked || isLoading}
                onClick={onBackToForm}
                className="inline-flex items-center gap-1.5 text-xs font-medium text-zinc-400 transition hover:text-white disabled:opacity-40 disabled:cursor-not-allowed cursor-pointer"
              >
                <ArrowLeft className="h-3.5 w-3.5" />
                Изменить данные
              </button>
              <span className="inline-flex items-center gap-1.5 rounded-lg border border-fuchsia-400/25 bg-fuchsia-500/10 px-2.5 py-1 text-xs font-medium text-fuchsia-200">
                <User className="h-3.5 w-3.5" />
                {otpTargetName}
              </span>
            </div>

            <div className="mb-5">
              <h3 className="text-xl sm:text-2xl font-bold tracking-tight text-white">
                Подтверждение номера
              </h3>
              <p className="mt-1 text-sm text-zinc-400">
                Мы отправили 6-значный код для{' '}
                <span className="font-semibold text-white">{otpTargetName}</span>{' '}
                на{' '}
                <span className="font-mono font-medium text-zinc-200">
                  {formatDisplayMsisdn(otpTargetMsisdn)}
                </span>
              </p>
            </div>

            {/* 6 отдельных ячеек ввода кода */}
            <div
              key={shakeKey}
              className={`${errorStatus === 401 ? 'animate-shake' : ''}`}
            >
              <div
                className={`grid grid-cols-6 gap-2 sm:gap-3 rounded-2xl p-2 transition-colors ${
                  isLocked ? 'bg-red-950/30 border border-red-500/30 opacity-55' : ''
                }`}
                onPaste={handleOtpPaste}
              >
                {otpDigits.map((digit, index) => {
                  const hasError =
                    errorStatus === 401 || errorStatus === 410 || errorStatus === 422;
                  return (
                    <input
                      key={index}
                      ref={(el) => {
                        inputRefs.current[index] = el;
                      }}
                      type="text"
                      inputMode="numeric"
                      maxLength={1}
                      disabled={isLocked || isExpired || isLoading}
                      value={digit}
                      onChange={(e) => handleOtpBoxChange(index, e.target.value)}
                      onKeyDown={(e) => handleOtpKeyDown(index, e)}
                      aria-label={`Цифра ${index + 1}`}
                      className={`h-13 sm:h-14 w-full rounded-xl border text-center font-mono text-xl sm:text-2xl font-bold transition-all focus:outline-none ${isLocked
                        ? 'border-red-500/40 bg-red-950/50 text-red-300/60 cursor-not-allowed'
                        : isExpired
                          ? 'border-rose-500/35 bg-black/30 text-zinc-500 cursor-not-allowed'
                          : hasError
                            ? 'border-red-500/80 bg-red-500/15 text-red-200 ring-2 ring-red-500/25'
                            : digit
                              ? 'border-fuchsia-400/70 bg-fuchsia-500/10 text-white shadow-[0_0_20px_-6px_rgba(217,70,239,0.5)]'
                              : 'border-white/15 bg-black/45 text-white focus:border-fuchsia-400 focus:ring-2 focus:ring-fuchsia-500/25'
                        }`}
                    />
                  );
                })}
              </div>
            </div>

            {errorMessage && !isLocked && errorStatus !== 429 && (
              <div className="mt-3 flex items-start gap-2 rounded-xl border border-red-500/40 bg-red-500/10 px-3 py-2.5 text-xs font-medium text-red-200 animate-fade-in-soft">
                {errorStatus === 0 ? (
                  <WifiOff className="mt-0.5 h-4 w-4 shrink-0 text-red-400" />
                ) : (
                  <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-red-400" />
                )}
                <span>{errorMessage}</span>
              </div>
            )}

            {/* Два независимых таймера */}
            <div className="mt-5 rounded-2xl border border-white/10 bg-black/30 p-3.5">
              <div className="flex items-center justify-between text-xs">
                <div className="flex items-center gap-2 text-zinc-300">
                  <Clock
                    className={`h-4 w-4 ${
                      otpCountdown <= 15 && otpCountdown > 0
                        ? 'text-amber-400 animate-pulse'
                        : otpCountdown === 0
                          ? 'text-rose-400'
                          : 'text-fuchsia-300'
                    }`}
                  />
                  <span>
                    {otpCountdown > 0
                      ? 'Код действует ещё:'
                      : 'Срок действия кода истёк'}
                  </span>
                </div>
                <span
                  className={`font-mono text-sm font-semibold ${
                    otpCountdown === 0
                      ? 'text-rose-400'
                      : otpCountdown <= 20
                        ? 'text-amber-300'
                        : 'text-white'
                  }`}
                >
                  {formatTimer(otpCountdown)}
                </span>
              </div>

              <div className="mt-2.5 h-1.5 w-full overflow-hidden rounded-full bg-white/10">
                <div
                  className={`h-full rounded-full transition-all duration-1000 ease-linear ${
                    otpCountdown === 0
                      ? 'bg-rose-500'
                      : otpCountdown <= 20
                        ? 'bg-amber-400'
                        : 'bg-gradient-to-r from-fuchsia-500 to-violet-400'
                  }`}
                  style={{ width: `${progressPercent}%` }}
                />
              </div>

              <div className="mt-3 flex items-center justify-between">
                <span className="text-[11px] text-zinc-500">
                  TTL: {otpTtlTotal || 120} сек
                </span>
                <button
                  type="button"
                  disabled={isResendBlocked || isLocked || isLoading}
                  onClick={onResendOtp}
                  className={`inline-flex items-center gap-1.5 text-xs font-medium transition ${isResendBlocked || isLocked || isLoading
                    ? 'text-zinc-500 cursor-not-allowed'
                    : 'text-fuchsia-300 hover:text-fuchsia-200 cursor-pointer'
                    }`}
                >
                  <RefreshCw className="h-3.5 w-3.5" />
                  {isResendBlocked
                    ? `Отправить повторно (${formatTimer(resendCooldown)})`
                    : 'Отправить код повторно'}
                </button>
              </div>
            </div>

            <button
              type="button"
              disabled={isLocked || isExpired || !isOtpComplete || isLoading}
              onClick={() => onVerifyCode(otpDigits.join(''))}
              className={`mt-5 flex w-full items-center justify-center gap-2 rounded-2xl px-5 py-3.5 text-sm font-semibold transition-all duration-200 ${
                isLocked || isExpired || !isOtpComplete || isLoading
                  ? 'bg-zinc-800/80 text-zinc-500 border border-white/5 cursor-not-allowed'
                  : 'bg-gradient-to-r from-fuchsia-600 via-purple-600 to-violet-600 text-white shadow-lg shadow-purple-900/40 hover:brightness-110 active:scale-[0.99] cursor-pointer'
              }`}
            >
              {isLoading ? (
                <Loader2 className="h-4.5 w-4.5 animate-spin" />
              ) : (
                <ShieldCheck className="h-4.5 w-4.5" />
              )}
              <span>{isLoading ? 'Проверка кода...' : 'Подтвердить'}</span>
            </button>
          </div>
        )}
      </div>

      <div className="relative z-10 mt-6 flex items-center justify-between border-t border-white/10 pt-4 text-[11px] text-zinc-500">
        <span>Демо-клиент OTP Shield (ТЗ 6)</span>
        <span className="font-mono text-zinc-400">
          {activeUser ? `USER: ${activeUser.username}` : 'SESSION: GUEST'}
        </span>
      </div>
    </section>
  );
}
