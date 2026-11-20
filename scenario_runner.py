"""Прогон сценариев в формате жюри (scenarios_examples.json) против запущенного сервиса.

Сервис должен быть запущен в тестовом режиме:
    OTP_TEST_MODE=1 OTP_TRUSTED_IP_HEADER=X-Test-Client-IP python -m otp_service

Запуск:
    python scenario_runner.py scenarios/scenarios_examples.json scenarios/extra_scenarios.json

Шаги:
  request          — HTTP-запрос, сверка status
  capture_delivery — последняя строка outbox/sms.jsonl для номера -> переменная
  advance          — перемотка подменяемых часов (POST /__test__/advance)
  parallel         — одновременные запросы, сверка status_counts
Подстановки в body: "$c1" — код из доставки, "$wrong:c1" — заведомо другой код.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from otp_service.delivery import read_last_code

IP_HEADER = "X-Test-Client-IP"
DEFAULT_OUTBOX = Path(__file__).resolve().parent / "outbox" / "sms.jsonl"


@dataclass
class ScenarioResult:
    id: str
    title: str
    passed: bool
    failures: list[str] = field(default_factory=list)


def wrong_code(code: str) -> str:
    return f"{(int(code) + 1) % 1_000_000:06d}"


def substitute(value: Any, bound: dict[str, str]) -> Any:
    if isinstance(value, str) and value.startswith("$"):
        name = value[1:]
        if name.startswith("wrong:"):
            return wrong_code(bound[name[len("wrong:"):]])
        return bound[name]
    if isinstance(value, dict):
        return {k: substitute(v, bound) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, bound) for v in value]
    return value


def _do_request(client: httpx.Client, req: dict[str, Any], bound: dict[str, str]) -> int:
    headers = {}
    if req.get("client_ip"):
        headers[IP_HEADER] = req["client_ip"]
    kwargs: dict[str, Any] = {"headers": headers}
    if "raw_body" in req:                       # для проверки битого JSON
        kwargs["content"] = req["raw_body"]
        headers["Content-Type"] = "application/json"
    else:
        kwargs["json"] = substitute(req.get("body"), bound)
    resp = client.request(req.get("method", "POST"), req["path"], **kwargs)
    return resp.status_code


def run_scenario(base_url: str, outbox: Path, scenario: dict[str, Any]) -> ScenarioResult:
    result = ScenarioResult(scenario["id"], scenario.get("title", ""), True)
    bound: dict[str, str] = {}
    with httpx.Client(base_url=base_url, timeout=10) as client:
        client.post("/__test__/reset").raise_for_status()
        for i, step in enumerate(scenario["steps"], 1):
            op = step["op"]
            if op == "request":
                status = _do_request(client, step, bound)
                exp = step.get("expected", {}).get("status")
                if exp is not None and status != exp:
                    result.failures.append(f"шаг {i} {step['path']}: ожидали {exp}, получили {status}")
            elif op == "capture_delivery":
                bound[step["bind"]] = read_last_code(outbox, step["msisdn"])
            elif op == "advance":
                client.post("/__test__/advance", json={"seconds": step["seconds"]}).raise_for_status()
            elif op == "parallel":
                reqs = step["requests"]
                barrier = threading.Barrier(len(reqs))

                def fire(req: dict[str, Any]) -> int:
                    with httpx.Client(base_url=base_url, timeout=10) as c:
                        barrier.wait()
                        return _do_request(c, req, bound)

                with ThreadPoolExecutor(max_workers=len(reqs)) as pool:
                    statuses = list(pool.map(fire, reqs))
                exp = step.get("expected", {})
                counts = {str(k): v for k, v in Counter(statuses).items()}
                if "count" in exp and len(statuses) != exp["count"]:
                    result.failures.append(f"шаг {i}: ожидали {exp['count']} ответов, получили {len(statuses)}")
                if "status_counts" in exp and counts != exp["status_counts"]:
                    result.failures.append(f"шаг {i}: ожидали {exp['status_counts']}, получили {counts}")
            else:
                result.failures.append(f"шаг {i}: неизвестная операция {op}")
    result.passed = not result.failures
    return result


def load_scenarios(paths: list[Path]) -> list[dict[str, Any]]:
    scenarios: list[dict[str, Any]] = []
    for p in paths:
        scenarios.extend(json.loads(Path(p).read_text(encoding="utf-8")))
    return scenarios


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--base-url", default=os.environ.get("OTP_BASE_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--outbox", type=Path, default=Path(os.environ.get("OTP_OUTBOX", DEFAULT_OUTBOX)))
    args = ap.parse_args()

    results = [run_scenario(args.base_url, args.outbox, sc) for sc in load_scenarios(args.files)]
    for r in results:
        print(f"{'PASS' if r.passed else 'FAIL'}  {r.id}  {r.title}")
        for f in r.failures:
            print(f"        {f}")
    passed = sum(r.passed for r in results)
    print(f"\nИтого: {passed}/{len(results)} сценариев пройдено")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
