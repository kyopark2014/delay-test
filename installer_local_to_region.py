#!/usr/bin/env python3
"""Local Zone → Region 지연 측정용 EC2 환경 설치.

Chicago Local Zone(송신) + Virginia Region(수신)을 같은 VPC에 올리고,
SG peer(자기참조) + SSM IAM으로 LZ에서 Region private IP로 TCP RTT를 재는 환경을 만듭니다.

실행:
  python installer_local_to_region.py
  python installer_local_to_region.py --force-new
  python installer_local_to_region.py --destroy --yes
  # 또는: python uninstaller_local_to_region.py --yes
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

from installer import (
    TARGETS,
    USER_DATA,
    ensure_local_zone_opted_in,
    get_default_vpc,
    get_my_public_ip,
    get_regional_subnet,
    pick_free_cidr,
    resolve_ami,
    wait_instance_running,
)

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config_local_to_region.json"

DEFAULT_REGION = "us-east-1"
DEFAULT_PORTS = [22, 80, 443]
NAME_PREFIX = "delay-test-l2r"
IAM_ROLE_NAME = f"{NAME_PREFIX}-ssm-role"
IAM_PROFILE_NAME = f"{NAME_PREFIX}-ssm-profile"
SOURCE_ID = "chicago"  # Local Zone (probe 송신)
DEST_ID = "virginia"  # Region (probe 수신)


def log(msg: str) -> None:
    print(msg, flush=True)


def load_config() -> dict[str, Any]:
    if CONFIG_PATH.is_file():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("targets", {})
                return data
        except json.JSONDecodeError as exc:
            log(f"config 파싱 실패, 새로 작성: {exc}")
    return {"region": DEFAULT_REGION, "mode": "local-to-region", "targets": {}}


def save_config(config: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    log(f"config 저장: {CONFIG_PATH}")


def ensure_lz_subnet(
    ec2,
    vpc_id: str,
    az: str,
    *,
    hint_cidr: str | None,
    name: str,
) -> str:
    """Local Zone 서브넷 (Project=delay-test-l2r)."""
    existing = ec2.describe_subnets(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "availability-zone", "Values": [az]},
        ]
    )["Subnets"]
    tagged = [
        s
        for s in existing
        if any(
            t.get("Key") == "Project" and t.get("Value") == NAME_PREFIX
            for t in s.get("Tags", [])
        )
    ]
    if tagged:
        subnet_id = tagged[0]["SubnetId"]
        log(f"기존 LZ 서브넷 재사용: {subnet_id} ({az})")
    elif existing:
        subnet_id = existing[0]["SubnetId"]
        log(f"기존 서브넷 재사용: {subnet_id} ({az})")
    else:
        cidr = pick_free_cidr(ec2, vpc_id, hint_cidr)
        resp = ec2.create_subnet(
            VpcId=vpc_id,
            CidrBlock=cidr,
            AvailabilityZone=az,
            TagSpecifications=[
                {
                    "ResourceType": "subnet",
                    "Tags": [
                        {"Key": "Name", "Value": name},
                        {"Key": "Project", "Value": NAME_PREFIX},
                    ],
                }
            ],
        )
        subnet_id = resp["Subnet"]["SubnetId"]
        log(f"서브넷 생성: {subnet_id} {cidr} @ {az}")

    ec2.modify_subnet_attribute(SubnetId=subnet_id, MapPublicIpOnLaunch={"Value": True})

    rts = ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])[
        "RouteTables"
    ]
    main_rt = None
    for rt in rts:
        for assoc in rt.get("Associations", []):
            if assoc.get("Main"):
                main_rt = rt
                break
    if main_rt is None and rts:
        main_rt = rts[0]
    if main_rt:
        already = any(
            a.get("SubnetId") == subnet_id for a in main_rt.get("Associations", [])
        )
        if not already:
            try:
                ec2.associate_route_table(
                    RouteTableId=main_rt["RouteTableId"], SubnetId=subnet_id
                )
                log(f"라우트 테이블 연결: {main_rt['RouteTableId']} → {subnet_id}")
            except ClientError as exc:
                if "Resource.AlreadyAssociated" not in str(exc):
                    log(f"라우트 연결 경고: {exc}")
    return subnet_id


def ensure_ssm_instance_profile(iam) -> str:
    """SSM용 IAM Role + Instance Profile. 반환: Instance Profile 이름."""
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "ec2.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
    try:
        iam.get_role(RoleName=IAM_ROLE_NAME)
        log(f"IAM Role 재사용: {IAM_ROLE_NAME}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "NoSuchEntity":
            raise
        iam.create_role(
            RoleName=IAM_ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description=f"{NAME_PREFIX}: SSM for LZ-to-Region latency probe",
            Tags=[
                {"Key": "Name", "Value": IAM_ROLE_NAME},
                {"Key": "Project", "Value": NAME_PREFIX},
            ],
        )
        log(f"IAM Role 생성: {IAM_ROLE_NAME}")

    managed = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
    attached = iam.list_attached_role_policies(RoleName=IAM_ROLE_NAME)["AttachedPolicies"]
    if not any(p["PolicyArn"] == managed for p in attached):
        iam.attach_role_policy(RoleName=IAM_ROLE_NAME, PolicyArn=managed)
        log("AmazonSSMManagedInstanceCore 연결")

    try:
        iam.get_instance_profile(InstanceProfileName=IAM_PROFILE_NAME)
        log(f"Instance Profile 재사용: {IAM_PROFILE_NAME}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "NoSuchEntity":
            raise
        iam.create_instance_profile(
            InstanceProfileName=IAM_PROFILE_NAME,
            Tags=[
                {"Key": "Name", "Value": IAM_PROFILE_NAME},
                {"Key": "Project", "Value": NAME_PREFIX},
            ],
        )
        iam.add_role_to_instance_profile(
            InstanceProfileName=IAM_PROFILE_NAME,
            RoleName=IAM_ROLE_NAME,
        )
        log(f"Instance Profile 생성: {IAM_PROFILE_NAME}")
        # EC2가 profile을 인식할 때까지 잠시 대기
        time.sleep(10)

    # Role이 profile에 있는지 확인
    prof = iam.get_instance_profile(InstanceProfileName=IAM_PROFILE_NAME)["InstanceProfile"]
    roles = [r["RoleName"] for r in prof.get("Roles", [])]
    if IAM_ROLE_NAME not in roles:
        iam.add_role_to_instance_profile(
            InstanceProfileName=IAM_PROFILE_NAME,
            RoleName=IAM_ROLE_NAME,
        )
        time.sleep(5)

    return IAM_PROFILE_NAME


def ensure_security_group(
    ec2,
    vpc_id: str,
    my_ip: str,
    ports: list[int],
    existing_sg: str | None,
) -> str:
    group_name = f"{NAME_PREFIX}-sg"
    if existing_sg:
        try:
            ec2.describe_security_groups(GroupIds=[existing_sg])
            log(f"기존 SG 재사용: {existing_sg}")
            _sync_ingress(ec2, existing_sg, my_ip, ports)
            return existing_sg
        except ClientError:
            pass

    found = ec2.describe_security_groups(
        Filters=[
            {"Name": "group-name", "Values": [group_name]},
            {"Name": "vpc-id", "Values": [vpc_id]},
        ]
    )
    if found["SecurityGroups"]:
        sg_id = found["SecurityGroups"][0]["GroupId"]
        log(f"이름 일치 SG 재사용: {sg_id}")
        _sync_ingress(ec2, sg_id, my_ip, ports)
        return sg_id

    resp = ec2.create_security_group(
        GroupName=group_name,
        Description=f"{NAME_PREFIX}: Local Zone to Region latency (SG peer + my IP)",
        VpcId=vpc_id,
        TagSpecifications=[
            {
                "ResourceType": "security-group",
                "Tags": [
                    {"Key": "Name", "Value": group_name},
                    {"Key": "Project", "Value": NAME_PREFIX},
                ],
            }
        ],
    )
    sg_id = resp["GroupId"]
    log(f"Security Group 생성: {sg_id}")
    _sync_ingress(ec2, sg_id, my_ip, ports)
    return sg_id


def _sync_ingress(ec2, sg_id: str, my_ip: str, ports: list[int]) -> None:
    """내 IP(/32) + 동일 SG(self) → TCP ports. LZ↔Region private 통신용."""
    sg = ec2.describe_security_groups(GroupIds=[sg_id])["SecurityGroups"][0]
    existing_cidr = {
        (p["FromPort"], p["ToPort"], r.get("CidrIp"))
        for p in sg.get("IpPermissions", [])
        if p.get("IpProtocol") == "tcp"
        for r in p.get("IpRanges", [])
    }
    existing_sg = {
        (p["FromPort"], p["ToPort"], r.get("GroupId"))
        for p in sg.get("IpPermissions", [])
        if p.get("IpProtocol") == "tcp"
        for r in p.get("UserIdGroupPairs", [])
    }

    to_add: list[dict[str, Any]] = []
    cidr = f"{my_ip}/32"
    for port in ports:
        if (port, port, cidr) not in existing_cidr:
            to_add.append(
                {
                    "IpProtocol": "tcp",
                    "FromPort": port,
                    "ToPort": port,
                    "IpRanges": [
                        {"CidrIp": cidr, "Description": f"{NAME_PREFIX} from operator IP"}
                    ],
                }
            )
        if (port, port, sg_id) not in existing_sg:
            to_add.append(
                {
                    "IpProtocol": "tcp",
                    "FromPort": port,
                    "ToPort": port,
                    "UserIdGroupPairs": [
                        {
                            "GroupId": sg_id,
                            "Description": f"{NAME_PREFIX} LZ-Region peer",
                        }
                    ],
                }
            )
    if to_add:
        ec2.authorize_security_group_ingress(GroupId=sg_id, IpPermissions=to_add)
        log(f"인바운드: {cidr} + SG-self → TCP {ports}")
    else:
        log(f"인바운드 규칙 이미 존재: {cidr} + SG-self → TCP {ports}")


def launch_instance(
    ec2,
    *,
    ami_id: str,
    instance_type: str,
    subnet_id: str,
    sg_id: str,
    iam_profile: str,
    target_id: str,
    label: str,
    role: str,
) -> dict:
    resp = ec2.run_instances(
        ImageId=ami_id,
        InstanceType=instance_type,
        MinCount=1,
        MaxCount=1,
        UserData=USER_DATA,
        IamInstanceProfile={"Name": iam_profile},
        TagSpecifications=[
            {
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "Name", "Value": f"{NAME_PREFIX}-{target_id}"},
                    {"Key": "Project", "Value": NAME_PREFIX},
                    {"Key": "Target", "Value": target_id},
                    {"Key": "Role", "Value": role},
                    {"Key": "Label", "Value": label[:256]},
                ],
            }
        ],
        NetworkInterfaces=[
            {
                "DeviceIndex": 0,
                "SubnetId": subnet_id,
                "Groups": [sg_id],
                "AssociatePublicIpAddress": True,
            }
        ],
        MetadataOptions={"HttpTokens": "required", "HttpPutResponseHopLimit": 2},
    )
    return resp["Instances"][0]


def find_running_by_tag(ec2, target_id: str) -> dict | None:
    resp = ec2.describe_instances(
        Filters=[
            {"Name": "tag:Project", "Values": [NAME_PREFIX]},
            {"Name": "tag:Target", "Values": [target_id]},
            {"Name": "instance-state-name", "Values": ["running"]},
        ]
    )
    for res in resp.get("Reservations", []):
        for inst in res.get("Instances", []):
            return inst
    return None


def install_one(
    ec2,
    *,
    target_id: str,
    role: str,
    ports: list[int],
    vpc_id: str,
    sg_id: str,
    ami_id: str,
    iam_profile: str,
    force_new: bool,
    instance_type_override: str | None,
    existing: dict[str, Any] | None,
) -> dict[str, Any]:
    spec = TARGETS[target_id]
    label = spec["label"]
    instance_type = instance_type_override or spec["instance_type"]
    region = spec["region"]

    log(f"\n======= {role}: {target_id} — {label} =======")
    log(f"kind={spec['kind']} instance_type={instance_type}")

    if spec["kind"] == "local-zone":
        az = ensure_local_zone_opted_in(ec2, spec["az"] or spec["zone_name"])
        subnet_id = ensure_lz_subnet(
            ec2,
            vpc_id,
            az,
            hint_cidr=spec.get("subnet_cidr_hint"),
            name=f"{NAME_PREFIX}-{target_id}-subnet",
        )
    else:
        subnet_id, az = get_regional_subnet(ec2, vpc_id)
        log(f"리전 서브넷: {subnet_id} @ {az}")

    if not force_new:
        # config 또는 태그로 기존 running 재사용
        reuse_id = (existing or {}).get("instance_id")
        inst = None
        if reuse_id:
            try:
                resp = ec2.describe_instances(InstanceIds=[reuse_id])
                cand = resp["Reservations"][0]["Instances"][0]
                if cand["State"]["Name"] == "running":
                    inst = cand
            except ClientError:
                pass
        if inst is None:
            inst = find_running_by_tag(ec2, target_id)
        if inst and inst.get("PrivateIpAddress"):
            entry = _entry_from_instance(
                inst,
                target_id=target_id,
                label=label,
                kind=spec["kind"],
                region=region,
                role=role,
                ports=ports,
                vpc_id=vpc_id,
                sg_id=sg_id,
                ami_id=ami_id,
                instance_type=instance_type,
            )
            log(
                f"기존 인스턴스 재사용: {entry['instance_id']} "
                f"private={entry['private_ip']} public={entry.get('public_ip')}"
            )
            return entry

    inst = launch_instance(
        ec2,
        ami_id=ami_id,
        instance_type=instance_type,
        subnet_id=subnet_id,
        sg_id=sg_id,
        iam_profile=iam_profile,
        target_id=target_id,
        label=label,
        role=role,
    )
    instance_id = inst["InstanceId"]
    log(f"인스턴스 생성: {instance_id}")
    inst = wait_instance_running(ec2, instance_id)
    entry = _entry_from_instance(
        inst,
        target_id=target_id,
        label=label,
        kind=spec["kind"],
        region=region,
        role=role,
        ports=ports,
        vpc_id=vpc_id,
        sg_id=sg_id,
        ami_id=ami_id,
        instance_type=instance_type,
        subnet_id=subnet_id,
        az=az,
    )
    log(
        f"배포 완료: {label} → private={entry['private_ip']} public={entry.get('public_ip')}"
    )
    return entry


def _entry_from_instance(
    inst: dict,
    *,
    target_id: str,
    label: str,
    kind: str,
    region: str,
    role: str,
    ports: list[int],
    vpc_id: str,
    sg_id: str,
    ami_id: str,
    instance_type: str,
    subnet_id: str | None = None,
    az: str | None = None,
) -> dict[str, Any]:
    return {
        "id": target_id,
        "label": label,
        "kind": kind,
        "role": role,
        "region": region,
        "az": inst.get("Placement", {}).get("AvailabilityZone", az),
        "instance_id": inst["InstanceId"],
        "instance_type": inst.get("InstanceType", instance_type),
        "ami_id": inst.get("ImageId", ami_id),
        "vpc_id": vpc_id,
        "subnet_id": inst.get("SubnetId", subnet_id),
        "security_group_id": sg_id,
        "private_ip": inst.get("PrivateIpAddress", ""),
        "public_ip": inst.get("PublicIpAddress", ""),
        "public_dns": inst.get("PublicDnsName", ""),
        "ports": ports,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def wait_ssm_online(ssm, instance_id: str, timeout: float = 300.0) -> bool:
    log(f"SSM Online 대기: {instance_id} (최대 {timeout:.0f}s)")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = ssm.describe_instance_information(
                Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
            )
            infos = resp.get("InstanceInformationList") or []
            if infos and infos[0].get("PingStatus") == "Online":
                log(f"SSM Online: {instance_id}")
                return True
        except ClientError as exc:
            log(f"  SSM 조회 경고: {exc}")
        time.sleep(8)
    log(f"경고: SSM Online 대기 시간 초과 — {instance_id}")
    return False


def install(args: argparse.Namespace) -> int:
    region = args.region or DEFAULT_REGION
    ports = [int(p.strip()) for p in args.ports.split(",") if p.strip()]

    try:
        session = boto3.Session(region_name=region)
        ec2 = session.client("ec2")
        iam = session.client("iam")
        ssm = session.client("ssm")
        ident = session.client("sts").get_caller_identity()
        log(f"AWS Account: {ident['Account']} / {ident['Arn']}")
    except NoCredentialsError:
        log("AWS 자격 증명이 없습니다.")
        return 1

    if args.destroy:
        from uninstaller_local_to_region import destroy as uninstall_destroy

        if not args.yes:
            log("--yes 없이 destroy를 실행할 수 없습니다.")
            return 1
        return uninstall_destroy(
            region=region,
            yes=True,
            keep_iam=args.keep_iam,
        )

    log("=== Local Zone → Region 지연 측정 환경 설치 ===")
    log(f"송신(source): {SOURCE_ID} Local Zone")
    log(f"수신(dest):   {DEST_ID} Region")

    my_ip = get_my_public_ip()
    log(f"내 공인 IP: {my_ip}")

    config = load_config()
    vpc_id = args.vpc_id or config.get("vpc_id") or get_default_vpc(ec2)
    log(f"VPC: {vpc_id}")

    ami_id = args.ami or resolve_ami(ec2)
    log(f"AMI: {ami_id}")

    iam_profile = ensure_ssm_instance_profile(iam)
    sg_id = ensure_security_group(
        ec2,
        vpc_id,
        my_ip,
        ports,
        config.get("security_group_id"),
    )

    # Region(수신) 먼저, 그다음 Local Zone(송신)
    dest = install_one(
        ec2,
        target_id=DEST_ID,
        role="dest",
        ports=ports,
        vpc_id=vpc_id,
        sg_id=sg_id,
        ami_id=ami_id,
        iam_profile=iam_profile,
        force_new=args.force_new,
        instance_type_override=args.instance_type_region,
        existing=(config.get("targets") or {}).get(DEST_ID),
    )
    source = install_one(
        ec2,
        target_id=SOURCE_ID,
        role="source",
        ports=ports,
        vpc_id=vpc_id,
        sg_id=sg_id,
        ami_id=ami_id,
        iam_profile=iam_profile,
        force_new=args.force_new,
        instance_type_override=args.instance_type_lz,
        existing=(config.get("targets") or {}).get(SOURCE_ID),
    )

    # probe는 source(Chicago)에서 SSM으로 실행
    wait_ssm_online(ssm, source["instance_id"], timeout=args.ssm_wait)
    wait_ssm_online(ssm, dest["instance_id"], timeout=min(120.0, args.ssm_wait))

    config = {
        "mode": "local-to-region",
        "region": region,
        "my_ip": my_ip,
        "vpc_id": vpc_id,
        "security_group_id": sg_id,
        "iam_instance_profile": iam_profile,
        "ports": ports,
        "source_id": SOURCE_ID,
        "dest_id": DEST_ID,
        "path": {
            "from": SOURCE_ID,
            "to": DEST_ID,
            "preferred_host": "private_ip",
            "note": "Chicago Local Zone → Virginia Region (VPC private)",
        },
        "targets": {
            DEST_ID: dest,
            SOURCE_ID: source,
        },
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    save_config(config)

    log("\n=== 설치 요약 ===")
    log(
        f"  [source/{SOURCE_ID}] {source.get('az')}  "
        f"private={source.get('private_ip')}  public={source.get('public_ip')}"
    )
    log(
        f"  [dest/{DEST_ID}]   {dest.get('az')}  "
        f"private={dest.get('private_ip')}  public={dest.get('public_ip')}"
    )
    log("\n측정: python delaytest_local_to_region.py")
    log("삭제: python uninstaller_local_to_region.py --yes")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Local Zone → Region 지연 측정용 EC2 설치 (Chicago LZ → Virginia)"
    )
    p.add_argument("--region", default=DEFAULT_REGION)
    p.add_argument("--ports", default=",".join(str(x) for x in DEFAULT_PORTS))
    p.add_argument("--vpc-id", default=None)
    p.add_argument("--ami", default=None)
    p.add_argument("--force-new", action="store_true")
    p.add_argument(
        "--instance-type-lz",
        default=None,
        help="Local Zone(Chicago) 인스턴스 타입 (기본 c6i.large)",
    )
    p.add_argument(
        "--instance-type-region",
        default=None,
        help="Region(Virginia) 인스턴스 타입 (기본 t3.micro)",
    )
    p.add_argument("--ssm-wait", type=float, default=300.0, help="SSM Online 대기(초)")
    p.add_argument("--destroy", action="store_true")
    p.add_argument("--yes", "-y", action="store_true")
    p.add_argument("--keep-iam", action="store_true", help="destroy 시 IAM 유지")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    try:
        return install(args)
    except ClientError as exc:
        log(f"AWS API 오류: {exc}")
        return 1
    except Exception as exc:
        log(f"실패: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
