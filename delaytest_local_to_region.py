#!/usr/bin/env python3
"""Local Zone → Region TCP 지연 측정.

Chicago Local Zone EC2에서 SSM으로 probe를 실행해 Virginia Region EC2
(private IP 기본)까지의 TCP SYN RTT를 집계합니다.

사전조건:
  python installer_local_to_region.py

실행:
  python delaytest_local_to_region.py
  python delaytest_local_to_region.py --count 30 --ports 22,80
  python delaytest_local_to_region.py --via public
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config_local_to_region.json"
RESULTS_DIR = SCRIPT_DIR / "results"

DEFAULT_COUNT = 30
DEFAULT_INTERVAL = 1.0
DEFAULT_PORTS = [22, 80]


def log(msg: str) -> None:
    print(msg, flush=True)


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.is_file():
        raise FileNotFoundError(
            f"{CONFIG_PATH} 없음. 먼저 `python installer_local_to_region.py`를 실행하세요."
        )
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("config_local_to_region.json 형식이 올바르지 않습니다.")
    return data


def summarize(samples: list[float | None]) -> dict[str, Any]:
    ok = [s for s in samples if s is not None]
    lost = len(samples) - len(ok)
    if not ok:
        return {
            "sent": len(samples),
            "received": 0,
            "loss_pct": 100.0,
            "min_ms": None,
            "avg_ms": None,
            "max_ms": None,
            "stdev_ms": None,
            "p50_ms": None,
            "p95_ms": None,
            "approx_one_way_ms": None,
        }
    ok_sorted = sorted(ok)
    avg = statistics.mean(ok)
    stdev = statistics.stdev(ok) if len(ok) > 1 else 0.0
    p50 = statistics.median(ok)
    p95_idx = min(len(ok_sorted) - 1, max(0, math.ceil(0.95 * len(ok_sorted)) - 1))
    return {
        "sent": len(samples),
        "received": len(ok),
        "loss_pct": round(100.0 * lost / len(samples), 2),
        "min_ms": round(min(ok), 3),
        "avg_ms": round(avg, 3),
        "max_ms": round(max(ok), 3),
        "stdev_ms": round(stdev, 3),
        "p50_ms": round(p50, 3),
        "p95_ms": round(ok_sorted[p95_idx], 3),
        "approx_one_way_ms": round(avg / 2.0, 3),
    }


def _fmt_ms(v: Any) -> str:
    return f"{v:.3f}" if isinstance(v, (int, float)) else "n/a"


def resolve_host(dest: dict[str, Any], via: str) -> str:
    if via == "public":
        host = dest.get("public_ip") or dest.get("public_dns")
        if not host:
            raise RuntimeError("dest에 public_ip가 없습니다.")
        return host
    host = dest.get("private_ip")
    if not host:
        raise RuntimeError("dest에 private_ip가 없습니다. installer를 다시 실행하세요.")
    return host


def ensure_ssm_online(ssm, instance_id: str, timeout: float = 120.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = ssm.describe_instance_information(
            Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
        )
        infos = resp.get("InstanceInformationList") or []
        if infos and infos[0].get("PingStatus") == "Online":
            return
        time.sleep(5)
    raise RuntimeError(
        f"SSM Online이 아닙니다: {instance_id}. "
        "installer_local_to_region.py 재실행 또는 IAM/SSM 에이전트를 확인하세요."
    )


def build_probe_script(host: str, ports: list[int], count: int, interval: float) -> str:
    """소스 EC2에서 실행할 Python probe (stdout에 JSON 한 줄)."""
    ports_lit = ",".join(str(p) for p in ports)
    return f"""#!/bin/bash
set -euo pipefail
python3 - <<'PY'
import json, socket, time, statistics, math

HOST = {host!r}
PORTS = [{ports_lit}]
COUNT = {int(count)}
INTERVAL = {float(interval)}

def tcp_rtt(host, port, timeout=5.0):
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return (time.perf_counter() - start) * 1000.0
    except ConnectionRefusedError:
        return (time.perf_counter() - start) * 1000.0
    except OSError as exc:
        if getattr(exc, "errno", None) in (61, 111):
            return (time.perf_counter() - start) * 1000.0
        return None

