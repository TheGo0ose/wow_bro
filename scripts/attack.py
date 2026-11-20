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


async def run_all(base_url: str, identities_path: Path, outbox_path: Path) -> int:
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

    results = [r1, r2, r3]
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
        help="Path to outbox/sms.jsonl (used only for Attack 3 replay demo)",
    )
    args = parser.parse_args()
    return asyncio.run(run_all(args.base_url, Path(args.identities), Path(args.outbox)))


if __name__ == "__main__":
    sys.exit(main())
