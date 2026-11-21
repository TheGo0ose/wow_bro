from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.table import Table


@dataclass
class AttackResult:
    name: str
    target: str
    expected: str
    observed: str
    breaches: int
    blocked_or_stopped: bool

    @property
    def status_label(self) -> str:
        if self.breaches == 0 and self.blocked_or_stopped:
            return "[bold green]DEFENDED (0 breaches)[/]"
        return f"[bold red]FAILED (breaches={self.breaches})[/]"


def load_identities(path: Path) -> tuple[list[str], list[str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    numbers = [str(n) for n in data.get("numbers", [])]
    client_ips = [str(ip) for ip in data.get("client_ips", [])]
    if len(numbers) < 50:
        raise ValueError(f"Expected at least 50 numbers in {path}, got {len(numbers)}")
    if not client_ips:
        client_ips = ["192.0.2.10", "198.51.100.20"]
    return numbers, client_ips


def read_latest_code(outbox_path: Path, msisdn: str, timeout_s: float = 3.0) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if outbox_path.exists():
            lines = outbox_path.read_text(encoding="utf-8").splitlines()
            for raw in reversed(lines):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if str(row.get("msisdn")) == msisdn and row.get("code"):
                    return str(row["code"])
        time.sleep(0.05)
    raise RuntimeError(f"Timed out waiting for OTP delivery for {msisdn} in {outbox_path}")


async def run_attack_1_bruteforce(
    client: httpx.AsyncClient, msisdn: str, client_ip: str
) -> AttackResult:
    """Attack 1: Brute-force single MSISDN -> 4x 401, then 423 lockout on verify & send."""
    send_resp = await client.post(
        "/otp/send",
        json={"msisdn": msisdn},
        headers={"X-Forwarded-For": client_ip},
    )
    if send_resp.status_code != 200:
        return AttackResult(
            name="1. Brute-force Single MSISDN",
            target=f"{msisdn} ({client_ip})",
            expected="4x 401, 6x 423 + send=423",
            observed=f"initial send={send_resp.status_code}",
            breaches=0,
            blocked_or_stopped=False,
        )

    statuses: list[int] = []
    for i in range(10):
        guess = f"{(990000 + i):06d}"
        resp = await client.post(
            "/otp/verify",
            json={"msisdn": msisdn, "code": guess},
            headers={"X-Forwarded-For": client_ip},
        )
        statuses.append(resp.status_code)

    resend_resp = await client.post(
        "/otp/send",
        json={"msisdn": msisdn},
        headers={"X-Forwarded-For": client_ip},
    )

    breaches = sum(1 for s in statuses if s == 200)
    first_four_ok = statuses[:4] == [401, 401, 401, 401]
    rest_locked = all(s == 423 for s in statuses[4:])
    resend_locked = resend_resp.status_code == 423

    counts = Counter(statuses)
    observed = f"verify={dict(sorted(counts.items()))}, resend={resend_resp.status_code}"
    return AttackResult(
        name="1. Brute-force Single MSISDN",
        target=f"{msisdn} ({client_ip})",
        expected="4x 401, 6x 423, resend=423",
        observed=observed,
        breaches=breaches,
        blocked_or_stopped=(first_four_ok and rest_locked and resend_locked),
    )


async def run_attack_2_ip_spray(
    client: httpx.AsyncClient, numbers: list[str], client_ip: str
) -> AttackResult:
    """Attack 2: Distributed IP spray across 50 numbers -> first 20x 401, 21..50x 429."""
    statuses: list[int] = []
    for idx, msisdn in enumerate(numbers[:50]):
        resp = await client.post(
            "/otp/verify",
            json={"msisdn": msisdn, "code": f"{(100000 + idx):06d}"},
            headers={"X-Forwarded-For": client_ip},
        )
        statuses.append(resp.status_code)

    breaches = sum(1 for s in statuses if s == 200)
    first_20_ok = all(s == 401 for s in statuses[:20])
    rest_429 = all(s == 429 for s in statuses[20:])
    counts = Counter(statuses)
    return AttackResult(
        name="2. Distributed IP Spray (50 MSISDNs)",
        target=f"50 numbers via {client_ip}",
        expected="20x 401, 30x 429",
        observed=f"verify={dict(sorted(counts.items()))}",
        breaches=breaches,
        blocked_or_stopped=(first_20_ok and rest_429),
    )


async def run_attack_3_race_replay(
    client: httpx.AsyncClient, msisdn: str, outbox_path: Path
) -> AttackResult:
    """Attack 3: Race condition replay -> 30 concurrent valid verifies -> strictly 1x 200, 29x 401."""
    send_resp = await client.post(
        "/otp/send",
        json={"msisdn": msisdn},
        headers={"X-Forwarded-For": "203.0.113.1"},
    )
    if send_resp.status_code != 200:
        return AttackResult(
            name="3. Race Condition Replay (30x parallel)",
            target=msisdn,
            expected="1x 200, 29x 401",
            observed=f"initial send={send_resp.status_code}",
            breaches=0,
            blocked_or_stopped=False,
        )

    code = read_latest_code(outbox_path, msisdn)

    async def fire(idx: int) -> int:
        # Distinct documentation IPs so IP rate limit (20/min) doesn't mask concurrency check
        resp = await client.post(
            "/otp/verify",
            json={"msisdn": msisdn, "code": code},
            headers={"X-Forwarded-For": f"203.0.113.{10 + idx}"},
        )
        return resp.status_code

    statuses = await asyncio.gather(*(fire(i) for i in range(30)))
    counts = Counter(statuses)
    ok_count = counts.get(200, 0)
    fail_count = counts.get(401, 0)
    # 1 legitimate verify (200) is expected; >1 would be a race-condition breach
    extra_breaches = max(0, ok_count - 1)
    return AttackResult(
        name="3. Race Condition Replay (30x parallel)",
        target=f"{msisdn} (30 workers)",
        expected="1x 200, 29x 401",
        observed=f"verify={dict(sorted(counts.items()))}",
        breaches=extra_breaches,
        blocked_or_stopped=(ok_count == 1 and fail_count == 29),
    )


def _pick_wrong_code(seed: int, forbidden: str | set[str]) -> str:
    """Generate a deterministic 6-digit guess that does not match any forbidden OTP."""
    blocked = {forbidden} if isinstance(forbidden, str) else forbidden
    candidate = seed % 1_000_000
    while f"{candidate:06d}" in blocked:
        candidate = (candidate + 1) % 1_000_000
    return f"{candidate:06d}"


async def run_attack_4_jury_1000(
    client: httpx.AsyncClient,
    numbers: list[str],
    outbox_path: Path,
    suffix: int,
    attempts: int = 1000,
) -> AttackResult:
    """Attack 4: Jury 1000-attempt multi-wave simulation (IP rotation, 50-MSISDN spray, botnet).

    Wave 4A (300): Single MSISDN brute-forced with 300 rotated IPs; final guess uses real code.
    Wave 4B (350): Single attacker IP spraying 50 MSISDNs (7 passes).
    Wave 4C (350): 35-IP botnet concurrently attacking 10 active targets (35 req/target).
    """
    wave_a_n = max(10, (attempts * 30) // 100)
    wave_b_n = max(50, (attempts * 35) // 100)
    wave_c_n = max(50, attempts - wave_a_n - wave_b_n)

    # Wave 4A: Single-MSISDN IP-rotation brute-force
    wave4a_msisdn = f"996779{suffix:05d}0"
    send_4a = await client.post(
        "/otp/send",
        json={"msisdn": wave4a_msisdn},
        headers={"X-Forwarded-For": "10.200.0.1"},
    )
    if send_4a.status_code != 200:
        return AttackResult(
            name=f"4. Jury {attempts}-Attempt Multi-Wave Attack",
            target=f"{attempts} requests (3 waves)",
            expected="0x 200 (0 breaches), 0x 500",
            observed=f"Wave 4A send failed: {send_4a.status_code}",
            breaches=0,
            blocked_or_stopped=False,
        )
    real_code_4a = read_latest_code(outbox_path, wave4a_msisdn)

    statuses_4a: list[int] = []
    sem = asyncio.Semaphore(40)

    async def fire_verify(msisdn: str, code: str, ip: str) -> int:
        async with sem:
            resp = await client.post(
                "/otp/verify",
                json={"msisdn": msisdn, "code": code},
                headers={"X-Forwarded-For": ip},
            )
            return resp.status_code

    for i in range(wave_a_n):
        ip = f"10.200.{(i // 250) % 250}.{(i % 250) + 1}"
        guess = real_code_4a if i == wave_a_n - 1 else _pick_wrong_code(100000 + i, real_code_4a)
        statuses_4a.append(await fire_verify(wave4a_msisdn, guess, ip))

    stats_after_4a = await client.get("/security/stats", params={"limit": 200})
    leaked_codes = False
    if stats_after_4a.status_code == 200:
        leaked_codes = real_code_4a in json.dumps(stats_after_4a.json().get("events", []))
    else:
        leaked_codes = True

    # Wave 4B: Single-IP high-volume 50-MSISDN spray
    spray_attacker_ip = "198.51.100.250"
    statuses_4b: list[int] = []
    for i in range(wave_b_n):
        msisdn = numbers[i % 50]
        guess = f"{(200000 + i) % 1_000_000:06d}"
        statuses_4b.append(await fire_verify(msisdn, guess, spray_attacker_ip))

    # Wave 4C: Distributed botnet concurrent attack across 10 active targets (35 IPs)
    wave4c_targets = [f"996779{suffix:05d}{k:02d}" for k in range(1, 11)]
    wave4c_codes: list[str] = []
    for target_msisdn in wave4c_targets:
        send_resp = await client.post(
            "/otp/send",
            json={"msisdn": target_msisdn},
            headers={"X-Forwarded-For": "10.201.0.254"},
        )
        if send_resp.status_code != 200:
            return AttackResult(
                name=f"4. Jury {attempts}-Attempt Multi-Wave Attack",
                target=f"{attempts} requests (3 waves)",
                expected="0x 200 (0 breaches), 0x 500",
                observed=f"Wave 4C send failed for {target_msisdn}: {send_resp.status_code}",
                breaches=0,
                blocked_or_stopped=False,
            )
        wave4c_codes.append(read_latest_code(outbox_path, target_msisdn))

    wave4c_tasks = []
    for i in range(wave_c_n):
        target_idx = i % len(wave4c_targets)
        target_msisdn = wave4c_targets[target_idx]
        target_real_code = wave4c_codes[target_idx]
        bot_ip = f"10.201.1.{(i % 35) + 1}"
        guess = _pick_wrong_code(300000 + i, target_real_code)
        wave4c_tasks.append(fire_verify(target_msisdn, guess, bot_ip))

    statuses_4c = list(await asyncio.gather(*wave4c_tasks))

    stats_after_4c = await client.get("/security/stats", params={"limit": 200})
    if stats_after_4c.status_code == 200:
        events_json = json.dumps(stats_after_4c.json().get("events", []))
        if real_code_4a in events_json or any(c in events_json for c in wave4c_codes):
            leaked_codes = True
    else:
        leaked_codes = True

    all_statuses = statuses_4a + statuses_4b + statuses_4c
    total_counts = Counter(all_statuses)
    breaches = total_counts.get(200, 0)
    server_errors = total_counts.get(500, 0)

    c4a = Counter(statuses_4a)
    c4b = Counter(statuses_4b)
    c4c = Counter(statuses_4c)

    wave4a_ok = c4a == Counter({401: 4, 423: wave_a_n - 4})
    wave4b_ok = (
        all(s in (401, 423) for s in statuses_4b[:20])
        and all(s == 429 for s in statuses_4b[20:])
        and c4b.get(200, 0) == 0
    )
    wave4c_ok = c4c == Counter({401: 40, 423: wave_c_n - 40})
    stopped = (
        len(all_statuses) == wave_a_n + wave_b_n + wave_c_n
        and breaches == 0
        and server_errors == 0
        and not leaked_codes
        and wave4a_ok
        and wave4b_ok
        and wave4c_ok
    )

    observed = (
        f"total={len(all_statuses)} {dict(sorted(total_counts.items()))} "
        f"[4A:{dict(sorted(c4a.items()))}, 4B:{dict(sorted(c4b.items()))}, 4C:{dict(sorted(c4c.items()))}, "
        f"audit_leak={leaked_codes}]"
    )
    return AttackResult(
        name=f"4. Jury {wave_a_n + wave_b_n + wave_c_n}-Attempt Multi-Wave Attack",
        target="4A(1x300 rot) + 4B(50x7 1IP) + 4C(10x35 botnet)",
        expected="0x 200 (0 breaches), 0x 500, audit clean",
        observed=observed,
        breaches=breaches,
        blocked_or_stopped=stopped,
    )


async def run_all(
    base_url: str,
    identities_path: Path,
    outbox_path: Path,
    attempts: int = 1000,
) -> int:
    console = Console()
    numbers, client_ips = load_identities(identities_path)

    suffix = int(time.time()) % 100000
    attack1_msisdn = f"996778{suffix:05d}1"
    attack3_msisdn = f"996778{suffix:05d}2"
    spray_ip = client_ips[0]
    bf_ip = client_ips[1] if len(client_ips) > 1 else "198.51.100.20"

    console.print(
        Panel.fit(
            f"[bold cyan]OTP-Shield Penetration & Defense Verification[/]\n"
            f"Target: [white]{base_url}[/] | Identities: [white]{identities_path}[/] | Outbox: [white]{outbox_path}[/]",
            border_style="cyan",
        )
    )

    async with httpx.AsyncClient(base_url=base_url, timeout=15.0) as client:
        r1 = await run_attack_1_bruteforce(client, attack1_msisdn, bf_ip)
        r2 = await run_attack_2_ip_spray(client, numbers, spray_ip)
        r3 = await run_attack_3_race_replay(client, attack3_msisdn, outbox_path)
        r4 = await run_attack_4_jury_1000(client, numbers, outbox_path, suffix, attempts=attempts)

    results = [r1, r2, r3, r4]
    total_breaches = sum(r.breaches for r in results)
    all_defended = all(r.blocked_or_stopped and r.breaches == 0 for r in results)

    table = Table(title="Attack Simulation Summary", show_lines=True)
    table.add_column("Attack Vector", style="bold white")
    table.add_column("Target", style="cyan")
    table.add_column("Expected Defense", style="dim")
    table.add_column("Observed Result", style="white")
    table.add_column("Breaches", justify="right")
    table.add_column("Verdict")

    for r in results:
        table.add_row(
            r.name,
            r.target,
            r.expected,
            r.observed,
            str(r.breaches),
            r.status_label,
        )

    console.print(table)
    console.print(
        f"\n[bold]Total Successful Breaches:[/] "
        f"[{'green' if total_breaches == 0 else 'red'}]{total_breaches}[/] | "
        f"[bold]Overall Defense:[/] "
        f"[{'green' if all_defended else 'red'}]{'ALL DEFENSES PASSED' if all_defended else 'DEFENSE CHECK FAILED'}[/]"
    )

    return 0 if (total_breaches == 0 and all_defended) else 1


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="OTP-Shield live attack verification script")
    parser.add_argument("--base-url", default="http://localhost:8000", help="OTP API base URL")
    parser.add_argument(
        "--identities",
        default=str(repo_root / "test_identities.json"),
        help="Path to test_identities.json",
    )
    parser.add_argument(
        "--outbox",
        default=str(repo_root / "outbox" / "sms.jsonl"),
        help="Path to outbox/sms.jsonl (used only for Attack 3 & Wave 4A/4C audit verification)",
    )
    parser.add_argument(
        "--attempts",
        type=int,
        default=1000,
        help="Number of verify attempts for Attack 4 jury multi-wave simulation (default: 1000)",
    )
    args = parser.parse_args()
    return asyncio.run(
        run_all(
            args.base_url,
            Path(args.identities),
            Path(args.outbox),
            attempts=args.attempts,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
