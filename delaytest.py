#!/usr/bin/env python3
"""로컬에서 EC2로 tcpping/tcping 지연을 측정하고 결과를 정리합니다.

실행:
  python delaytest.py --target virginia
  python delaytest.py --target chicago
  python delaytest.py --target all
  python delaytest.py --count 30 --ports 22,80
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import socket
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
RESULTS_DIR = SCRIPT_DIR / "results"

DEFAULT_COUNT = 30
DEFAULT_INTERVAL = 1.0
DEFAULT_PORTS = [22, 80]


def log(msg: str) -> None:
    print(msg, flush=True)


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.is_file():
        raise FileNotFoundError(
            f"{CONFIG_PATH} 없음. 먼저 `python installer.py`를 실행하세요."
        )
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("config.json 형식이 올바르지 않습니다.")
    # installer migrate와 동일하게 구형 flat → targets
    if "targets" not in data and data.get("instance_id"):
        data = {
            "region": data.get("region"),
            "my_ip": data.get("my_ip"),
            "ports": data.get("ports"),
            "targets": {
                "virginia": {
                    "id": "virginia",
                    "label": "Region (Virginia, us-east-1)",
                    "kind": "region",
                    "region": data.get("region"),
                    "instance_id": data.get("instance_id"),
                    "public_ip": data.get("public_ip"),
                    "public_dns": data.get("public_dns"),
                    "ports": data.get("ports"),
                    "az": data.get("az"),
                }
            },
        }
    return data


def resolve_targets(config: dict[str, Any], target_arg: str | None) -> list[dict[str, Any]]:
    targets = config.get("targets") or {}
    if target_arg in (None, "", "default"):
        # 단일 host 하위호환 또는 첫 타겟
        if targets:
            # virginia 우선, 없으면 전부
            if "virginia" in targets:
                return [targets["virginia"]]
            return [next(iter(targets.values()))]
        if config.get("public_ip"):
            return [
                {
                    "id": "custom",
                    "label": "custom",
                    "public_ip": config.get("public_ip"),
                    "ports": config.get("ports"),
                }
            ]
        return []
    if target_arg == "all":
        order = ["virginia", "chicago"]
        out = []
        for key in order:
            if key in targets:
                out.append(targets[key])
        for key, val in targets.items():
            if key not in order:
                out.append(val)
        return out
    if target_arg not in targets:
        raise KeyError(
            f"config에 타겟 '{target_arg}' 없음. 있는 타겟: {list(targets.keys())}. "
            f"`python installer.py --target {target_arg}` 를 먼저 실행하세요."
        )
    return [targets[target_arg]]


def find_probe_tool() -> tuple[str, str]:
    """(tool_name, binary_path). 우선순위: tcpping → tcping → python."""
    for name in ("tcpping", "tcping"):
        path = shutil.which(name)
        if path:
            return name, path
    # Homebrew 경로 직접 확인
    for candidate in (
        "/opt/homebrew/bin/tcpping",
        "/opt/homebrew/bin/tcping",
        "/usr/local/bin/tcpping",
        "/usr/local/bin/tcping",
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return Path(candidate).name, candidate
    return "python", sys.executable


def python_tcp_ping(host: str, port: int, timeout: float = 5.0) -> float | None:
    """TCP RTT(ms). open(SYN-ACK)뿐 아니라 closed(RST)도 응답 시각으로 측정.

    Connection refused(Errno 61/111)는 호스트까지 도달한 뒤의 RST이므로
    지연 측정에 포함. timeout/unreachable만 실패(None).
    """
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return (time.perf_counter() - start) * 1000.0
    except ConnectionRefusedError:
        # RST 수신 — 왕복 완료
        return (time.perf_counter() - start) * 1000.0
    except OSError as exc:
        # macOS: errno 61 ECONNREFUSED 가 ConnectionRefusedError가 아닐 수 있음
        if getattr(exc, "errno", None) in (61, 111):
            return (time.perf_counter() - start) * 1000.0
        return None


def run_python_probes(
    host: str, port: int, count: int, interval: float
) -> list[float | None]:
    samples: list[float | None] = []
    for i in range(count):
        rtt = python_tcp_ping(host, port)
        samples.append(rtt)
        if rtt is None:
            log(f"  [{i + 1}/{count}] {host}:{port}  timeout/fail")
        else:
            log(f"  [{i + 1}/{count}] {host}:{port}  {rtt:.3f} ms")
        if i + 1 < count:
            time.sleep(interval)
    return samples


def parse_rtt_ms(line: str) -> float | None:
    """tcpping/tcping 출력에서 RTT(ms) 추출."""
    # 예: "seq=0 ... time=123.456 ms" / "123.45ms" / "time=12.3"
    patterns = [
        r"time[=:\s]+([0-9]+(?:\.[0-9]+)?)\s*ms",
        r"([0-9]+(?:\.[0-9]+)?)\s*ms",
        r"rtt[=:\s]+([0-9]+(?:\.[0-9]+)?)",
    ]
    for pat in patterns:
        m = re.search(pat, line, re.IGNORECASE)
        if m:
            return float(m.group(1))
    return None


def run_tcpping(
    binary: str, host: str, port: int, count: int, interval: float
) -> tuple[list[float | None], str]:
    """원조 tcpping (Perl): sudo tcpping -c -r N -x COUNT host port"""
    cmd = [
        "sudo",
        "-n",
        binary,
        "-c",
        "-r",
        str(max(1, int(interval))),
        "-x",
        str(count),
        host,
        str(port),
    ]
    log(f"  $ {' '.join(cmd)}")
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=count * (interval + 5) + 30,
        )
    except subprocess.TimeoutExpired:
        return [None] * count, "timeout"
    raw = (proc.stdout or "") + (proc.stderr or "")
    samples: list[float | None] = []
    for line in raw.splitlines():
        rtt = parse_rtt_ms(line)
        if rtt is not None:
            samples.append(rtt)
            log(f"  {line.strip()}")
    if not samples and proc.returncode != 0:
        log(f"  tcpping 실패(rc={proc.returncode}): {raw.strip()[:500]}")
        # sudo -n 실패 시 sudo 없이 재시도
        if "password" in raw.lower() or proc.returncode == 1:
            cmd2 = [binary, "-c", "-r", str(max(1, int(interval))), "-x", str(count), host, str(port)]
            log(f"  retry without sudo: {' '.join(cmd2)}")
            proc2 = subprocess.run(
                cmd2,
                capture_output=True,
                text=True,
                timeout=count * (interval + 5) + 30,
            )
            raw = (proc2.stdout or "") + (proc2.stderr or "")
            samples = []
            for line in raw.splitlines():
                rtt = parse_rtt_ms(line)
                if rtt is not None:
                    samples.append(rtt)
                    log(f"  {line.strip()}")
    while len(samples) < count:
        samples.append(None)
    return samples[:count], raw


def _tcping_once(binary: str, host: str, port: int, timeout_sec: float = 5.0) -> tuple[float | None, str]:
    """Homebrew tcping(2.x): tcping [-t sec] <host> <port>  (단발)."""
    cmd = [binary, "-t", str(max(1, int(timeout_sec))), host, str(port)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec + 5)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    raw = ((proc.stdout or "") + (proc.stderr or "")).strip()
    # 예: "172.x.x.x port 22 open: 45.123 ms" / "... closed ..."
    rtt = parse_rtt_ms(raw)
    if rtt is not None:
        return rtt, raw
    # 일부 버전은 종료코드만 주고 시간 미출력 → Python fallback 한 번
    return None, raw or f"rc={proc.returncode}"


def run_tcping(
    binary: str, host: str, port: int, count: int, interval: float
) -> tuple[list[float | None], str]:
    """Homebrew tcping 단발을 count회 반복. 파싱 실패 시 Python TCP RTT."""
    # 버전 확인: -h 에 host/port 단발 형식인지
    help_out = ""
    try:
        help_proc = subprocess.run([binary], capture_output=True, text=True, timeout=5)
        help_out = (help_proc.stdout or "") + (help_proc.stderr or "")
    except Exception:
        pass

    samples: list[float | None] = []
    raw_lines: list[str] = []

    if "<host> <port>" in help_out or "timeout_sec" in help_out:
        log(f"  $ {binary} -t 5 {host} {port}  (x{count})")
        for i in range(count):
            rtt, raw = _tcping_once(binary, host, port)
            raw_lines.append(raw)
            if rtt is None:
                # 도구가 ms를 안 찍으면 Python으로 대체 측정
                rtt = python_tcp_ping(host, port)
                src = "python-fallback"
            else:
                src = "tcping"
            samples.append(rtt)
            if rtt is None:
                log(f"  [{i + 1}/{count}] {host}:{port}  fail ({raw})")
            else:
                log(f"  [{i + 1}/{count}] {host}:{port}  {rtt:.3f} ms ({src})")
            if i + 1 < count:
                time.sleep(interval)
        return samples, "\n".join(raw_lines)

    # 그 외 멀티샷 CLI 시도
    candidates = [
        [binary, "-c", str(count), "-i", str(interval), f"{host}:{port}"],
        [binary, "-c", str(count), "-i", str(interval), host, str(port)],
        [binary, "-n", str(count), host, str(port)],
    ]
    raw = help_out
    for cmd in candidates:
        log(f"  $ {' '.join(cmd)}")
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=count * (interval + 5) + 30,
            )
        except subprocess.TimeoutExpired:
            continue
        raw = (proc.stdout or "") + (proc.stderr or "")
        samples = []
        for line in raw.splitlines():
            rtt = parse_rtt_ms(line)
            if rtt is not None:
                samples.append(rtt)
                log(f"  {line.strip()}")
        if samples:
            while len(samples) < count:
                samples.append(None)
            return samples[:count], raw
    return [None] * count, raw


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


def wait_port_open(host: str, port: int, timeout: float = 120.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if python_tcp_ping(host, port, timeout=3.0) is not None:
            return True
        time.sleep(3)
    return False


def run_for_port(
    tool: str,
    binary: str,
    host: str,
    port: int,
    count: int,
    interval: float,
) -> dict[str, Any]:
    log(f"\n=== {host}:{port} ({count}회, interval={interval}s, tool={tool}) ===")
    if tool == "tcpping":
        samples, raw = run_tcpping(binary, host, port, count, interval)
        if all(s is None for s in samples):
            log("  tcpping 시간 파싱 실패 → Python TCP RTT fallback")
            samples = run_python_probes(host, port, count, interval)
            raw = "(python fallback)"
            tool = "python-tcp"
    elif tool == "tcping":
        # Homebrew tcping(2.x)은 open/closed만 보고 ms를 안 줌 → 도달성 확인 후 Python RTT
        log(f"  reachability check via tcping, RTT via Python TCP connect")
        _rtt, reach_raw = _tcping_once(binary, host, port)
        log(f"  tcping: {reach_raw or '(no output)'}")
        samples = run_python_probes(host, port, count, interval)
        raw = reach_raw + "\n(python-tcp RTT)"
        tool = "tcping+python-tcp"
    else:
        samples = run_python_probes(host, port, count, interval)
        raw = "(python)"
        tool = "python-tcp"

    stats = summarize(samples)
    log(
        f"  → min={stats['min_ms']} avg={stats['avg_ms']} max={stats['max_ms']} "
        f"stdev={stats['stdev_ms']} ms | loss={stats['loss_pct']}% "
        f"| ~one-way={stats['approx_one_way_ms']} ms"
    )
    return {
        "host": host,
        "port": port,
        "tool": tool,
        "samples_ms": samples,
        "stats": stats,
        "raw_tail": "\n".join(raw.splitlines()[-40:]) if isinstance(raw, str) else "",
    }


def _fmt_ms(v: Any) -> str:
    return f"{v:.3f}" if isinstance(v, (int, float)) else "n/a"


def print_summary_table(title: str, results: list[dict[str, Any]]) -> None:
    log("\n" + "=" * 78)
    log(title)
    log("=" * 78)
    header = (
        f"{'Port':>6}  {'Tool':<18}  {'Min':>8}  {'Avg':>8}  {'Max':>8}  "
        f"{'Stdev':>8}  {'P95':>8}  {'Loss%':>6}  {'~1-way':>8}"
    )
    log(header)
    log("-" * len(header))
    for r in results:
        s = r["stats"]
        log(
            f"{r['port']:>6}  {r['tool']:<18}  {_fmt_ms(s['min_ms']):>8}  {_fmt_ms(s['avg_ms']):>8}  "
            f"{_fmt_ms(s['max_ms']):>8}  {_fmt_ms(s['stdev_ms']):>8}  {_fmt_ms(s['p95_ms']):>8}  "
            f"{s['loss_pct']:>6.1f}  {_fmt_ms(s['approx_one_way_ms']):>8}"
        )
    log("=" * 78)


def print_comparison(target_payloads: list[dict[str, Any]], port: int = 22) -> None:
    log("\n" + "#" * 78)
    log(f"비교 요약 — 시험장소: 서울 → 대상별 TCP {port} RTT (ms)")
    log("#" * 78)
    header = (
        f"{'Target':<12} {'Label':<32} {'Avg':>8} {'P50':>8} {'P95':>8} "
        f"{'Stdev':>8} {'~1-way':>8}"
    )
    log(header)
    log("-" * len(header))
    rows = []
    for tp in target_payloads:
        match = next((r for r in tp["results"] if r["port"] == port), None)
        if not match:
            continue
        s = match["stats"]
        rows.append((tp, s))
        log(
            f"{tp['target_id']:<12} {str(tp.get('label') or '')[:32]:<32} "
            f"{_fmt_ms(s['avg_ms']):>8} {_fmt_ms(s['p50_ms']):>8} {_fmt_ms(s['p95_ms']):>8} "
            f"{_fmt_ms(s['stdev_ms']):>8} {_fmt_ms(s['approx_one_way_ms']):>8}"
        )
    if len(rows) >= 2:
        a_tp, a_s = rows[0]
        b_tp, b_s = rows[1]
        if isinstance(a_s.get("avg_ms"), (int, float)) and isinstance(b_s.get("avg_ms"), (int, float)):
            delta = b_s["avg_ms"] - a_s["avg_ms"]
            faster = a_tp["target_id"] if delta > 0 else b_tp["target_id"]
            log("-" * len(header))
            log(
                f"avg 차이: {b_tp['target_id']} − {a_tp['target_id']} = {delta:+.3f} ms "
                f"→ 더 낮음(빠름): {faster}"
            )
    log("#" * 78)


def measure_one_target(
    *,
    target: dict[str, Any],
    ports: list[int],
    count: int,
    interval: float,
    wait: float,
    tool: str,
    binary: str,
    my_ip: str | None,
) -> dict[str, Any]:
    host = target.get("public_ip") or target.get("public_dns")
    if not host:
        raise RuntimeError(f"타겟 {target.get('id')}에 public_ip가 없습니다.")
    tid = target.get("id") or "unknown"
    label = target.get("label") or tid

    log(f"\n######## 타겟: {tid} — {label} ########")
    log(f"호스트: {host}  AZ: {target.get('az')}  kind: {target.get('kind')}")
    log(f"포트: {ports}  횟수: {count}  interval: {interval}s  tool: {tool}")
    if my_ip:
        log(f"측정 PC 공인 IP: {my_ip}")

    primary = 22 if 22 in ports else ports[0]
    log(f"포트 {primary} 오픈 대기 (최대 {wait:.0f}s)...")
    if wait_port_open(host, primary, timeout=wait):
        log(f"포트 {primary} 도달 가능")
    else:
        log(f"경고: {host}:{primary} 미오픈 — 계속 진행")

    if 80 in ports and primary != 80:
        if wait_port_open(host, 80, timeout=min(120.0, wait)):
            log("포트 80 도달 가능")
        else:
            log("포트 80 미오픈")

    results = [
        run_for_port(tool, binary, host, port, count, interval) for port in ports
    ]
    print_summary_table(f"결과: {tid} ({label})", results)

    return {
        "target_id": tid,
        "label": label,
        "kind": target.get("kind"),
        "az": target.get("az"),
        "region": target.get("region"),
        "host": host,
        "instance_id": target.get("instance_id"),
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "tool": tool,
        "count": count,
        "interval": interval,
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="tcpping 지연 테스트")
    parser.add_argument(
        "--target",
        default="all",
        help="virginia | chicago | all (기본 all)",
    )
    parser.add_argument("--host", default=None, help="대상 IP/DNS (지정 시 config 타겟 무시)")
    parser.add_argument("--ports", default=None, help="콤마 구분 포트")
    parser.add_argument("--port", type=int, default=None, help="단일 포트")
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    parser.add_argument("--wait", type=float, default=180.0, help="포트 오픈 대기(초)")
    parser.add_argument("--tool", choices=("auto", "tcpping", "tcping", "python"), default="auto")
    args = parser.parse_args()

    config: dict[str, Any] = {}
    try:
        config = load_config()
    except FileNotFoundError as exc:
        if not args.host:
            log(str(exc))
            return 1

    if args.port:
        ports = [args.port]
    elif args.ports:
        ports = [int(p.strip()) for p in args.ports.split(",") if p.strip()]
    else:
        ports = list(config.get("ports") or DEFAULT_PORTS) or DEFAULT_PORTS

    if args.tool == "auto":
        tool, binary = find_probe_tool()
    elif args.tool == "python":
        tool, binary = "python", sys.executable
    else:
        path = shutil.which(args.tool)
        if not path:
            log(f"{args.tool} 바이너리를 찾을 수 없습니다.")
            return 1
        tool, binary = args.tool, path

    if args.host:
        target_list = [
            {
                "id": "custom",
                "label": "custom (--host)",
                "public_ip": args.host,
                "ports": ports,
            }
        ]
    else:
        try:
            target_list = resolve_targets(config, args.target)
        except KeyError as exc:
            log(str(exc))
            return 1
        if not target_list:
            log("측정할 타겟이 없습니다. installer.py --target virginia|chicago 를 실행하세요.")
            return 1

    target_payloads: list[dict[str, Any]] = []
    for t in target_list:
        t_ports = ports
        if not args.ports and not args.port and t.get("ports"):
            t_ports = list(t["ports"])
        payload = measure_one_target(
            target=t,
            ports=t_ports,
            count=args.count,
            interval=args.interval,
            wait=args.wait,
            tool=tool,
            binary=binary,
            my_ip=config.get("my_ip"),
        )
        target_payloads.append(payload)

    if len(target_payloads) >= 2:
        print_comparison(target_payloads, port=22)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    combined = {
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "site": "서울",
        "my_ip": config.get("my_ip"),
        "tool": tool,
        "count": args.count,
        "interval": args.interval,
        "targets": target_payloads,
    }
    out_path = RESULTS_DIR / f"delaytest-{stamp}.json"
    out_path.write_text(json.dumps(combined, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    latest = RESULTS_DIR / "latest.json"
    latest.write_text(json.dumps(combined, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # 타겟별 개별 파일
    for tp in target_payloads:
        tid = tp["target_id"]
        one = RESULTS_DIR / f"latest-{tid}.json"
        one.write_text(json.dumps(tp, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # 비교 전용
    if len(target_payloads) >= 2:
        compare_path = RESULTS_DIR / "comparison-latest.json"
        compare_path.write_text(
            json.dumps(combined, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        log(f"비교 결과: {compare_path}")

    log(f"\n결과 저장: {out_path}")
    log(f"최신 결과: {latest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
