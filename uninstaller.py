#!/usr/bin/env python3
"""delay-test 인프라 삭제 (EC2 + Security Group + config.json).

실행:
  python uninstaller.py --yes
  python uninstaller.py --target virginia --yes
  python uninstaller.py --target chicago --yes
  python uninstaller.py --target all --yes
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"

DEFAULT_REGION = "us-east-1"
NAME_PREFIX = "delay-test"


def log(msg: str) -> None:
    print(msg, flush=True)


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.is_file():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError as exc:
        log(f"config.json 파싱 실패: {exc}")
        return {}


def save_config(config: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def find_tagged_instances(ec2, target: str | None = None) -> list[dict[str, Any]]:
    filters = [
        {"Name": "tag:Project", "Values": [NAME_PREFIX]},
        {
            "Name": "instance-state-name",
            "Values": ["pending", "running", "stopping", "stopped"],
        },
    ]
    if target and target != "all":
        filters.append({"Name": "tag:Target", "Values": [target]})
    resp = ec2.describe_instances(Filters=filters)
    out: list[dict[str, Any]] = []
    for res in resp.get("Reservations", []):
        out.extend(res.get("Instances", []))
    return out


def find_tagged_security_groups(ec2) -> list[dict[str, Any]]:
    by_tag = ec2.describe_security_groups(
        Filters=[{"Name": "tag:Project", "Values": [NAME_PREFIX]}]
    )["SecurityGroups"]
    by_name = ec2.describe_security_groups(
        Filters=[{"Name": "group-name", "Values": [f"{NAME_PREFIX}-sg"]}]
    )["SecurityGroups"]
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for sg in by_tag + by_name:
        gid = sg["GroupId"]
        if gid not in seen:
            seen.add(gid)
            out.append(sg)
    return out


def find_tagged_subnets(ec2, target: str | None = None) -> list[str]:
    filters = [{"Name": "tag:Project", "Values": [NAME_PREFIX]}]
    if target and target != "all":
        # Name = delay-test-{target}-subnet
        filters.append({"Name": "tag:Name", "Values": [f"{NAME_PREFIX}-{target}-subnet"]})
    return [s["SubnetId"] for s in ec2.describe_subnets(Filters=filters)["Subnets"]]


def terminate_instance(ec2, instance_id: str) -> None:
    try:
        state = ec2.describe_instances(InstanceIds=[instance_id])
        current = state["Reservations"][0]["Instances"][0]["State"]["Name"]
    except ClientError as exc:
        log(f"  인스턴스 조회 실패(스킵): {instance_id} — {exc}")
        return

    if current in ("terminated", "shutting-down"):
        if current == "shutting-down":
            log(f"  종료 중 대기: {instance_id}")
            ec2.get_waiter("instance_terminated").wait(InstanceIds=[instance_id])
            log(f"  종료 완료: {instance_id}")
        else:
            log(f"  이미 종료됨: {instance_id}")
        return

    log(f"  인스턴스 종료: {instance_id} (state={current})")
    ec2.terminate_instances(InstanceIds=[instance_id])
    ec2.get_waiter("instance_terminated").wait(
        InstanceIds=[instance_id],
        WaiterConfig={"Delay": 5, "MaxAttempts": 60},
    )
    log(f"  종료 완료: {instance_id}")


def delete_security_group(ec2, sg_id: str, *, retries: int = 6) -> bool:
    for attempt in range(1, retries + 1):
        try:
            ec2.delete_security_group(GroupId=sg_id)
            log(f"  Security Group 삭제: {sg_id}")
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("InvalidGroup.NotFound", "InvalidSecurityGroupID.NotFound"):
                log(f"  SG 이미 없음: {sg_id}")
                return True
            wait = min(5 * attempt, 20)
            log(f"  SG 삭제 재시도 {attempt}/{retries} ({wait}s): {exc}")
            time.sleep(wait)
    log(f"  SG 삭제 실패(수동 삭제 필요): {sg_id}")
    return False


def delete_subnet(ec2, subnet_id: str) -> None:
    try:
        ec2.delete_subnet(SubnetId=subnet_id)
        log(f"  서브넷 삭제: {subnet_id}")
    except ClientError as exc:
        log(f"  서브넷 삭제 스킵: {subnet_id} — {exc}")


def collect_targets(
    ec2, config: dict[str, Any], target: str
) -> tuple[list[str], list[str], list[str]]:
    """(instance_ids, security_group_ids, subnet_ids)"""
    instance_ids: list[str] = []
    sg_ids: list[str] = []
    subnet_ids: list[str] = []

    targets = config.get("targets") or {}
    # 구형 flat
    if not targets and config.get("instance_id"):
        targets = {"virginia": config}

    selected_keys: list[str]
    if target == "all":
        selected_keys = list(targets.keys()) or ["virginia", "chicago"]
    else:
        selected_keys = [target]

    for key in selected_keys:
        t = targets.get(key) or {}
        if t.get("instance_id") and t["instance_id"] not in instance_ids:
            instance_ids.append(t["instance_id"])
        if t.get("security_group_id") and t["security_group_id"] not in sg_ids:
            sg_ids.append(t["security_group_id"])
        if t.get("subnet_id") and t.get("kind") == "local-zone":
            if t["subnet_id"] not in subnet_ids:
                subnet_ids.append(t["subnet_id"])

    if config.get("security_group_id") and target == "all":
        if config["security_group_id"] not in sg_ids:
            sg_ids.append(config["security_group_id"])

    for inst in find_tagged_instances(ec2, None if target == "all" else target):
        iid = inst["InstanceId"]
        if iid not in instance_ids:
            instance_ids.append(iid)

    if target == "all":
        for sg in find_tagged_security_groups(ec2):
            if sg["GroupId"] not in sg_ids:
                sg_ids.append(sg["GroupId"])
        for sid in find_tagged_subnets(ec2, None):
            if sid not in subnet_ids:
                subnet_ids.append(sid)
    else:
        for sid in find_tagged_subnets(ec2, target):
            if sid not in subnet_ids:
                subnet_ids.append(sid)

    return instance_ids, sg_ids, subnet_ids


def destroy(
    *,
    region: str,
    delete_sg: bool,
    yes: bool,
    keep_config: bool,
    target: str = "all",
    delete_subnet_flag: bool = True,
) -> int:
    config = load_config()
    region = region or config.get("region") or DEFAULT_REGION
    target = (target or "all").lower()

    try:
        session = boto3.Session(region_name=region)
        ec2 = session.client("ec2")
        sts = session.client("sts")
        ident = sts.get_caller_identity()
        log(f"AWS Account: {ident['Account']} / {ident['Arn']}")
        log(f"리전: {region}  target: {target}")
    except NoCredentialsError:
        log("AWS 자격 증명이 없습니다. aws configure 또는 환경변수를 확인하세요.")
        return 1

    instance_ids, sg_ids, subnet_ids = collect_targets(ec2, config, target)

    # 부분 삭제 시 다른 타겟이 살아 있으면 SG 유지
    remaining_targets = 0
    if target != "all" and isinstance(config.get("targets"), dict):
        for key, t in config["targets"].items():
            if key == target:
                continue
            iid = t.get("instance_id")
            if not iid:
                continue
            try:
                st = ec2.describe_instances(InstanceIds=[iid])["Reservations"][0]["Instances"][0][
                    "State"
                ]["Name"]
                if st not in ("terminated", "shutting-down"):
                    remaining_targets += 1
            except ClientError:
                pass
        if remaining_targets > 0:
            delete_sg = False
            log(f"다른 타겟 {remaining_targets}개 생존 → SG 유지")

    log("")
    log("삭제 대상:")
    log(f"  EC2           : {instance_ids or '(없음)'}")
    log(f"  Security Group: {sg_ids or '(없음)'}  delete_sg={delete_sg}")
    log(f"  Subnet (LZ)   : {subnet_ids or '(없음)'}  delete_subnet={delete_subnet_flag}")
    log(f"  config.json   : {CONFIG_PATH if CONFIG_PATH.exists() else '(없음)'}")

    if not instance_ids and not sg_ids and not subnet_ids and not CONFIG_PATH.exists():
        log("지울 delay-test 리소스가 없습니다.")
        return 0

    if not yes:
        try:
            ans = input("위 리소스를 삭제할까요? [y/N] ").strip().lower()
        except EOFError:
            ans = "n"
        if ans not in ("y", "yes"):
            log("취소됨.")
            return 1

    log("\n=== EC2 종료 ===")
    for iid in instance_ids:
        terminate_instance(ec2, iid)

    if delete_subnet_flag and subnet_ids:
        log("\n=== Local Zone 서브넷 삭제 ===")
        if instance_ids:
            log("  ENI 정리 대기 15s...")
            time.sleep(15)
        for sid in subnet_ids:
            delete_subnet(ec2, sid)

    if delete_sg and sg_ids:
        log("\n=== Security Group 삭제 ===")
        if instance_ids and not subnet_ids:
            log("  ENI 정리 대기 10s...")
            time.sleep(10)
        for gid in sg_ids:
            delete_security_group(ec2, gid)
    elif sg_ids:
        log(f"\nSG 유지: {sg_ids}")

    if CONFIG_PATH.exists():
        if target == "all" and not keep_config:
            CONFIG_PATH.unlink()
            log(f"\nconfig.json 삭제: {CONFIG_PATH}")
        elif target != "all":
            cfg = load_config()
            targets = cfg.get("targets") or {}
            if target in targets:
                del targets[target]
                cfg["targets"] = targets
                if keep_config or targets:
                    save_config(cfg)
                    log(f"\nconfig.json에서 타겟 '{target}' 제거")
                else:
                    CONFIG_PATH.unlink()
                    log("\nconfig.json 삭제 (타겟 없음)")
        elif keep_config:
            log("\nconfig.json 유지 (--keep-config)")

    log("\n삭제 완료.")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="delay-test EC2/SG 삭제")
    p.add_argument("--region", default=None, help=f"기본: config 또는 {DEFAULT_REGION}")
    p.add_argument("--target", default="all", help="virginia | chicago | all")
    p.add_argument("--yes", "-y", action="store_true", help="확인 없이 삭제")
    p.add_argument("--keep-sg", action="store_true", help="Security Group 유지")
    p.add_argument("--keep-config", action="store_true", help="config.json 유지")
    p.add_argument("--keep-subnet", action="store_true", help="Local Zone 서브넷 유지")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    try:
        return destroy(
            region=args.region or "",
            delete_sg=not args.keep_sg,
            yes=args.yes,
            keep_config=args.keep_config,
            target=args.target,
            delete_subnet_flag=not args.keep_subnet,
        )
    except ClientError as exc:
        log(f"AWS API 오류: {exc}")
        return 1
    except Exception as exc:
        log(f"실패: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
