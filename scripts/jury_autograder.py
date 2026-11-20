from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

MAX_SCORE = 40


@dataclass
class ScenarioResult:
    """Outcome of a single jury scenario run."""

    scenario_id: str
    title: str
    passed: bool
    elapsed_ms: float
    steps_executed: int
    detail: str

    @property
    def badge(self) -> str:
        return "[bold green]PASS[/]" if self.passed else "[bold red]FAIL[/]"


def resolve_value(value: Any, bindings: dict[str, str]) -> Any:
    """Resolve $c1 and $wrong:c1 template placeholders in scenario payloads."""
    if isinstance(value, str):
        if value.startswith("$wrong:"):
            name = value.removeprefix("$wrong:")
            return f"{(int(bindings[name]) + 1) % 1_000_000:06d}"
        if value.startswith("$"):
            name = value[1:]
            return bindings[name]
    return value


def resolve_body(body: dict[str, Any], bindings: dict[str, str]) -> dict[str, Any]:
    """Resolve all placeholder values in a JSON request body."""
    return {key: resolve_value(val, bindings) for key, val in body.items()}


def capture_last_delivery(outbox_path: Path, msisdn: str, timeout_s: float = 0.5) -> str:
    """Read host outbox JSONL directly and validate schema for the latest msisdn entry."""
    deadline = time.monotonic() + timeout_s
    while True:
        if outbox_path.exists():
            matched_row: dict[str, Any] | None = None
            for raw_line in outbox_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                row = json.loads(line)
                if row.get("msisdn") == msisdn:
                    matched_row = row

            if matched_row is not None:
                ts = matched_row.get("ts")
                code = matched_row.get("code")
                if not isinstance(ts, str) or not ts:
                    raise AssertionError(f"Invalid outbox 'ts' field: {matched_row}")
                if matched_row.get("msisdn") != msisdn:
                    raise AssertionError(f"Outbox msisdn mismatch: {matched_row}")
                if not isinstance(code, str) or len(code) != 6 or not code.isdigit():
                    raise AssertionError(f"Invalid outbox 'code' field: {matched_row}")
                return code

        if time.monotonic() >= deadline:
            break
        time.sleep(0.05)

    raise AssertionError(f"No outbox delivery found for msisdn={msisdn} in {outbox_path}")


def assert_no_code_leaks(stats_data: dict[str, Any], bindings: dict[str, str]) -> None:
    """Verify none of the captured OTP codes appear in /security/stats counters or events."""
    if not bindings:
        return

    counters_json = json.dumps(stats_data.get("counters", {}), ensure_ascii=False)
    events = stats_data.get("events", [])
    for code in bindings.values():
        if code in counters_json:
            raise AssertionError(f"Captured code {code} leaked in security counters")
        for ev in events:
            if "code" in ev:
                raise AssertionError(f"Key 'code' leaked in security event: {ev}")
            sanitized = {
                k: v
                for k, v in ev.items()
                if k not in ("id", "ts", "msisdn", "client_ip")
            }
            if code in json.dumps(sanitized, ensure_ascii=False):
                raise AssertionError(f"Captured OTP code {code} leaked in security event: {ev}")


async def send_step_request(
    client: httpx.AsyncClient,
    req_spec: dict[str, Any],
    bindings: dict[str, str],
) -> httpx.Response:
    """Send a single scenario HTTP request with optional X-Forwarded-For."""
    headers: dict[str, str] = {}
    if "client_ip" in req_spec:
        headers["X-Forwarded-For"] = str(req_spec["client_ip"])

    body = resolve_body(req_spec.get("body", {}), bindings)
    return await client.request(
        method=str(req_spec["method"]),
        url=str(req_spec["path"]),
        json=body,
        headers=headers,
    )


