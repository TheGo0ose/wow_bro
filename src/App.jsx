import { useEffect, useState } from 'react';
import { Check, Globe, MessageSquarePlus, Settings2 } from 'lucide-react';
import RegistrationPanel from './components/RegistrationPanel';
import MessageSenderPanel from './components/MessageSenderPanel';
import {
  getApiBaseUrl,
  requestOtpCode,
  saveApiBaseUrl,
  verifyOtpCode,
} from './api/client';
import { buildMsisdn } from './utils/phoneMask';

const OTP_WINDOW_SECONDS = 120;
const DEFAULT_LOCKOUT_SECONDS = 15 * 60;

export default function App() {
  // External API endpoint configuration (no built-in OTP service)
  const [apiBaseUrl, setApiBaseUrl] = useState(() => getApiBaseUrl());
  const [isEditingApi, setIsEditingApi] = useState(false);
  const [apiDraft, setApiDraft] = useState(apiBaseUrl);

  // Registration / Login state
  const [authMode, setAuthMode] = useState('register'); // 'register' | 'login'
  const [fullName, setFullName] = useState('');
  const [countryKey, setCountryKey] = useState('RU');
  const [phoneDigits, setPhoneDigits] = useState('');
  const [step, setStep] = useState('phone'); // 'phone' | 'otp' | 'success'
  const [otpDigits, setOtpDigits] = useState(['', '', '', '', '', '']);
  const [isLoading, setIsLoading] = useState(false);

  // Session state returned by external API
  const [authToken, setAuthToken] = useState(null);
  const [sessionMsisdn, setSessionMsisdn] = useState(null);

  // Countdown timers driven by API responses
  const [otpCountdown, setOtpCountdown] = useState(OTP_WINDOW_SECONDS);
  const [rateLimitSeconds, setRateLimitSeconds] = useState(0);
  const [lockoutSeconds, setLockoutSeconds] = useState(0);

  // Error UI state
  const [errorStatus, setErrorStatus] = useState(null);
  const [errorMessage, setErrorMessage] = useState('');
  const [shakeKey, setShakeKey] = useState(0);

  // 120-second code input countdown
  useEffect(() => {
    if (step !== 'otp' || otpCountdown <= 0 || lockoutSeconds > 0) return;
    const timer = setInterval(() => {
      setOtpCountdown((prev) => (prev <= 1 ? 0 : prev - 1));
    }, 1000);
    return () => clearInterval(timer);
  }, [step, otpCountdown, lockoutSeconds]);

  // 429 retry-after countdown
  useEffect(() => {
    if (rateLimitSeconds <= 0) return;
    const timer = setInterval(() => {
      setRateLimitSeconds((prev) => (prev <= 1 ? 0 : prev - 1));
    }, 1000);
    return () => clearInterval(timer);
  }, [rateLimitSeconds]);

  // 423 lockout countdown
  useEffect(() => {
    if (lockoutSeconds <= 0) return;
    const timer = setInterval(() => {
      setLockoutSeconds((prev) => {
        if (prev <= 1) {
          setErrorStatus(null);
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
  };

  const handleRequestCode = async () => {
    if (isLoading || lockoutSeconds > 0 || rateLimitSeconds > 0) return;
    const msisdn = buildMsisdn(countryKey, phoneDigits);

    setIsLoading(true);
    setErrorStatus(null);
    setErrorMessage('');

    const res = await requestOtpCode({
      msisdn,
      mode: authMode,
      fullName: fullName.trim(),
    });

    setIsLoading(false);

    if (res.ok) {
      const ttl = Number(res.data?.ttl_seconds ?? res.data?.expires_in ?? OTP_WINDOW_SECONDS);
      setOtpDigits(['', '', '', '', '', '']);
      setOtpCountdown(Number.isFinite(ttl) && ttl > 0 ? ttl : OTP_WINDOW_SECONDS);
      setStep('otp');
      return;
    }

    if (res.status === 429) {
      setRateLimitSeconds(res.retryAfter || 30);
      setErrorStatus(429);
      setErrorMessage(res.error || 'Слишком много запросов. Подождите перед повтором.');
      return;
    }

    if (res.status === 423) {
      setLockoutSeconds(res.retryAfter || DEFAULT_LOCKOUT_SECONDS);
      setErrorStatus(423);
      setErrorMessage(res.error || 'Номер временно заблокирован.');
      return;
    }

    setErrorStatus(res.status);
    setErrorMessage(res.error);
  };

  const handleVerifyCode = async () => {
    if (isLoading || lockoutSeconds > 0 || otpCountdown <= 0) return;
    const msisdn = buildMsisdn(countryKey, phoneDigits);
    const code = otpDigits.join('');

    setIsLoading(true);
    setErrorStatus(null);
    setErrorMessage('');

    const res = await verifyOtpCode({ msisdn, code });
    setIsLoading(false);

    if (res.ok) {
      setAuthToken(res.data?.access_token ?? res.data?.token ?? null);
      setSessionMsisdn(msisdn);
      setStep('success');
      return;
    }

    if (res.status === 401) {
      setErrorStatus(401);
      setErrorMessage(res.error || 'Неверный код подтверждения.');
      setShakeKey((k) => k + 1);
      return;
    }

    if (res.status === 410) {
      setOtpCountdown(0);
      setErrorStatus(410);
      setErrorMessage(res.error || 'Срок действия кода истёк. Запросите новый.');
      return;
    }

    if (res.status === 423) {
      setLockoutSeconds(res.retryAfter || DEFAULT_LOCKOUT_SECONDS);
      setErrorStatus(423);
      setErrorMessage(res.error || 'Номер заблокирован.');
      return;
    }

    if (res.status === 429) {
      setRateLimitSeconds(res.retryAfter || 30);
      setErrorStatus(429);
      setErrorMessage(res.error || 'Превышен лимит попыток.');
      return;
    }

    setErrorStatus(res.status);
    setErrorMessage(res.error);
  };

  const handleResetSession = () => {
    setStep('phone');
    setOtpDigits(['', '', '', '', '', '']);
    setOtpCountdown(OTP_WINDOW_SECONDS);
    setRateLimitSeconds(0);
    setLockoutSeconds(0);
    setErrorStatus(null);
    setErrorMessage('');
    setAuthToken(null);
    setSessionMsisdn(null);
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
                PulseConnect
              </h1>
              <p className="text-xs text-zinc-400">
                Клиентский сайт регистрации и отправки сообщений
              </p>
            </div>
          </div>

          {/* External API Base URL Setting */}
          <div className="flex items-center gap-2">
            {isEditingApi ? (
              <form onSubmit={handleSaveApiUrl} className="flex items-center gap-2">
                <input
                  type="url"
                  value={apiDraft}
                  onChange={(e) => setApiDraft(e.target.value)}
                  placeholder="http://localhost:8000"
                  className="w-56 rounded-xl border border-fuchsia-400/40 bg-black/50 px-3 py-1.5 font-mono text-xs text-white focus:outline-none"
                />
                <button
                  type="submit"
                  className="inline-flex items-center gap-1 rounded-xl border border-emerald-500/40 bg-emerald-500/15 px-3 py-1.5 text-xs font-medium text-emerald-300 hover:bg-emerald-500/25 cursor-pointer"
                >
                  <Check className="h-3.5 w-3.5" />
                  Сохранить
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
                title="Настроить URL внешнего API-сервера"
              >
                <Globe className="h-3.5 w-3.5 text-fuchsia-300" />
                <span className="font-mono">{apiBaseUrl}</span>
                <Settings2 className="h-3.5 w-3.5 text-zinc-400" />
              </button>
            )}
          </div>
        </div>
      </header>

      {/* Two-Column Client Workspace */}
      <main className="mx-auto flex w-full max-w-7xl flex-1 items-center px-4 py-6 sm:px-6 lg:px-8">
        <div className="grid w-full grid-cols-1 gap-6 lg:grid-cols-12 lg:items-stretch">
          <div className="lg:col-span-5 flex flex-col">
            <RegistrationPanel
              authMode={authMode}
              onChangeAuthMode={setAuthMode}
              fullName={fullName}
              onChangeFullName={setFullName}
              countryKey={countryKey}
              onChangeCountry={(next) => {
                setCountryKey(next);
                setPhoneDigits('');
              }}
              phoneDigits={phoneDigits}
              onChangePhoneDigits={setPhoneDigits}
              step={step}
              otpDigits={otpDigits}
              onChangeOtpDigits={setOtpDigits}
              otpCountdown={otpCountdown}
              rateLimitSeconds={rateLimitSeconds}
              lockoutSeconds={lockoutSeconds}
              errorStatus={errorStatus}
              errorMessage={errorMessage}
              shakeKey={shakeKey}
              isLoading={isLoading}
              sessionMsisdn={sessionMsisdn}
              onRequestCode={handleRequestCode}
              onVerifyCode={handleVerifyCode}
              onBackToPhone={() => {
                setStep('phone');
                setErrorStatus(null);
                setErrorMessage('');
              }}
              onResetSession={handleResetSession}
            />
          </div>

          <div className="lg:col-span-7 flex flex-col">
            <MessageSenderPanel
              authToken={authToken}
              sessionMsisdn={sessionMsisdn}
            />
          </div>
        </div>
      </main>
    </div>
  );
}
