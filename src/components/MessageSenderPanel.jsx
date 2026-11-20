import { useState } from 'react';
import {
  AlertCircle,
  CheckCircle2,
  Loader2,
  Mail,
  MessageSquare,
  Send,
  Smartphone,
  Trash2,
  WifiOff,
} from 'lucide-react';
import { sendMessage } from '../api/client';
import { formatLogTime } from '../utils/phoneMask';

const CHANNELS = [
  { id: 'sms', label: 'SMS', icon: Smartphone },
  { id: 'push', label: 'Push / In-App', icon: MessageSquare },
  { id: 'email', label: 'Email', icon: Mail },
];

export default function MessageSenderPanel({ authToken, sessionMsisdn }) {
  const [channel, setChannel] = useState('sms');
  const [recipient, setRecipient] = useState('+79991234567');
  const [text, setText] = useState('');
  const [isSending, setIsSending] = useState(false);
  const [feedback, setFeedback] = useState(null);
  const [history, setHistory] = useState([]);

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!recipient.trim() || !text.trim() || isSending) return;

    setIsSending(true);
    setFeedback(null);

    const res = await sendMessage({
      recipient: recipient.trim(),
      text: text.trim(),
      channel,
      token: authToken,
    });

    setIsSending(false);

    const entry = {
      id: `${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
      time: formatLogTime(),
      channel,
      recipient: recipient.trim(),
      text: text.trim(),
      status: res.status,
      ok: res.ok,
      error: res.error,
    };
    setHistory((prev) => [entry, ...prev].slice(0, 20));

    if (res.ok) {
      setFeedback({
        type: 'success',
        message: `Сообщение принято внешним сервером (HTTP ${res.status}).`,
      });
      setText('');
    } else {
      setFeedback({
        type: res.status === 0 ? 'network' : 'error',
        message: res.error || `Ошибка отправки (HTTP ${res.status})`,
      });
    }
  };

  return (
    <section className="flex flex-col justify-between rounded-3xl border border-white/10 bg-gradient-to-br from-[#231b2c]/90 via-[#120e18]/95 to-[#060508]/98 p-6 sm:p-8 shadow-[0_25px_70px_-20px_rgba(0,0,0,0.85)] backdrop-blur-xl">
      <div>
        {/* Header */}
        <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2.5">
            <div className="flex h-9 w-9 items-center justify-center rounded-xl border border-violet-400/25 bg-violet-500/10 text-violet-300">
              <MessageSquare className="h-4.5 w-4.5" />
            </div>
            <div>
              <span className="text-[11px] font-medium uppercase tracking-widest text-zinc-400">
                Messaging Client
              </span>
              <h2 className="text-base font-semibold text-white leading-tight">
                Отправка сообщений
              </h2>
            </div>
          </div>

          {sessionMsisdn && (
            <span className="rounded-full border border-emerald-500/30 bg-emerald-500/10 px-3 py-1 font-mono text-[11px] text-emerald-300">
              Авторизован: {sessionMsisdn}
            </span>
          )}
        </div>

        {/* Compose Form */}
        <form onSubmit={handleSubmit} className="space-y-4">
          {/* Channel Selector */}
          <div>
            <span className="mb-2 block text-xs font-medium uppercase tracking-wider text-zinc-400">
              Канал доставки
            </span>
            <div className="grid grid-cols-3 gap-2">
              {CHANNELS.map((item) => {
                const Icon = item.icon;
                const active = channel === item.id;
                return (
                  <button
                    key={item.id}
                    type="button"
                    onClick={() => setChannel(item.id)}
                    className={`flex items-center justify-center gap-2 rounded-xl border px-3 py-2.5 text-xs font-medium transition cursor-pointer ${
                      active
                        ? 'border-fuchsia-400/50 bg-fuchsia-500/20 text-fuchsia-200'
                        : 'border-white/10 bg-black/30 text-zinc-400 hover:text-zinc-200 hover:border-white/20'
                    }`}
                  >
                    <Icon className="h-3.5 w-3.5" />
                    <span>{item.label}</span>
                  </button>
                );
              })}
            </div>
          </div>

          {/* Recipient */}
          <div>
            <label
              htmlFor="msg-recipient"
              className="mb-2 block text-xs font-medium uppercase tracking-wider text-zinc-400"
            >
              {channel === 'email' ? 'Email получателя' : 'Получатель (номер или ID)'}
            </label>
            <input
              id="msg-recipient"
              type="text"
              value={recipient}
              onChange={(e) => setRecipient(e.target.value)}
              placeholder={
                channel === 'email' ? 'user@example.com' : '+79991234567'
              }
              className="w-full rounded-2xl border border-white/15 bg-black/40 px-4 py-3 font-mono text-sm text-white placeholder:text-zinc-600 focus:border-fuchsia-400/70 focus:outline-none focus:ring-2 focus:ring-fuchsia-500/20"
            />
          </div>

          {/* Message Body */}
          <div>
            <div className="mb-2 flex items-center justify-between">
              <label
                htmlFor="msg-body"
                className="text-xs font-medium uppercase tracking-wider text-zinc-400"
              >
                Текст сообщения
              </label>
              <span className="font-mono text-[11px] text-zinc-500">
                {text.length} / 500
              </span>
            </div>
            <textarea
              id="msg-body"
              rows={4}
              maxLength={500}
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder="Введите текст сообщения для отправки через внешний API..."
              className="w-full resize-none rounded-2xl border border-white/15 bg-black/40 px-4 py-3 text-sm text-white placeholder:text-zinc-600 focus:border-fuchsia-400/70 focus:outline-none focus:ring-2 focus:ring-fuchsia-500/20"
            />
          </div>

          {feedback && (
            <div
              className={`flex items-start gap-2.5 rounded-xl border px-3.5 py-2.5 text-xs animate-fade-in-soft ${
                feedback.type === 'success'
                  ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-200'
                  : 'border-red-500/40 bg-red-500/10 text-red-200'
              }`}
            >
              {feedback.type === 'success' ? (
                <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-emerald-400" />
              ) : feedback.type === 'network' ? (
                <WifiOff className="mt-0.5 h-4 w-4 shrink-0 text-red-400" />
              ) : (
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-red-400" />
              )}
              <span>{feedback.message}</span>
            </div>
          )}

          <button
            type="submit"
            disabled={!recipient.trim() || !text.trim() || isSending}
            className={`flex w-full items-center justify-center gap-2 rounded-2xl px-5 py-3.5 text-sm font-semibold transition-all duration-200 ${
              !recipient.trim() || !text.trim() || isSending
                ? 'bg-zinc-800/80 text-zinc-500 border border-white/5 cursor-not-allowed'
                : 'bg-gradient-to-r from-violet-600 via-purple-600 to-fuchsia-600 text-white shadow-lg shadow-purple-900/40 hover:brightness-110 active:scale-[0.99] cursor-pointer'
            }`}
          >
            {isSending ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Send className="h-4 w-4" />
            )}
            <span>{isSending ? 'Отправка...' : 'Отправить сообщение'}</span>
          </button>
        </form>
      </div>

      {/* Outgoing Requests History */}
      <div className="mt-6 border-t border-white/10 pt-5">
        <div className="mb-3 flex items-center justify-between">
          <span className="text-xs font-semibold uppercase tracking-wider text-zinc-400">
            История отправок ({history.length})
          </span>
          {history.length > 0 && (
            <button
              type="button"
              onClick={() => setHistory([])}
              className="inline-flex items-center gap-1 text-xs text-zinc-500 hover:text-zinc-300 cursor-pointer"
            >
              <Trash2 className="h-3.5 w-3.5" />
              Очистить
            </button>
          )}
        </div>

        {history.length === 0 ? (
          <div className="rounded-2xl border border-white/5 bg-black/25 px-4 py-5 text-center text-xs text-zinc-500">
            Отправленные через внешний API сообщения появятся здесь.
          </div>
        ) : (
          <div className="terminal-scroll max-h-44 space-y-2 overflow-y-auto pr-1">
            {history.map((item) => (
              <div
                key={item.id}
                className="rounded-xl border border-white/10 bg-black/30 px-3.5 py-2.5 text-xs animate-fade-in-soft"
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="font-mono text-zinc-300">
                    [{item.time}] {item.channel.toUpperCase()} → {item.recipient}
                  </span>
                  <span
                    className={`rounded-md px-1.5 py-0.5 font-mono text-[10px] font-semibold ${
                      item.ok
                        ? 'bg-emerald-500/15 text-emerald-300'
                        : 'bg-red-500/15 text-red-300'
                    }`}
                  >
                    {item.status === 0 ? 'NETWORK ERR' : `HTTP ${item.status}`}
                  </span>
                </div>
                <p className="mt-1 truncate text-zinc-400">{item.text}</p>
              </div>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}
