#!/usr/bin/env python3
"""tcpping 지연 측정용 EC2 설치.

설치 옵션(--target):
  1) virginia — Region (Virginia, us-east-1)
  2) chicago  — Local Zone (Chicago, us-east-1-chi-2a)

실행:
  python installer.py --target virginia
  python installer.py --target chicago
  python installer.py --target all
  python installer.py --list-targets
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"

DEFAULT_REGION = "us-east-1"
DEFAULT_PORTS = [22, 80, 443]
NAME_PREFIX = "delay-test"

# 설치 옵션 카탈로그
TARGETS: dict[str, dict[str, Any]] = {
    "virginia": {
        "id": "virginia",
        "label": "Region (Virginia, us-east-1)",
        "kind": "region",
        "region": "us-east-1",
        "az": None,  # default VPC의 regional AZ 자동 선택
        "zone_name": None,
        "instance_type": "t3.micro",
        "location_note": "시험: 서울 → Virginia Region",
    },
    "chicago": {
        "id": "chicago",
        "label": "Local Zone (Chicago)",
        "kind": "local-zone",
        "region": "us-east-1",
        "az": "us-east-1-chi-2a",
        "zone_name": "us-east-1-chi-2",
        "instance_type": "c6i.large",  # Chicago LZ x86 (t3 미제공)
        "location_note": "시험: 서울 → Chicago Local Zone",
        "subnet_cidr_hint": "172.31.96.0/20",
    },
}


USER_DATA = """#!/bin/bash
set -eux
mkdir -p /var/www/delay-test
echo "delay-test ok" > /var/www/delay-test/index.html
nohup python3 -m http.server 80 --bind 0.0.0.0 --directory /var/www/delay-test \
  >/var/log/delay-http.log 2>&1 &
