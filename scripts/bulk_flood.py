from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections import Counter

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.table import Table


async def run_bulk_flood(base_url: str, count: int, poll_seconds: float) -> int:
    console = Console()
    console.print(
        Panel.fit(
            f"[bold cyan]OTP-Shield Telecom Priority & Bulk Flood Demo[/]\n"
            f"Target: [white]{base_url}[/] | Bulk Batch: [white]{count} SMS (5 TPS)[/] vs Priority OTP: [white]100 TPS[/]",
            border_style="cyan",
        )
    )

    async with httpx.AsyncClient(base_url=base_url, timeout=15.0) as client:
        before_resp = await client.get("/telecom/status")
        before_resp.raise_for_status()
        before = before_resp.json()
        before_bal = before.get("balances", {})
        storage_engine = before.get("storage_engine", "Unknown")
        console.print(f"[bold]Storage Engine:[/] [cyan]{storage_engine}[/]")

        # 1. Fire promotional bulk SMS (bulk_mkt, 5 TPS)
        t_bulk_start = time.perf_counter()
        bulk_resp = await client.post(
            "/telecom/bulk",
            json={
                "count": count,
                "text": "Akciya! Skidki 20% tolko segodnya! Podrobnosti v prilozhenii.",
            },
        )
        bulk_ms = (time.perf_counter() - t_bulk_start) * 1000.0

        # 2. Immediately fire a high-priority OTP via POST /otp/send
        priority_msisdn = f"996777{int(time.time()) % 100000:05d}9"
        t_otp_start = time.perf_counter()
        otp_resp = await client.post("/otp/send", json={"msisdn": priority_msisdn})
        otp_ms = (time.perf_counter() - t_otp_start) * 1000.0

        console.print(
            f"[bold]POST /telecom/bulk ({count} SMS):[/] HTTP {bulk_resp.status_code} "
            f"in {bulk_ms:.1f} ms"
        )
        if bulk_resp.status_code == 503:
            console.print(
                "[bold yellow]Telecom operator offline (HTTP 503). "
                "Start with: docker compose --profile telecom up -d[/]"
            )
        elif bulk_resp.status_code == 402:
            console.print(
                "[bold red]Insufficient balance for bulk_mkt (HTTP 402)[/] "
                f"detail={bulk_resp.json()}"
            )
        console.print(
            f"[bold]POST /otp/send ({priority_msisdn}):[/] HTTP {otp_resp.status_code} in "
            f"[bold green]{otp_ms:.1f} ms[/] (unblocked by bulk queue)"
        )

        # 3. Poll GET /telecom/status to observe SUBMITTED -> DELIVRD progression
        latest_status = before
        gw = before.get("gateway", {})
        gw_online = bool(
            gw.get("online")
            or gw.get("telecom_online")
            or gw.get("bound") == "BOUND_TRX"
            or gw.get("status") in ("BOUND_TRX", "online")
        )

        deadline = time.monotonic() + poll_seconds
        polls = 0
        while time.monotonic() < deadline:
            await asyncio.sleep(1.0)
            polls += 1
            st_resp = await client.get("/telecom/status")
            st_resp.raise_for_status()
            latest_status = st_resp.json()
            cdrs = latest_status.get("cdrs", [])
            counts = Counter(str(c.get("status", "UNKNOWN")) for c in cdrs)
            console.print(
                f"  [dim]Poll #{polls}:[/] CDRs={len(cdrs)} | statuses={dict(counts)} | "
                f"balances={latest_status.get('balances', {})}"
            )
            if not gw_online and polls >= 2:
                break
            if cdrs and counts.get("SUBMITTED", 0) == 0:
                break

    after_bal = latest_status.get("balances", {})
    bal_table = Table(title="Telecom Account Balances (som)")
    bal_table.add_column("Account", style="bold cyan")
    bal_table.add_column("Rate / TPS")
    bal_table.add_column("Before", justify="right")
    bal_table.add_column("After", justify="right")
    bal_table.add_column("Delta", justify="right", style="yellow")

    for acct, meta in [
        ("otp_svc", "0.45 som/seg | 100 TPS"),
        ("bulk_mkt", "0.30 som/seg | 5 TPS"),
    ]:
        b_val = float(before_bal.get(acct, 0.0))
        a_val = float(after_bal.get(acct, 0.0))
        bal_table.add_row(acct, meta, f"{b_val:.2f}", f"{a_val:.2f}", f"{a_val - b_val:+.2f}")

    console.print(bal_table)

    final_storage = latest_status.get("storage_engine", storage_engine)
    latest_gw = latest_status.get("gateway", {})
    counters = latest_gw.get("counters", latest_gw)
    counter_parts = [
        f"{k}={counters[k]}"
        for k in ("amqp_consumed", "otp_dispatched", "bulk_dispatched")
        if k in counters
    ]
    gw_summary = f" | Counters: {', '.join(counter_parts)}" if counter_parts else ""
    console.print(f"[bold]Storage Engine:[/] [cyan]{final_storage}[/]{gw_summary}")

    cdrs = latest_status.get("cdrs", [])
    cdr_table = Table(title=f"Latest CDR Records (showing up to 15 of {len(cdrs)})")
    cdr_table.add_column("msg_id", style="cyan")
    cdr_table.add_column("msisdn")
    cdr_table.add_column("Operator / HLR")
    cdr_table.add_column("channel")
    cdr_table.add_column("segments", justify="right")
    cdr_table.add_column("cost_som", justify="right")
    cdr_table.add_column("status")

    for c in cdrs[:15]:
        st = str(c.get("status", "SUBMITTED"))
        if st == "DELIVRD":
            st_styled = f"[green]{st}[/]"
        elif st == "UNDELIV":
            st_styled = f"[red]{st}[/]"
        else:
            st_styled = f"[yellow]{st}[/]"
        op = str(c.get("operator") or "-")
        hlr_status = str(c.get("hlr_status", ""))
        op_hlr = f"{op} [red]({hlr_status})[/]" if hlr_status == "ABSENT_SUBSCRIBER" else op
        cdr_table.add_row(
            str(c.get("msg_id", "")),
            str(c.get("msisdn", "")),
            op_hlr,
            str(c.get("channel", "")),
            str(c.get("segments", 1)),
            f"{float(c.get('cost_som', 0.0)):.2f}",
            st_styled,
        )

    console.print(cdr_table)
    return 0 if otp_resp.status_code == 200 and bulk_resp.status_code < 400 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="OTP-Shield bulk flood & priority OTP demo")
    parser.add_argument("--base-url", default="http://localhost:8000", help="OTP API base URL")
    parser.add_argument("--count", type=int, default=50, help="Number of bulk SMS to enqueue")
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=14.0,
        help="Max seconds to poll /telecom/status for DLR updates",
    )
    args = parser.parse_args()
    return asyncio.run(run_bulk_flood(args.base_url, args.count, args.poll_seconds))


if __name__ == "__main__":
    sys.exit(main())
