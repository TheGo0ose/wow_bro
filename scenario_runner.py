"""Прогон сценариев в формате жюри (scenarios_examples.json).

Два режима времени:
  * по умолчанию — подменённые часы: сервис запущен с OTP_TEST_MODE=1, шаг advance
    перематывает часы (POST /__test__/advance), сценарий идёт мгновенно;
  * --real-time — как у жюри по умолчанию: advance = настоящее ожидание.

IP клиента (client_ip в сценарии):
  * --ip-map 192.0.2.10=127.0.0.1,198.51.100.20=127.0.0.2 — запрос реально
    уходит с указанного локального адреса, сервис берёт IP из соединения
    (так делает жюри). Это режим по умолчанию;
  * --ip-mode header — IP передаётся заголовком X-Test-Client-IP (нужен
    OTP_TRUSTED_IP_HEADER=X-Test-Client-IP у сервиса).

--spawn: перед каждым сценарием запускается свежий `python -m otp_service`
(сценарии независимы, состояние не переносится), после — останавливается.

Примеры:
    # быстро, все сценарии, свежий сервис на каждый сценарий
    python scenario_runner.py --spawn scenarios/scenarios_examples.json scenarios/extra_scenarios.json
    # как жюри: реальное время, IP из соединения
    python scenario_runner.py --spawn --real-time scenarios/scenarios_examples.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from otp_service.delivery import read_last_code

ROOT = Path(__file__).resolve().parent
IP_HEADER = "X-Test-Client-IP"
DEFAULT_OUTBOX = ROOT / "outbox" / "sms.jsonl"
DEFAULT_IP_MAP = "192.0.2.10=127.0.0.1,198.51.100.20=127.0.0.2"


@dataclass
class ScenarioResult:
    id: str
    title: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    seconds: float = 0.0


@dataclass
class RunConfig:
    base_url: str
    outbox: Path
    real_time: bool = False
    ip_mode: str = "source"                       # source | header
    ip_map: dict[str, str] = field(default_factory=dict)


def parse_ip_map(text: str) -> dict[str, str]:
    pairs = (p.split("=", 1) for p in text.split(",") if p.strip())
    return {k.strip(): v.strip() for k, v in pairs}


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


class Clients:
    """По одному HTTP-клиенту на каждый локальный адрес источника."""

    def __init__(self, cfg: RunConfig) -> None:
        self.cfg = cfg
        self._clients: dict[str | None, httpx.Client] = {}

    def for_ip(self, client_ip: str | None) -> httpx.Client:
        source = None
        if client_ip and self.cfg.ip_mode == "source":
            source = self.cfg.ip_map.get(client_ip)
            if source is None:
                raise KeyError(f"нет адреса для client_ip={client_ip}, задайте --ip-map")
        if source not in self._clients:
            transport = httpx.HTTPTransport(local_address=source) if source else None
            self._clients[source] = httpx.Client(base_url=self.cfg.base_url, timeout=15,
                                                 transport=transport)
        return self._clients[source]

    def close(self) -> None:
        for c in self._clients.values():
            c.close()


def _do_request(cfg: RunConfig, client: httpx.Client, req: dict[str, Any],
                bound: dict[str, str]) -> int:
    headers = {}
    if req.get("client_ip") and cfg.ip_mode == "header":
        headers[IP_HEADER] = req["client_ip"]
    kwargs: dict[str, Any] = {"headers": headers}
    if "raw_body" in req:                       # для проверки битого JSON
        kwargs["content"] = req["raw_body"]
        headers["Content-Type"] = "application/json"
    else:
        kwargs["json"] = substitute(req.get("body"), bound)
    return client.request(req.get("method", "POST"), req["path"], **kwargs).status_code


def run_scenario(cfg: RunConfig, scenario: dict[str, Any]) -> ScenarioResult:
    result = ScenarioResult(scenario["id"], scenario.get("title", ""), True)
    bound: dict[str, str] = {}
    clients = Clients(cfg)
    started = time.perf_counter()
    try:
        if not cfg.real_time:
            clients.for_ip(None).post("/__test__/reset").raise_for_status()
        for i, step in enumerate(scenario["steps"], 1):
            op = step["op"]
            if op == "request":
                status = _do_request(cfg, clients.for_ip(step.get("client_ip")), step, bound)
                exp = step.get("expected", {}).get("status")
                if exp is not None and status != exp:
                    result.failures.append(f"шаг {i} {step['path']}: ожидали {exp}, получили {status}")
            elif op == "capture_delivery":
                bound[step["bind"]] = read_last_code(cfg.outbox, step["msisdn"])
            elif op == "advance":
                if cfg.real_time:
                    time.sleep(step["seconds"])
                else:
                    clients.for_ip(None).post("/__test__/advance",
                                              json={"seconds": step["seconds"]}).raise_for_status()
            elif op == "parallel":
                reqs = step["requests"]
                barrier = threading.Barrier(len(reqs))

                def fire(req: dict[str, Any]) -> int:
                    own = Clients(cfg)              # отдельное соединение на поток
                    try:
                        client = own.for_ip(req.get("client_ip"))
                        barrier.wait()
                        return _do_request(cfg, client, req, bound)
                    finally:
                        own.close()

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
    finally:
        clients.close()
    result.seconds = time.perf_counter() - started
    result.passed = not result.failures
    return result


def load_scenarios(paths: list[Path]) -> list[dict[str, Any]]:
    scenarios: list[dict[str, Any]] = []
    for p in paths:
        scenarios.extend(json.loads(Path(p).read_text(encoding="utf-8")))
    return scenarios


class SpawnedService:
    """Свежий процесс сервиса на один сценарий."""

    def __init__(self, port: int, outbox: Path, test_mode: bool) -> None:
        env = dict(os.environ, OTP_PORT=str(port), OTP_OUTBOX=str(outbox),
                   OTP_SECURITY_LOG=str(outbox.parent / "security.jsonl"),
                   OTP_TEST_MODE="1" if test_mode else "0", PYTHONIOENCODING="utf-8")
        env.pop("OTP_TRUSTED_IP_HEADER", None)
        self.proc = subprocess.Popen([sys.executable, "-m", "otp_service"], cwd=ROOT, env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        url = f"http://127.0.0.1:{port}/health"
        deadline = time.time() + 15
        while True:
            try:
                if httpx.get(url, timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            if time.time() > deadline or self.proc.poll() is not None:
                self.stop()
                raise RuntimeError("сервис не запустился")
            time.sleep(0.1)

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--base-url", default=os.environ.get("OTP_BASE_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--outbox", type=Path, default=Path(os.environ.get("OTP_OUTBOX", DEFAULT_OUTBOX)))
    ap.add_argument("--real-time", action="store_true", help="advance = настоящее ожидание")
    ap.add_argument("--ip-mode", choices=["source", "header"], default="source")
    ap.add_argument("--ip-map", default=DEFAULT_IP_MAP)
    ap.add_argument("--spawn", action="store_true", help="свежий сервис на каждый сценарий")
    ap.add_argument("--port", type=int, default=8077, help="порт для --spawn")
    ap.add_argument("--only", default="", help="через запятую: id сценариев")
    args = ap.parse_args()

    scenarios = load_scenarios(args.files)
    if args.only:
        wanted = {x.strip() for x in args.only.split(",")}
        scenarios = [s for s in scenarios if s["id"] in wanted]

    results = []
    for sc in scenarios:
        service = None
        outbox = args.outbox
        base_url = args.base_url
        if args.spawn:
            outbox = Path(tempfile.mkdtemp(prefix="otp-")) / "sms.jsonl"
            base_url = f"http://127.0.0.1:{args.port}"
            service = SpawnedService(args.port, outbox, test_mode=not args.real_time)
        cfg = RunConfig(base_url, outbox, args.real_time, args.ip_mode, parse_ip_map(args.ip_map))
        try:
            r = run_scenario(cfg, sc)
        finally:
            if service:
                service.stop()
        results.append(r)
        print(f"{'PASS' if r.passed else 'FAIL'}  {r.id}  {r.title}  ({r.seconds:.1f} с)", flush=True)
        for f in r.failures:
            print(f"        {f}", flush=True)

    passed = sum(r.passed for r in results)
    print(f"\nИтого: {passed}/{len(results)} сценариев пройдено")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