cat >/etc/systemd/system/delay-http.service <<'EOF'
[Unit]
Description=delay-test HTTP on :80
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 -m http.server 80 --bind 0.0.0.0 --directory /var/www/delay-test
Restart=always

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now delay-http.service || true
"""


def log(msg: str) -> None:
    print(msg, flush=True)


def load_config() -> dict[str, Any]:
    if CONFIG_PATH.is_file():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return migrate_config(data)
        except json.JSONDecodeError as exc:
            log(f"config.json 파싱 실패, 새로 작성합니다: {exc}")
    return {"region": DEFAULT_REGION, "targets": {}}


def migrate_config(data: dict[str, Any]) -> dict[str, Any]:
    """구형 flat config → targets.virginia 로 승격."""
    if "targets" in data and isinstance(data["targets"], dict):
        return data
    if data.get("instance_id"):
        target = {
            "id": "virginia",
            "label": TARGETS["virginia"]["label"],
            "kind": "region",
            "region": data.get("region", DEFAULT_REGION),
            "az": data.get("az"),
            "instance_id": data.get("instance_id"),
            "instance_type": data.get("instance_type"),
            "ami_id": data.get("ami_id"),
            "vpc_id": data.get("vpc_id"),
            "subnet_id": data.get("subnet_id"),
            "security_group_id": data.get("security_group_id"),
            "public_ip": data.get("public_ip"),
            "public_dns": data.get("public_dns"),
            "ports": data.get("ports", DEFAULT_PORTS),
            "created_at": data.get("created_at"),
        }
        return {
            "region": data.get("region", DEFAULT_REGION),
            "my_ip": data.get("my_ip"),
            "ports": data.get("ports", DEFAULT_PORTS),
            "security_group_id": data.get("security_group_id"),
            "vpc_id": data.get("vpc_id"),
            "targets": {"virginia": target},
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    data.setdefault("targets", {})
    return data


def save_config(config: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    log(f"config 저장: {CONFIG_PATH}")


def get_my_public_ip() -> str:
    with urllib.request.urlopen("https://checkip.amazonaws.com", timeout=10) as resp:
        return resp.read().decode("utf-8").strip()


def resolve_ami(ec2) -> str:
    result = ec2.describe_images(
        Owners=["amazon"],
        Filters=[
            {"Name": "name", "Values": ["al2023-ami-*-kernel-*-x86_64"]},
            {"Name": "architecture", "Values": ["x86_64"]},
            {"Name": "state", "Values": ["available"]},
            {"Name": "virtualization-type", "Values": ["hvm"]},
        ],
    )
    images = sorted(result["Images"], key=lambda i: i.get("CreationDate", ""), reverse=True)
    if not images:
        raise RuntimeError("Amazon Linux 2023 AMI를 찾지 못했습니다.")
    return images[0]["ImageId"]


def get_default_vpc(ec2) -> str:
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])
    if not vpcs["Vpcs"]:
        raise RuntimeError("default VPC가 없습니다. --vpc-id를 지정하세요.")
    return vpcs["Vpcs"][0]["VpcId"]


def ensure_local_zone_opted_in(ec2, zone_name: str) -> str:
    """Local Zone 그룹 옵트인. zone_name 예: us-east-1-chi-2. 반환: AZ name."""
    azs = ec2.describe_availability_zones(
        AllAvailabilityZones=True,
        Filters=[{"Name": "zone-type", "Values": ["local-zone"]}],
    )["AvailabilityZones"]
    match = [z for z in azs if z.get("ZoneName", "").startswith(zone_name) or z.get("GroupName") == zone_name]
    if not match:
        # 직접 AZ 이름으로 조회
        match = [z for z in azs if z.get("ZoneName") == zone_name]
    if not match:
        raise RuntimeError(f"Local Zone를 찾을 수 없습니다: {zone_name}")

    z = match[0]
    az_name = z["ZoneName"]
    status = z.get("OptInStatus", "")
    group = z.get("GroupName") or zone_name
    if status == "not-opted-in":
        log(f"Local Zone 옵트인 요청: {group}")
        ec2.modify_availability_zone_group(GroupName=group, OptInStatus="opted-in")
        # 상태 반영 대기
        for _ in range(30):
            time.sleep(5)
            again = ec2.describe_availability_zones(
                AllAvailabilityZones=True,
                ZoneNames=[az_name],
            )["AvailabilityZones"][0]
            if again.get("OptInStatus") == "opted-in" and again.get("State") == "available":
                log(f"Local Zone available: {az_name}")
                return az_name
            log(f"  대기 중... OptInStatus={again.get('OptInStatus')} State={again.get('State')}")
        raise RuntimeError(f"Local Zone 옵트인 대기 시간 초과: {az_name}")
    if z.get("State") != "available":
        raise RuntimeError(f"Local Zone 사용 불가: {az_name} state={z.get('State')}")
    log(f"Local Zone OK: {az_name} (opt-in={status})")
    return az_name


def pick_free_cidr(ec2, vpc_id: str, hint: str | None = None) -> str:
    vpc = ec2.describe_vpcs(VpcIds=[vpc_id])["Vpcs"][0]
    vpc_net = ipaddress.ip_network(vpc["CidrBlock"])
    used = [
        ipaddress.ip_network(s["CidrBlock"])
        for s in ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]
    ]
    if hint:
        candidate = ipaddress.ip_network(hint)
        if candidate.subnet_of(vpc_net) and not any(candidate.overlaps(u) for u in used):
            return str(candidate)
    # /20 슬라이스 탐색
    for subnet in vpc_net.subnets(new_prefix=20):
        if not any(subnet.overlaps(u) for u in used):
            return str(subnet)
    raise RuntimeError(f"VPC {vpc_id}에 남는 /20 CIDR이 없습니다.")


def ensure_subnet_in_az(
    ec2,
    vpc_id: str,
    az: str,
    *,
    hint_cidr: str | None = None,
    name: str,
) -> str:
    existing = ec2.describe_subnets(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "availability-zone", "Values": [az]},
        ]
    )["Subnets"]
    # 프로젝트 태그 우선
    tagged = [
        s
        for s in existing
        if any(t.get("Key") == "Project" and t.get("Value") == NAME_PREFIX for t in s.get("Tags", []))
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

    # main route table에 연결 (IGW 라우트 필요)
    rts = ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["RouteTables"]
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
            # 명시 associate (default VPC main은 암묵 연결일 수 있음)
            try:
                ec2.associate_route_table(RouteTableId=main_rt["RouteTableId"], SubnetId=subnet_id)
                log(f"라우트 테이블 연결: {main_rt['RouteTableId']} → {subnet_id}")
            except ClientError as exc:
                # 이미 연결되어 있으면 무시
                if "Resource.AlreadyAssociated" not in str(exc):
                    log(f"라우트 연결 경고: {exc}")

    return subnet_id


def get_regional_subnet(ec2, vpc_id: str) -> tuple[str, str]:
    subnets = ec2.describe_subnets(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "default-for-az", "Values": ["true"]},
        ]
    )["Subnets"]
    # Local Zone 제외 (이름에 하이픈 도시 코드)
    regional = [s for s in subnets if s["AvailabilityZone"].count("-") == 2]
    if not regional:
        regional = [
            s
            for s in ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]
            if s["AvailabilityZone"].count("-") == 2
        ]
    if not regional:
        raise RuntimeError("리전 AZ 서브넷을 찾지 못했습니다.")
    public = [s for s in regional if s.get("MapPublicIpOnLaunch")]
    chosen = public[0] if public else regional[0]
    return chosen["SubnetId"], chosen["AvailabilityZone"]


def ensure_security_group(ec2, vpc_id: str, my_ip: str, ports: list[int], existing_sg: str | None) -> str:
    cidr = f"{my_ip}/32"
    group_name = f"{NAME_PREFIX}-sg"

    if existing_sg:
        try:
            ec2.describe_security_groups(GroupIds=[existing_sg])
            log(f"기존 Security Group 재사용: {existing_sg}")
            _sync_ingress(ec2, existing_sg, cidr, ports)
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
        _sync_ingress(ec2, sg_id, cidr, ports)
        return sg_id

    resp = ec2.create_security_group(
        GroupName=group_name,
        Description=f"{NAME_PREFIX}: tcpping latency test",
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
    _sync_ingress(ec2, sg_id, cidr, ports)
    return sg_id


def _sync_ingress(ec2, sg_id: str, cidr: str, ports: list[int]) -> None:
    sg = ec2.describe_security_groups(GroupIds=[sg_id])["SecurityGroups"][0]
    existing = {
        (p["FromPort"], p["ToPort"], r.get("CidrIp"))
        for p in sg.get("IpPermissions", [])
        if p.get("IpProtocol") == "tcp"
        for r in p.get("IpRanges", [])
    }
    to_add = []
    for port in ports:
        if (port, port, cidr) not in existing:
            to_add.append(
                {
                    "IpProtocol": "tcp",
                    "FromPort": port,
                    "ToPort": port,
                    "IpRanges": [{"CidrIp": cidr, "Description": f"{NAME_PREFIX} from my IP"}],
                }
            )
    if to_add:
        ec2.authorize_security_group_ingress(GroupId=sg_id, IpPermissions=to_add)
        log(f"인바운드 허용: {cidr} → TCP {ports}")
    else:
        log(f"인바운드 규칙 이미 존재: {cidr} → TCP {ports}")


def wait_instance_running(ec2, instance_id: str) -> dict:
    log(f"인스턴스 running 대기: {instance_id}")
    ec2.get_waiter("instance_running").wait(
        InstanceIds=[instance_id],
        WaiterConfig={"Delay": 5, "MaxAttempts": 60},
    )
    deadline = time.time() + 90
    while time.time() < deadline:
        resp = ec2.describe_instances(InstanceIds=[instance_id])
        inst = resp["Reservations"][0]["Instances"][0]
        if inst.get("PublicIpAddress"):
            return inst
        time.sleep(3)
    resp = ec2.describe_instances(InstanceIds=[instance_id])
    return resp["Reservations"][0]["Instances"][0]


def launch_instance(
    ec2,
    *,
    ami_id: str,
    instance_type: str,
    subnet_id: str,
    sg_id: str,
    target_id: str,
    label: str,
) -> dict:
    resp = ec2.run_instances(
        ImageId=ami_id,
        InstanceType=instance_type,
        MinCount=1,
        MaxCount=1,
        UserData=USER_DATA,
        TagSpecifications=[
            {
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "Name", "Value": f"{NAME_PREFIX}-{target_id}"},
                    {"Key": "Project", "Value": NAME_PREFIX},
                    {"Key": "Target", "Value": target_id},
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


def install_target(
    ec2,
    config: dict[str, Any],
    target_id: str,
    *,
    ports: list[int],
    force_new: bool,
    instance_type_override: str | None,
    vpc_id_override: str | None,
    ami_override: str | None,
) -> dict[str, Any]:
    spec = TARGETS[target_id]
    region = spec["region"]
    label = spec["label"]
    instance_type = instance_type_override or spec["instance_type"]

    log(f"\n======= 설치: {target_id} — {label} =======")
    log(f"kind={spec['kind']} instance_type={instance_type}")

    my_ip = get_my_public_ip()
    log(f"내 공인 IP: {my_ip}")

    vpc_id = vpc_id_override or config.get("vpc_id") or get_default_vpc(ec2)
    log(f"VPC: {vpc_id}")

    if spec["kind"] == "local-zone":
        az = ensure_local_zone_opted_in(ec2, spec["az"] or spec["zone_name"])
        subnet_id = ensure_subnet_in_az(
            ec2,
            vpc_id,
            az,
            hint_cidr=spec.get("subnet_cidr_hint"),
            name=f"{NAME_PREFIX}-{target_id}-subnet",
        )
    else:
        subnet_id, az = get_regional_subnet(ec2, vpc_id)
        log(f"리전 서브넷: {subnet_id} @ {az}")

    ami_id = ami_override or resolve_ami(ec2)
    log(f"AMI: {ami_id}")

    sg_id = ensure_security_group(
        ec2,
        vpc_id,
        my_ip,
        ports,
        config.get("security_group_id")
        or (config.get("targets") or {}).get(target_id, {}).get("security_group_id"),
    )

    existing = (config.get("targets") or {}).get(target_id) or {}
    existing_id = existing.get("instance_id")
    if existing_id and not force_new:
        try:
            resp = ec2.describe_instances(InstanceIds=[existing_id])
            inst = resp["Reservations"][0]["Instances"][0]
            if inst["State"]["Name"] == "running" and inst.get("PublicIpAddress"):
                _sync_ingress(ec2, sg_id, f"{my_ip}/32", ports)
                entry = {
                    **existing,
                    "id": target_id,
                    "label": label,
                    "kind": spec["kind"],
                    "region": region,
                    "az": inst["Placement"]["AvailabilityZone"],
                    "instance_id": existing_id,
                    "instance_type": inst.get("InstanceType", instance_type),
                    "ami_id": inst.get("ImageId", ami_id),
                    "vpc_id": vpc_id,
                    "subnet_id": inst["SubnetId"],
                    "security_group_id": sg_id,
                    "public_ip": inst["PublicIpAddress"],
                    "public_dns": inst.get("PublicDnsName", ""),
                    "ports": ports,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                log(f"기존 인스턴스 재사용: {existing_id} ({inst['PublicIpAddress']})")
                return {"entry": entry, "my_ip": my_ip, "vpc_id": vpc_id, "sg_id": sg_id}
        except ClientError:
            pass

    inst = launch_instance(
        ec2,
        ami_id=ami_id,
        instance_type=instance_type,
        subnet_id=subnet_id,
        sg_id=sg_id,
        target_id=target_id,
        label=label,
    )
    instance_id = inst["InstanceId"]
    log(f"인스턴스 생성: {instance_id}")
    inst = wait_instance_running(ec2, instance_id)
    public_ip = inst.get("PublicIpAddress", "")
    public_dns = inst.get("PublicDnsName", "")
    if not public_ip:
        log("경고: 퍼블릭 IP 미할당 — 서브넷/IGW/Local Zone 라우팅을 확인하세요.")

    entry = {
        "id": target_id,
        "label": label,
        "kind": spec["kind"],
        "region": region,
        "az": inst.get("Placement", {}).get("AvailabilityZone", az),
        "instance_id": instance_id,
        "instance_type": instance_type,
        "ami_id": ami_id,
        "vpc_id": vpc_id,
        "subnet_id": subnet_id,
        "security_group_id": sg_id,
        "public_ip": public_ip,
        "public_dns": public_dns,
        "ports": ports,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    log(f"배포 완료: {label} → {public_ip}")
    return {"entry": entry, "my_ip": my_ip, "vpc_id": vpc_id, "sg_id": sg_id}


def list_targets() -> None:
    log("설치 옵션 (--target):")
    for i, (key, spec) in enumerate(TARGETS.items(), start=1):
        log(f"  {i}) {key:10} — {spec['label']}")
        log(f"      kind={spec['kind']}  default_instance={spec['instance_type']}")
        if spec.get("az"):
            log(f"      az={spec['az']}")


def install(args: argparse.Namespace) -> int:
    if args.list_targets:
        list_targets()
        return 0

    target_arg = (args.target or "virginia").lower()
    if target_arg == "all":
        target_ids = list(TARGETS.keys())
    elif target_arg in TARGETS:
        target_ids = [target_arg]
    else:
        log(f"알 수 없는 --target: {target_arg}")
        list_targets()
        return 1

    ports = [int(p.strip()) for p in args.ports.split(",") if p.strip()]
    region = TARGETS[target_ids[0]]["region"]

    try:
        session = boto3.Session(region_name=region)
        ec2 = session.client("ec2")
        ident = session.client("sts").get_caller_identity()
        log(f"AWS Account: {ident['Account']} / {ident['Arn']}")
    except NoCredentialsError:
        log("AWS 자격 증명이 없습니다.")
        return 1

    if args.destroy:
        from uninstaller import destroy as uninstall_destroy

        return uninstall_destroy(
            region=region,
            delete_sg=not args.keep_sg,
            yes=True,
            keep_config=False,
            target=target_arg if target_arg != "all" else "all",
        )

    config = load_config()
    config.setdefault("targets", {})

    for tid in target_ids:
        result = install_target(
            ec2,
            config,
            tid,
            ports=ports,
            force_new=args.force_new,
            instance_type_override=args.instance_type,
            vpc_id_override=args.vpc_id,
            ami_override=args.ami,
        )
        config["targets"][tid] = result["entry"]
        config["region"] = region
        config["my_ip"] = result["my_ip"]
        config["vpc_id"] = result["vpc_id"]
        config["security_group_id"] = result["sg_id"]
        config["ports"] = ports
        config["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_config(config)

    log("\n=== 설치 요약 ===")
    for tid, t in config["targets"].items():
        if tid in target_ids or target_arg == "all":
            log(f"  [{tid}] {t.get('label')}  {t.get('public_ip')}  ({t.get('az')})")
    log("\n측정: python delaytest.py --target all")
    log("삭제: python uninstaller.py --yes")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="tcpping 지연 테스트용 EC2 설치")
    p.add_argument(
        "--target",
        default="virginia",
        help="virginia | chicago | all  (1=Virginia Region, 2=Chicago Local Zone)",
    )
    p.add_argument("--list-targets", action="store_true")
    p.add_argument("--instance-type", default=None, help="타겟 기본 타입 덮어쓰기")
    p.add_argument("--ports", default=",".join(str(x) for x in DEFAULT_PORTS))
    p.add_argument("--vpc-id", default=None)
    p.add_argument("--ami", default=None)
    p.add_argument("--force-new", action="store_true")
    p.add_argument("--destroy", action="store_true", help="(호환) uninstaller 호출")
    p.add_argument("--keep-sg", action="store_true")
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
