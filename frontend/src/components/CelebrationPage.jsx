import { useState } from 'react';
import {
  Calendar,
  CheckCircle2,
  Lock,
  Mail,
  PartyPopper,
  RefreshCw,
  ShieldCheck,
  Smartphone,
  Sparkles,
  User,
} from 'lucide-react';
import ConfettiCanvas from './ConfettiCanvas';
import { formatDisplayMsisdn } from '../utils/phoneMask';

export default function CelebrationPage({ user, onRegisterAnother }) {
  const [burstKey, setBurstKey] = useState(1);

  return (
    <div className="relative flex w-full max-w-xl flex-col items-center">
      {/* Анимация конфетти при переходе на страницу поздравления */}
      <ConfettiCanvas burstKey={burstKey} />

      <section className="relative w-full overflow-hidden rounded-3xl border border-emerald-400/35 bg-gradient-to-br from-[#233630]/92 via-[#151821]/96 to-[#08070c]/98 p-7 sm:p-10 text-center shadow-[0_30px_90px_-20px_rgba(16,185,129,0.35)] backdrop-blur-xl animate-fade-in">
        {/* Декоративное свечение */}
        <div className="pointer-events-none absolute -top-24 left-1/2 h-56 w-56 -translate-x-1/2 rounded-full bg-emerald-400/20 blur-3xl" />
        <div className="pointer-events-none absolute -bottom-24 -right-16 h-56 w-56 rounded-full bg-fuchsia-500/15 blur-3xl" />

        <div className="relative z-10 flex flex-col items-center">
          {/* Анимированный значок поздравления */}
          <div className="relative mb-6 flex h-24 w-24 items-center justify-center">
            <span className="absolute inset-0 rounded-full border border-emerald-400/40 animate-ping-slow" />
            <div className="relative flex h-22 w-22 items-center justify-center rounded-full border border-emerald-400/50 bg-gradient-to-br from-emerald-500/25 to-teal-500/15 shadow-[0_0_50px_rgba(16,185,129,0.45)] animate-scale-pop">
              <PartyPopper className="h-11 w-11 text-emerald-300 animate-float" strokeWidth={2} />
            </div>
            <Sparkles className="absolute -right-2 -top-1 h-6 w-6 text-amber-300 animate-pulse" />
            <CheckCircle2 className="absolute -bottom-1 -right-1 h-7 w-7 rounded-full bg-[#0d1414] text-emerald-400" />
          </div>

          <span className="mb-3 inline-flex items-center gap-1.5 rounded-full border border-emerald-400/35 bg-emerald-500/15 px-3.5 py-1 font-mono text-xs font-medium text-emerald-300">
            <ShieldCheck className="h-3.5 w-3.5" />
            OTP SHIELD · РЕГИСТРАЦИЯ ПОДТВЕРЖДЕНА
          </span>

          <h2 className="bg-gradient-to-r from-white via-emerald-200 to-fuchsia-200 bg-clip-text text-3xl sm:text-4xl font-bold tracking-tight text-transparent">
            Поздравляем, {user.username}!
          </h2>

          <p className="mt-3 max-w-md text-sm sm:text-base text-zinc-300 leading-relaxed">
            Ваш аккаунт успешно создан и защищён одноразовым SMS-подтверждением. Добро пожаловать!
          </p>

          {/* Карточка созданного профиля */}
          <div className="mt-7 w-full rounded-2xl border border-white/10 bg-black/40 p-5 text-left text-xs sm:text-sm space-y-3">
            <div className="flex items-center justify-between gap-2">
              <span className="flex items-center gap-2 text-zinc-400">
                <User className="h-4 w-4 text-fuchsia-300" />
                Имя пользователя:
              </span>
              <span className="font-semibold text-white">{user.username}</span>
            </div>

            <div className="flex items-center justify-between gap-2">
              <span className="flex items-center gap-2 text-zinc-400">
                <Smartphone className="h-4 w-4 text-emerald-400" />
                Подтверждённый номер:
              </span>
              <span className="font-mono font-medium text-emerald-300">
                {formatDisplayMsisdn(user.msisdn)}
              </span>
            </div>

            <div className="flex items-center justify-between gap-2">
              <span className="flex items-center gap-2 text-zinc-400">
                <Lock className="h-4 w-4 text-violet-300" />
                Статус безопасности:
              </span>
              <span className="font-mono text-xs text-emerald-300">
                Пароль (SHA-256) + OTP 200 OK
              </span>
            </div>

            {user.email && (
              <div className="flex items-center justify-between gap-2">
                <span className="flex items-center gap-2 text-zinc-400">
                  <Mail className="h-4 w-4 text-sky-300" />
                  Email:
                </span>
                <span className="text-zinc-200">{user.email}</span>
              </div>
            )}

            {user.registeredAt && (
              <div className="flex items-center justify-between gap-2 border-t border-white/10 pt-3">
                <span className="flex items-center gap-2 text-zinc-500">
                  <Calendar className="h-4 w-4" />
                  Время регистрации:
                </span>
                <span className="font-mono text-xs text-zinc-400">
                  {new Date(user.registeredAt).toLocaleTimeString('ru-RU')}
                </span>
              </div>
            )}
          </div>

          {/* Кнопки действий */}
          <div className="mt-7 flex w-full flex-col gap-3 sm:flex-row">
            <button
              type="button"
              onClick={() => setBurstKey((k) => k + 1)}
              className="inline-flex flex-1 items-center justify-center gap-2 rounded-2xl border border-fuchsia-400/35 bg-fuchsia-500/15 px-5 py-3 text-sm font-semibold text-fuchsia-200 transition hover:bg-fuchsia-500/25 active:scale-[0.99] cursor-pointer"
            >
              <Sparkles className="h-4 w-4" />
              Ещё конфетти!
            </button>

            <button
              type="button"
              onClick={onRegisterAnother}
              className="inline-flex flex-1 items-center justify-center gap-2 rounded-2xl border border-white/15 bg-white/5 px-5 py-3 text-sm font-medium text-white transition hover:bg-white/10 hover:border-white/25 cursor-pointer"
            >
              <RefreshCw className="h-4 w-4" />
              Новая регистрация
            </button>
          </div>
        </div>
      </section>
    </div>
  );
}
