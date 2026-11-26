#!/usr/bin/env python3
"""Local Zone → Region 지연 측정 인프라 삭제.

installer_local_to_region.py 가 만든 EC2 / LZ 서브넷 / SG / SSM IAM /
config_local_to_region.json 을 정리합니다.

실행:
  python uninstaller_local_to_region.py --yes
  python uninstaller_local_to_region.py --yes --keep-iam
  python uninstaller_local_to_region.py --yes --keep-sg
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
CONFIG_PATH = SCRIPT_DIR / "config_local_to_region.json"

DEFAULT_REGION = "us-east-1"
NAME_PREFIX = "delay-test-l2r"
IAM_ROLE_NAME = f"{NAME_PREFIX}-ssm-role"
IAM_PROFILE_NAME = f"{NAME_PREFIX}-ssm-profile"


def log(msg: str) -> None:
    print(msg, flush=True)


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.is_file():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError as exc:
        log(f"config 파싱 실패: {exc}")
        return {}


def find_tagged_instances(ec2) -> list[dict[str, Any]]:
    resp = ec2.describe_instances(
        Filters=[
            {"Name": "tag:Project", "Values": [NAME_PREFIX]},
            {
                "Name": "instance-state-name",
                "Values": ["pending", "running", "stopping", "stopped"],
            },
        ]
    )
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


def find_tagged_subnets(ec2) -> list[str]:
    return [
        s["SubnetId"]
        for s in ec2.describe_subnets(
            Filters=[{"Name": "tag:Project", "Values": [NAME_PREFIX]}]
        )["Subnets"]
    ]


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


def delete_security_group(ec2, sg_id: str, *, retries: int = 8) -> bool:
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
            wait = min(5 * attempt, 25)
            log(f"  SG 삭제 재시도 {attempt}/{retries} ({wait}s): {exc}")
            time.sleep(wait)
    log(f"  SG 삭제 실패(수동 삭제 필요): {sg_id}")
    return False


def delete_subnet(ec2, subnet_id: str, *, retries: int = 6) -> bool:
    for attempt in range(1, retries + 1):
        try:
            ec2.delete_subnet(SubnetId=subnet_id)
            log(f"  서브넷 삭제: {subnet_id}")
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("InvalidSubnetID.NotFound", "InvalidSubnet.NotFound"):
                log(f"  서브넷 이미 없음: {subnet_id}")
                return True
            wait = min(5 * attempt, 20)
            log(f"  서브넷 삭제 재시도 {attempt}/{retries} ({wait}s): {exc}")
            time.sleep(wait)
    log(f"  서브넷 삭제 실패(수동 삭제 필요): {subnet_id}")
    return False


def delete_iam(iam, *, keep_iam: bool) -> None:
    if keep_iam:
        log(f"IAM 유지: role={IAM_ROLE_NAME} profile={IAM_PROFILE_NAME}")
        return

    log("\n=== IAM 삭제 ===")
    try:
        iam.remove_role_from_instance_profile(
            InstanceProfileName=IAM_PROFILE_NAME,
            RoleName=IAM_ROLE_NAME,
        )
        log(f"  Role을 Instance Profile에서 분리: {IAM_ROLE_NAME}")
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code != "NoSuchEntity":
            log(f"  Role 분리 스킵: {exc}")

    try:
        iam.delete_instance_profile(InstanceProfileName=IAM_PROFILE_NAME)
        log(f"  Instance Profile 삭제: {IAM_PROFILE_NAME}")
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "NoSuchEntity":
            log(f"  Instance Profile 이미 없음: {IAM_PROFILE_NAME}")
        else:
            log(f"  Instance Profile 삭제 스킵: {exc}")

    try:
        attached = iam.list_attached_role_policies(RoleName=IAM_ROLE_NAME).get(
            "AttachedPolicies", []
        )
        for p in attached:
            iam.detach_role_policy(RoleName=IAM_ROLE_NAME, PolicyArn=p["PolicyArn"])
            log(f"  정책 분리: {p['PolicyArn']}")
        iam.delete_role(RoleName=IAM_ROLE_NAME)
        log(f"  IAM Role 삭제: {IAM_ROLE_NAME}")
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "NoSuchEntity":
            log(f"  IAM Role 이미 없음: {IAM_ROLE_NAME}")
        else:
            log(f"  IAM Role 삭제 스킵: {exc}")


def collect_targets(
    ec2, config: dict[str, Any]
) -> tuple[list[str], list[str], list[str]]:
    """(instance_ids, security_group_ids, subnet_ids)"""
    instance_ids: list[str] = []
    sg_ids: list[str] = []
    subnet_ids: list[str] = []

    for t in (config.get("targets") or {}).values():
        if not isinstance(t, dict):
            continue
        if t.get("instance_id") and t["instance_id"] not in instance_ids:
            instance_ids.append(t["instance_id"])
        if t.get("security_group_id") and t["security_group_id"] not in sg_ids:
            sg_ids.append(t["security_group_id"])
        if t.get("subnet_id") and t.get("kind") == "local-zone":
            if t["subnet_id"] not in subnet_ids:
                subnet_ids.append(t["subnet_id"])

    if config.get("security_group_id") and config["security_group_id"] not in sg_ids:
        sg_ids.append(config["security_group_id"])

    for inst in find_tagged_instances(ec2):
        iid = inst["InstanceId"]
        if iid not in instance_ids:
            instance_ids.append(iid)

    for sg in find_tagged_security_groups(ec2):
        if sg["GroupId"] not in sg_ids:
            sg_ids.append(sg["GroupId"])

    for sid in find_tagged_subnets(ec2):
        if sid not in subnet_ids:
            subnet_ids.append(sid)

    return instance_ids, sg_ids, subnet_ids


def iam_exists(iam) -> bool:
    try:
        iam.get_role(RoleName=IAM_ROLE_NAME)
        return True
    except ClientError:
        pass
    try:
        iam.get_instance_profile(InstanceProfileName=IAM_PROFILE_NAME)
        return True
    except ClientError:
        pass
    return False


def destroy(
    *,
    region: str,
    yes: bool,
    keep_iam: bool = False,
    keep_sg: bool = False,
    keep_subnet: bool = False,
    keep_config: bool = False,
) -> int:
    config = load_config()
    region = region or config.get("region") or DEFAULT_REGION

    try:
        session = boto3.Session(region_name=region)
        ec2 = session.client("ec2")
        iam = session.client("iam")
        ident = session.client("sts").get_caller_identity()
        log(f"AWS Account: {ident['Account']} / {ident['Arn']}")
        log(f"리전: {region}")
    except NoCredentialsError:
        log("AWS 자격 증명이 없습니다. aws configure 또는 환경변수를 확인하세요.")
        return 1

    instance_ids, sg_ids, subnet_ids = collect_targets(ec2, config)
    has_iam = (not keep_iam) and iam_exists(iam)

    log("")
    log("=== Local Zone → Region 환경 삭제 ===")
    log(f"  EC2           : {instance_ids or '(없음)'}")
    log(f"  Security Group: {sg_ids or '(없음)'}  delete_sg={not keep_sg}")
    log(f"  Subnet (LZ)   : {subnet_ids or '(없음)'}  delete_subnet={not keep_subnet}")
    log(f"  IAM           : role={IAM_ROLE_NAME} profile={IAM_PROFILE_NAME}  delete_iam={not keep_iam}")
    log(f"  config        : {CONFIG_PATH if CONFIG_PATH.exists() else '(없음)'}")

    nothing = (
        not instance_ids
        and not sg_ids
        and not subnet_ids
        and not has_iam
        and not CONFIG_PATH.exists()
    )
    if nothing:
        log("지울 delay-test-l2r 리소스가 없습니다.")
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

    if not keep_subnet and subnet_ids:
        log("\n=== Local Zone 서브넷 삭제 ===")
        if instance_ids:
            log("  ENI 정리 대기 15s...")
            time.sleep(15)
        for sid in subnet_ids:
            delete_subnet(ec2, sid)
    elif subnet_ids:
        log(f"\n서브넷 유지: {subnet_ids}")

    if not keep_sg and sg_ids:
        log("\n=== Security Group 삭제 ===")
        if instance_ids and keep_subnet:
            log("  ENI 정리 대기 10s...")
            time.sleep(10)
        for gid in sg_ids:
            delete_security_group(ec2, gid)
    elif sg_ids:
        log(f"\nSG 유지: {sg_ids}")

    delete_iam(iam, keep_iam=keep_iam)

    if CONFIG_PATH.exists():
        if keep_config:
            log(f"\nconfig 유지: {CONFIG_PATH}")
        else:
            CONFIG_PATH.unlink()
            log(f"\nconfig 삭제: {CONFIG_PATH}")

    log("\n삭제 완료.")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Local Zone → Region 지연 측정 EC2/SG/IAM 삭제"
    )
    p.add_argument("--region", default=None, help=f"기본: config 또는 {DEFAULT_REGION}")
    p.add_argument("--yes", "-y", action="store_true", help="확인 없이 삭제")
    p.add_argument("--keep-sg", action="store_true", help="Security Group 유지")
    p.add_argument("--keep-subnet", action="store_true", help="Local Zone 서브넷 유지")
    p.add_argument("--keep-iam", action="store_true", help="SSM IAM Role/Profile 유지")
    p.add_argument("--keep-config", action="store_true", help="config 파일 유지")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    try:
        return destroy(
            region=args.region or "",
            yes=args.yes,
            keep_iam=args.keep_iam,
            keep_sg=args.keep_sg,
            keep_subnet=args.keep_subnet,
            keep_config=args.keep_config,
        )
    except ClientError as exc:
        log(f"AWS API 오류: {exc}")
        return 1
    except Exception as exc:
        log(f"실패: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