def summarize(samples):
    ok = [s for s in samples if s is not None]
    lost = len(samples) - len(ok)
    if not ok:
        return {{
            "sent": len(samples), "received": 0, "loss_pct": 100.0,
            "min_ms": None, "avg_ms": None, "max_ms": None, "stdev_ms": None,
            "p50_ms": None, "p95_ms": None, "approx_one_way_ms": None,
        }}
    ok_sorted = sorted(ok)
    avg = statistics.mean(ok)
    stdev = statistics.stdev(ok) if len(ok) > 1 else 0.0
    p50 = statistics.median(ok)
    p95_idx = min(len(ok_sorted) - 1, max(0, math.ceil(0.95 * len(ok_sorted)) - 1))
    return {{
        "sent": len(samples),
        "received": len(ok),
        "loss_pct": round(100.0 * lost / len(samples), 2),
        "min_ms": round(min(ok), 3),
        "avg_ms": round(avg, 3),
        "max_ms": round(max(ok), 3),
        "stdev_ms": round(stdev, 3),
        "p50_ms": round(p50, 3),
        "p95_ms": round(ok_sorted[p95_idx], 3),
        "approx_one_way_ms": round(avg / 2.0, 3),
    }}

results = []
for port in PORTS:
    samples = []
    for i in range(COUNT):
        rtt = tcp_rtt(HOST, port)
        samples.append(rtt)
        tag = f"{{rtt:.3f}} ms" if rtt is not None else "fail"
        print(f"[probe] {{HOST}}:{{port}} [{{i+1}}/{{COUNT}}] {{tag}}", flush=True)
        if i + 1 < COUNT:
            time.sleep(INTERVAL)
    results.append({{
        "host": HOST,
        "port": port,
        "tool": "python-tcp-ssm",
        "samples_ms": samples,
        "stats": summarize(samples),
    }})