async def run_scenario(
    client: httpx.AsyncClient,
    scenario: dict[str, Any],
    *,
    start_time: str,
    outbox_path: Path,
) -> ScenarioResult:
    """Reset target state and execute one full jury scenario over real HTTP."""
    scenario_id = str(scenario["id"])
    title = str(scenario.get("title", ""))
    steps = scenario.get("steps", [])
    started = time.perf_counter()
    bindings: dict[str, str] = {}

    try:
        reset_resp = await client.post(
            "/_test/reset?flush=true",
            json={
                "flush": True,
                "start_time": start_time,
                "overrides": scenario.get("overrides", {}),
            },
        )
        if reset_resp.status_code != 200:
            raise AssertionError(
                f"/_test/reset failed: {reset_resp.status_code} ({reset_resp.text})"
            )

        for idx, step in enumerate(steps):
            op = step["op"]
            if op == "request":
                resp = await send_step_request(client, step, bindings)
                expected_status = int(step["expected"]["status"])
                if resp.status_code != expected_status:
                    raise AssertionError(
                        f"step {idx} ({step['method']} {step['path']}): "
                        f"expected {expected_status}, got {resp.status_code} ({resp.text})"
                    )
                if (
                    str(step["method"]).upper() == "POST"
                    and str(step["path"]) == "/otp/send"
                    and resp.status_code == 200
                ):
                    payload = resp.json()
                    if isinstance(payload, dict) and "code" in payload:
                        raise AssertionError(
                            f"step {idx}: POST /otp/send leaked 'code' in JSON response"
                        )
            elif op == "capture_delivery":
                bindings[str(step["bind"])] = capture_last_delivery(
                    outbox_path,
                    str(step["msisdn"]),
                )
            elif op == "advance":
                adv_resp = await client.post(
                    "/_test/advance",
                    json={"seconds": float(step["seconds"])},
                )
                if adv_resp.status_code != 200:
                    raise AssertionError(
                        f"step {idx} (/_test/advance): "
                        f"expected 200, got {adv_resp.status_code} ({adv_resp.text})"
                    )
            elif op == "parallel":
                responses = await asyncio.gather(
                    *(send_step_request(client, req, bindings) for req in step["requests"])
                )
                expected = step["expected"]
                if "count" in expected and len(responses) != int(expected["count"]):
                    raise AssertionError(
                        f"step {idx} (parallel): expected {expected['count']} responses, "
                        f"got {len(responses)}"
                    )
                counts = dict(Counter(str(r.status_code) for r in responses))
                if counts != expected["status_counts"]:
                    raise AssertionError(
                        f"step {idx} (parallel): expected {expected['status_counts']}, "
                        f"got {counts}"
                    )
            else:
                raise AssertionError(f"Unsupported scenario op: {op!r}")

        stats_resp = await client.get("/security/stats")
        if stats_resp.status_code != 200:
            raise AssertionError(
                f"GET /security/stats failed: {stats_resp.status_code} ({stats_resp.text})"
            )
        assert_no_code_leaks(stats_resp.json(), bindings)

        elapsed_ms = (time.perf_counter() - started) * 1000
        return ScenarioResult(
            scenario_id=scenario_id,
            title=title,
            passed=True,
            elapsed_ms=elapsed_ms,
            steps_executed=len(steps),
            detail=f"OK ({len(steps)} steps, 0 leaks)",
        )
    except (AssertionError, httpx.HTTPError, json.JSONDecodeError, KeyError, ValueError) as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000
        return ScenarioResult(
            scenario_id=scenario_id,
            title=title,
            passed=False,
            elapsed_ms=elapsed_ms,
            steps_executed=len(steps),
            detail=str(exc),
        )


async def run_autograder(
    base_url: str,
    scenarios_path: Path,
    outbox_path: Path,
    identities_path: Path,
) -> int:
    """Execute all jury scenarios against the live service and print rich report."""
    console = Console()
    identities = json.loads(identities_path.read_text(encoding="utf-8"))
    scenarios: list[dict[str, Any]] = json.loads(scenarios_path.read_text(encoding="utf-8"))
    start_time = str(identities["start_time"])

    console.print(
        Panel.fit(
            f"[bold cyan]OTP-Shield Jury Autograder[/]\n"
            f"Target: [white]{base_url}[/] | Scenarios: [white]{scenarios_path}[/] "
            f"({len(scenarios)}) | Outbox: [white]{outbox_path}[/]",
            border_style="cyan",
        )
    )

    results: list[ScenarioResult] = []
    async with httpx.AsyncClient(base_url=base_url, timeout=15.0) as client:
        try:
            for scenario in scenarios:
                res = await run_scenario(
                    client,
                    scenario,
                    start_time=start_time,
                    outbox_path=outbox_path,
                )
                results.append(res)
        finally:
            await client.post("/_test/reset?flush=true")

    table = Table(title="Jury Autograder Results", show_lines=False)
    table.add_column("ID", style="bold cyan", no_wrap=True)
    table.add_column("Scenario", style="white")
    table.add_column("Result", no_wrap=True)
    table.add_column("Time", justify="right", no_wrap=True)
    table.add_column("Details", style="dim")

    for res in results:
        table.add_row(
            res.scenario_id,
            res.title,
            res.badge,
            f"{res.elapsed_ms:.1f} ms",
            res.detail,
        )

    console.print(table)

    passed_count = sum(1 for r in results if r.passed)
    total_count = len(results)
    score = MAX_SCORE if (total_count > 0 and passed_count == total_count) else (
        int(MAX_SCORE * passed_count / total_count) if total_count > 0 else 0
    )
    total_ms = sum(r.elapsed_ms for r in results)
    score_color = "green" if passed_count == total_count else "red"

    console.print(
        f"\n[bold]Scenarios Passed:[/] [{score_color}]{passed_count} / {total_count}[/] | "
        f"[bold]Total Time:[/] {total_ms:.1f} ms | "
        f"[bold]Autograder Score:[/] [{score_color}]{score} / {MAX_SCORE}[/]"
    )

    return 0 if (total_count > 0 and passed_count == total_count) else 1


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description="External jury autograder harness for OTP-Shield",
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="Target OTP-Shield HTTP base URL",
    )
    parser.add_argument(
        "--scenarios",
        default=str(repo_root / "tests" / "jury_scenarios_full.json"),
        help="Path to jury scenarios JSON",
    )
    parser.add_argument(
        "--outbox",
        default=str(repo_root / "outbox" / "sms.jsonl"),
        help="Path to host outbox/sms.jsonl file",
    )
    parser.add_argument(
        "--identities",
        default=str(repo_root / "test_identities.json"),
        help="Path to test_identities.json",
    )
    args = parser.parse_args()
    return asyncio.run(
        run_autograder(
            base_url=args.base_url,
            scenarios_path=Path(args.scenarios),
            outbox_path=Path(args.outbox),
            identities_path=Path(args.identities),
        )
    )


if __name__ == "__main__":
    sys.exit(main())
