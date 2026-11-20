"""Учебная атака на СВОЙ локальный OTP-сервис. Показывает, что защита срабатывает.

  1. brute   — перебор кода для одного номера (000000, 000001, ...).
  2. spread  — распределённый перебор по 50 номерам с одного IP.
  3. spoof   — то же, но с подменой X-Forwarded-For на каждом запросе.

Атака считается остановленной, если ни одна попытка не получила 200.

    python attack.py                       # все атаки, по 1000 попыток
    python attack.py --mode brute --attempts 300

Запускается только против localhost — внешние сервисы атаковать нельзя.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parent
IDENTITIES = ROOT / "scenarios" / "test_identities.json"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]", "otp"}


@dataclass
class AttackReport:
    name: str
    attempts: int = 0
    statuses: Counter = field(default_factory=Counter)
    guessed: list[str] = field(default_factory=list)
    first_block: dict[int, int] = field(default_factory=dict)   # статус -> номер попытки
    seconds: float = 0.0

    @property
    def stopped(self) -> bool:
        return not self.guessed

    def record(self, status: int, msisdn: str) -> None:
        self.attempts += 1
        self.statuses[status] += 1
        if status in (423, 429) and status not in self.first_block:
            self.first_block[status] = self.attempts
        if status == 200:
            self.guessed.append(msisdn)

    def print(self) -> None:
        verdict = "ОСТАНОВЛЕНА" if self.stopped else f"УСПЕШНА ({len(self.guessed)} угадано)"
        print(f"\n=== {self.name}: {verdict}")
        print(f"    попыток verify: {self.attempts} за {self.seconds:.2f} с")
        print(f"    ответы: {dict(sorted(self.statuses.items()))}")
        names = {423: "блокировка номера (423)", 429: "IP-лимит (429)"}
        for status, n in sorted(self.first_block.items(), key=lambda kv: kv[1]):
            print(f"    {names[status]} сработала на попытке №{n}")


def guess(i: int) -> str:
    return f"{i % 1_000_000:06d}"


def ensure_local(base_url: str) -> None:
    host = urlparse(base_url).hostname or ""
    if host not in LOCAL_HOSTS:
        sys.exit(f"Отказ: {host} не localhost. Атаковать можно только свой локальный сервис.")


def send_codes(client: httpx.Client, numbers: list[str]) -> None:
    for n in numbers:
        client.post("/otp/send", json={"msisdn": n})


def attack_brute(client: httpx.Client, msisdn: str, attempts: int) -> AttackReport:
    rep = AttackReport(f"Перебор кода для {msisdn}")
    send_codes(client, [msisdn])
    t0 = time.perf_counter()
    for i in range(attempts):
        r = client.post("/otp/verify", json={"msisdn": msisdn, "code": guess(i)})
        rep.record(r.status_code, msisdn)
    rep.seconds = time.perf_counter() - t0
    return rep


def attack_spread(client: httpx.Client, numbers: list[str], attempts: int,
                  spoof: bool = False) -> AttackReport:
    title = "Распределённый перебор по %d номерам с одного IP" % len(numbers)
    rep = AttackReport(title + (" + подмена X-Forwarded-For" if spoof else ""))
    send_codes(client, numbers)
    t0 = time.perf_counter()
    for i in range(attempts):
        msisdn = numbers[i % len(numbers)]
        headers = {"X-Forwarded-For": f"203.0.113.{i % 250}"} if spoof else {}
        r = client.post("/otp/verify", json={"msisdn": msisdn, "code": guess(i // len(numbers))},
                        headers=headers)
        rep.record(r.status_code, msisdn)
    rep.seconds = time.perf_counter() - t0
    return rep


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--mode", choices=["all", "brute", "spread", "spoof"], default="all")
    ap.add_argument("--attempts", type=int, default=1000)
    ap.add_argument("--pause", type=float, default=61.0,
                    help="пауза между атаками, чтобы окно IP-лимита (60 с) очистилось")
    args = ap.parse_args()
    ensure_local(args.base_url)

    numbers = json.loads(IDENTITIES.read_text(encoding="utf-8"))["numbers"]
    reports: list[AttackReport] = []
    with httpx.Client(base_url=args.base_url, timeout=10) as client:
        try:
            client.get("/health").raise_for_status()
        except httpx.HTTPError as exc:
            sys.exit(f"Сервис недоступен по {args.base_url}: {exc}")
        plan = []
        if args.mode in ("all", "brute"):
            plan.append(lambda: attack_brute(client, numbers[0], args.attempts))
        if args.mode in ("all", "spread"):
            plan.append(lambda: attack_spread(client, numbers, args.attempts))
        if args.mode in ("all", "spoof"):
            plan.append(lambda: attack_spread(client, numbers, args.attempts, spoof=True))
        for i, run in enumerate(plan):
            if i and args.pause > 0:
                # Все атаки идут с одного IP: ждём, пока окно IP-лимита очистится,
                # иначе следующая атака сразу упрётся в 429 от предыдущей.
                print(f"... пауза {args.pause:.0f} с, пока очищается окно IP-лимита", flush=True)
                time.sleep(args.pause)
            reports.append(run())
            reports[-1].print()

    ok = all(r.stopped for r in reports)
    print("\nИТОГ:", "все атаки остановлены защитой" if ok else "ЗАЩИТА ПРОБИТА")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