print("RESULT_JSON:" + json.dumps({{"results": results}}, separators=(",", ":")))
PY
"""


def run_ssm_probe(
    ssm,
    *,
    instance_id: str,
    host: str,
    ports: list[int],
    count: int,
    interval: float,
    timeout_sec: float,
) -> tuple[list[dict[str, Any]], str]:
    script = build_probe_script(host, ports, count, interval)
    # 명령 전체 예상 시간 + 여유
    cmd_timeout = int(timeout_sec) + int(count * (interval + 2) * len(ports)) + 60

    log(f"SSM SendCommand → {instance_id}")
    log(f"  target host={host} ports={ports} count={count} interval={interval}s")
    resp = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [script]},
        TimeoutSeconds=max(60, min(cmd_timeout, 172800)),
        Comment="delay-test-l2r: Local Zone to Region TCP RTT",
    )
    command_id = resp["Command"]["CommandId"]
    log(f"  CommandId={command_id}")

    deadline = time.time() + cmd_timeout
    invocation: dict[str, Any] | None = None
    while time.time() < deadline:
        time.sleep(4)
        try:
            inv = ssm.get_command_invocation(
                CommandId=command_id,
                InstanceId=instance_id,
            )
        except ClientError as exc:
            # InvocationDoesNotExist — 아직 전파 전
            if "InvocationDoesNotExist" in str(exc):
                continue
            raise
        status = inv.get("Status")
        if status in ("Success", "Cancelled", "TimedOut", "Failed"):
            invocation = inv
            break
        log(f"  SSM status={status} …")

    if invocation is None:
        raise RuntimeError(f"SSM 명령 대기 시간 초과: {command_id}")

    stdout = invocation.get("StandardOutputContent") or ""
    stderr = invocation.get("StandardErrorContent") or ""
    status = invocation.get("Status")
    log(f"  SSM 완료: Status={status}")
    if stderr.strip():
        for line in stderr.strip().splitlines()[-20:]:
            log(f"  stderr: {line}")

    # progress 로그
    for line in stdout.splitlines():
        if line.startswith("[probe]"):
            log(f"  {line}")

    result_line = None
    for line in stdout.splitlines():
        if line.startswith("RESULT_JSON:"):
            result_line = line[len("RESULT_JSON:") :]
    if not result_line:
        raise RuntimeError(
            f"probe JSON을 찾지 못했습니다. Status={status}\n"
            f"stdout tail:\n{stdout[-1500:]}\nstderr:\n{stderr[-800:]}"
        )

    payload = json.loads(result_line)
    results = payload.get("results") or []
    # 로컬에서도 stats 재계산(일관성)
    for r in results:
        if "samples_ms" in r:
            r["stats"] = summarize(r["samples_ms"])
    return results, stdout


def print_summary(source: dict, dest: dict, host: str, via: str, results: list[dict]) -> None:
    log("\n" + "#" * 78)
    log(
        f"Local Zone → Region  |  {source.get('id')} ({source.get('az')}) "
        f"→ {dest.get('id')} ({dest.get('az')})"
    )
    log(f"via={via}  host={host}")
    log("#" * 78)
    header = (
        f"{'Port':>6}  {'Min':>8}  {'Avg':>8}  {'Max':>8}  "
        f"{'Stdev':>8}  {'P50':>8}  {'P95':>8}  {'Loss%':>6}  {'~1-way':>8}"
    )
    log(header)
    log("-" * len(header))
    for r in results:
        s = r["stats"]
        log(
            f"{r['port']:>6}  {_fmt_ms(s['min_ms']):>8}  {_fmt_ms(s['avg_ms']):>8}  "
            f"{_fmt_ms(s['max_ms']):>8}  {_fmt_ms(s['stdev_ms']):>8}  "
            f"{_fmt_ms(s['p50_ms']):>8}  {_fmt_ms(s['p95_ms']):>8}  "
            f"{s['loss_pct']:>6.1f}  {_fmt_ms(s['approx_one_way_ms']):>8}"
        )
    log("#" * 78)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Chicago Local Zone → Virginia Region TCP 지연 측정 (SSM)"
    )
    parser.add_argument("--ports", default=None, help="콤마 구분 포트 (기본 22,80)")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    parser.add_argument(
        "--via",
        choices=("private", "public"),
        default="private",
        help="측정 대상 주소 (기본: VPC private IP)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="SSM Online/여유 대기(초). probe 본체 시간과는 별도",
    )
    args = parser.parse_args()

    try:
        config = load_config()
    except FileNotFoundError as exc:
        log(str(exc))
        return 1

    targets = config.get("targets") or {}
    source_id = config.get("source_id") or "chicago"
    dest_id = config.get("dest_id") or "virginia"
    source = targets.get(source_id)
    dest = targets.get(dest_id)
    if not source or not dest:
        log(
            f"config에 source({source_id})/dest({dest_id})가 없습니다. "
            "installer_local_to_region.py를 실행하세요."
        )
        return 1

    if args.port:
        ports = [args.port]
    elif args.ports:
        ports = [int(p.strip()) for p in args.ports.split(",") if p.strip()]
    else:
        # 443은 RST만 나는 경우가 많아 LZ→Region 비교는 22/80 기본
        cfg_ports = [p for p in (config.get("ports") or DEFAULT_PORTS) if p in (22, 80)]
        ports = cfg_ports or list(DEFAULT_PORTS)

    try:
        host = resolve_host(dest, args.via)
    except RuntimeError as exc:
        log(str(exc))
        return 1

    region = config.get("region") or dest.get("region") or "us-east-1"
    try:
        session = boto3.Session(region_name=region)
        ssm = session.client("ssm")
        session.client("sts").get_caller_identity()
    except NoCredentialsError:
        log("AWS 자격 증명이 없습니다.")
        return 1

    source_iid = source["instance_id"]
    log("=== Local Zone → Region delay test ===")
    log(f"source: {source_id} {source.get('az')} id={source_iid}")
    log(f"dest:   {dest_id} {dest.get('az')} private={dest.get('private_ip')} public={dest.get('public_ip')}")
    log(f"probe:  {source_id} --[{args.via}]→ {host}  ports={ports} count={args.count}")

    try:
        ensure_ssm_online(ssm, source_iid, timeout=args.timeout)
        results, _raw = run_ssm_probe(
            ssm,
            instance_id=source_iid,
            host=host,
            ports=ports,
            count=args.count,
            interval=args.interval,
            timeout_sec=args.timeout,
        )
    except (ClientError, RuntimeError, json.JSONDecodeError) as exc:
        log(f"측정 실패: {exc}")
        return 1

    print_summary(source, dest, host, args.via, results)

    payload = {
        "mode": "local-to-region",
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "via": args.via,
        "host": host,
        "source": {
            "id": source_id,
            "label": source.get("label"),
            "kind": source.get("kind"),
            "az": source.get("az"),
            "instance_id": source_iid,
            "private_ip": source.get("private_ip"),
            "public_ip": source.get("public_ip"),
        },
        "dest": {
            "id": dest_id,
            "label": dest.get("label"),
            "kind": dest.get("kind"),
            "az": dest.get("az"),
            "instance_id": dest.get("instance_id"),
            "private_ip": dest.get("private_ip"),
            "public_ip": dest.get("public_ip"),
        },
        "count": args.count,
        "interval": args.interval,
        "tool": "python-tcp-ssm",
        "results": results,
    }

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = RESULTS_DIR / f"l2r-{stamp}.json"
    latest = RESULTS_DIR / "latest-local-to-region.json"
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    out_path.write_text(text, encoding="utf-8")
    latest.write_text(text, encoding="utf-8")
    log(f"\n결과 저장: {out_path}")
    log(f"최신 결과: {latest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
