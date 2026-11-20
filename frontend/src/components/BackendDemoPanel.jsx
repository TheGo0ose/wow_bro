import {
  CheckCircle2,
  ExternalLink,
  ShieldCheck,
  Terminal,
  Trash2,
  UserCheck,
  Users,
} from 'lucide-react';
import { formatDisplayMsisdn } from '../utils/phoneMask';

function getStatusClasses(status) {
  if (status >= 200 && status < 300) {
    return 'border-emerald-500/35 bg-emerald-500/15 text-emerald-300';
  }
  if (status === 429) {
    return 'border-amber-400/35 bg-amber-400/15 text-amber-200';
  }
  if (status === 423) {
    return 'border-red-500/45 bg-red-500/20 text-red-300';
  }
  if (status === 410) {
    return 'border-rose-400/35 bg-rose-500/15 text-rose-200';
  }
  return 'border-orange-400/35 bg-orange-500/15 text-orange-200';
}

export default function BackendDemoPanel({
  apiBaseUrl,
  accounts,
  activeUser,
  httpLogs,
  onClearLogs,
  onDeleteAccount,
  onSelectAccountForLogin,
}) {
  return (
    <section className="flex flex-col justify-between gap-5 rounded-3xl border border-white/10 bg-gradient-to-br from-[#231b2c]/90 via-[#120e18]/95 to-[#060508]/98 p-6 sm:p-8 shadow-[0_25px_70px_-20px_rgba(0,0,0,0.85)] backdrop-blur-xl">
      {/* Блок 1: Зарегистрированные аккаунты */}
      <div>
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2.5">
            <div className="flex h-9 w-9 items-center justify-center rounded-xl border border-violet-400/25 bg-violet-500/10 text-violet-300">
              <Users className="h-4.5 w-4.5" />
            </div>
            <div>
              <span className="text-[11px] font-medium uppercase tracking-widest text-zinc-400">
                Реальные аккаунты (localStorage)
              </span>
              <h2 className="text-base font-semibold text-white leading-tight">
                Зарегистрированные пользователи ({accounts.length})
              </h2>
            </div>
          </div>

          <a
            href={apiBaseUrl}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1.5 rounded-xl border border-fuchsia-400/30 bg-fuchsia-500/10 px-3 py-1.5 text-xs font-medium text-fuchsia-200 transition hover:bg-fuchsia-500/20"
          >
            <ShieldCheck className="h-3.5 w-3.5" />
            <span>SOC-дашборд бэкенда</span>
            <ExternalLink className="h-3 w-3" />
          </a>
        </div>

        {accounts.length === 0 ? (
          <div className="rounded-2xl border border-dashed border-white/10 bg-black/25 px-4 py-5 text-center text-xs text-zinc-400">
            Пока нет зарегистрированных аккаунтов. Пройдите регистрацию слева и подтвердите SMS-код из{' '}
            <code className="rounded bg-white/10 px-1.5 py-0.5 font-mono text-fuchsia-200">
              outbox/sms.jsonl
            </code>
            .
          </div>
        ) : (
          <div className="terminal-scroll max-h-44 space-y-2 overflow-y-auto pr-1">
            {accounts.map((acc) => {
              const isCurrent = activeUser?.id === acc.id;
              return (
                <div
                  key={acc.id}
                  className={`flex items-center justify-between gap-3 rounded-2xl border px-3.5 py-2.5 text-xs transition ${
                    isCurrent
                      ? 'border-emerald-500/40 bg-emerald-500/10'
                      : 'border-white/10 bg-black/30 hover:border-white/20'
                  }`}
                >
                  <div className="flex items-center gap-3 min-w-0">
                    <div
                      className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-full font-bold uppercase ${
                        isCurrent
                          ? 'bg-emerald-500/25 text-emerald-300'
                          : 'bg-fuchsia-500/20 text-fuchsia-200'
                      }`}
                    >
                      {acc.username.slice(0, 2)}
                    </div>
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="truncate font-semibold text-white">
                          {acc.username}
                        </span>
                        <span className="inline-flex items-center gap-1 rounded-full bg-emerald-500/15 px-1.5 py-0.5 text-[10px] text-emerald-300">
                          <CheckCircle2 className="h-2.5 w-2.5" />
                          OTP Verified
                        </span>
                        {isCurrent && (
                          <span className="rounded-full bg-fuchsia-500/20 px-1.5 py-0.5 text-[10px] text-fuchsia-200">
                            Активен
                          </span>
                        )}
                      </div>
                      <div className="mt-0.5 font-mono text-[11px] text-zinc-400">
                        {formatDisplayMsisdn(acc.msisdn)} ({acc.msisdn})
                      </div>
                    </div>
                  </div>

                  <div className="flex items-center gap-1.5 shrink-0">
                    {!isCurrent && (
                      <button
                        type="button"
                        onClick={() => onSelectAccountForLogin(acc)}
                        className="inline-flex items-center gap-1 rounded-lg border border-white/10 bg-white/5 px-2.5 py-1 text-[11px] text-zinc-300 transition hover:bg-white/10 hover:text-white cursor-pointer"
                        title="Перейти к форме входа для этого аккаунта"
                      >
                        <UserCheck className="h-3 w-3" />
                        Войти
                      </button>
                    )}
                    <button
                      type="button"
                      onClick={() => onDeleteAccount(acc.id)}
                      className="rounded-lg border border-white/5 p-1.5 text-zinc-500 transition hover:border-red-500/30 hover:bg-red-500/10 hover:text-red-300 cursor-pointer"
                      title="Удалить локальную запись аккаунта"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Блок 2: Живой инспектор реальных HTTP-запросов к OTP Shield */}
      <div className="flex flex-1 flex-col border-t border-white/10 pt-5">
        <div className="mb-3 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Terminal className="h-4 w-4 text-fuchsia-400" />
            <span className="text-xs font-semibold uppercase tracking-wider text-zinc-300">
              Живой HTTP-трафик к OTP Shield ({httpLogs.length})
            </span>
          </div>
          {httpLogs.length > 0 && (
            <button
              type="button"
              onClick={onClearLogs}
              className="inline-flex items-center gap-1 text-xs text-zinc-500 hover:text-zinc-300 cursor-pointer"
            >
              <Trash2 className="h-3.5 w-3.5" />
              Очистить
            </button>
          )}
        </div>

        <div className="terminal-scroll flex-1 min-h-[220px] max-h-[340px] overflow-y-auto rounded-2xl border border-white/10 bg-[#07050a]/90 p-3.5 font-mono text-[11px] leading-relaxed">
          {httpLogs.length === 0 ? (
            <div className="flex h-full min-h-[180px] flex-col items-center justify-center text-center text-zinc-600">
              <Terminal className="mb-2 h-5 w-5 opacity-50" />
              <p>Здесь отображаются только реальные HTTP-запросы к {apiBaseUrl}</p>
            </div>
          ) : (
            <div className="space-y-2">
              {httpLogs.map((log) => (
                <div
                  key={log.id}
                  className="rounded-xl border border-white/[0.06] bg-white/[0.02] p-2.5 animate-fade-in-soft"
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <span className="text-zinc-500">[{log.time}]</span>
                      <span className="font-semibold text-fuchsia-400">
                        {log.method}
                      </span>
                      <span className="text-sky-300">{log.path}</span>
                    </div>
                    <div className="flex items-center gap-2">
                      <span className="text-zinc-500">{log.latencyMs} ms</span>
                      <span
                        className={`rounded-md border px-1.5 py-0.5 text-[10px] font-semibold ${getStatusClasses(
                          log.status
                        )}`}
                      >
                        {log.status === 0 ? 'NETWORK ERR' : `HTTP ${log.status}`}
                      </span>
                    </div>
                  </div>
                  {log.requestBody && (
                    <div className="mt-1 text-zinc-400 break-all">
                      req: {JSON.stringify(log.requestBody)}
                    </div>
                  )}
                  {log.responsePreview && (
                    <div className="mt-0.5 text-zinc-300 break-all">
                      res: {log.responsePreview}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
