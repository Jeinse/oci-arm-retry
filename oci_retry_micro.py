"""Single-attempt E2.Micro runner, derived from WendyChen330/oci-arm-retry.

The OCI write surface is restricted to a capacity report and one launch call.
No local execution of this file creates an instance: only a manually armed
GitHub Actions campaign can reach launch().
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
import uuid

REGION = "ap-singapore-1"
SHAPE = "VM.Standard.E2.1.Micro"
NAME = "e2-micro-singapore-2"
BOOT_GB = 50
BOOT_VPUS = 10
STORAGE_GB_LIMIT = 200
INTERVAL_SECONDS = 1800
CONFIRMATION = "CREATE_ONE_E2_MICRO"
MANAGED_BY = "jeinse-e2-micro-retry"


class GuardError(RuntimeError):
    pass


def require(condition, code):
    if not condition:
        raise GuardError(code)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def initial_state():
    return {"version": 1, "mode": "paused", "attempts": 0,
            "next_allowed_at": 0, "launch_pending": False}


def classify_error(error):
    status = getattr(error, "status", None)
    code = str(getattr(error, "code", ""))
    message = str(getattr(error, "message", "")).strip().lower().rstrip(".")
    if status == 500 and code in {"InternalError", "OutOfHostCapacity"} and message == "out of host capacity":
        return "capacity"
    if status == 429:
        return "throttled"
    return "halt"


def safe_error(error):
    # Do not emit exception messages, response bodies, configuration or keys.
    result = {"type": type(error).__name__}
    for attr in ("status", "code", "request_id"):
        value = getattr(error, attr, None)
        if value is not None:
            result[attr] = str(value)[:100]
    if type(error).__name__ == "GuardError" and re.fullmatch(r"[A-Z0-9_]{1,100}", str(error)):
        result["guard"] = str(error)
    return result


@dataclass(frozen=True)
class Settings:
    tenancy: str
    user: str
    fingerprint: str
    key: str
    compartment: str
    subnet: str
    image: str
    ssh_key: str
    campaign: str
    free_approved: bool
    pass_phrase: str = ""
    image_mode: str = "platform"

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        required = ["OCI_TENANCY", "OCI_USER", "OCI_FINGERPRINT", "OCI_PRIVATE_KEY",
                    "OCI_SUBNET_ID", "OCI_IMAGE_ID", "OCI_SSH_PUBLIC_KEY", "GITHUB_REPOSITORY"]
        require(all(env.get(k, "").strip() for k in required), "MISSING_SETTINGS_OR_SECRETS")
        require(env.get("OCI_REGION", REGION) == REGION, "ONLY_SINGAPORE_ALLOWED")
        tenancy = env["OCI_TENANCY"].strip()
        require(tenancy.startswith("ocid1.tenancy.oc1."), "INVALID_TENANCY")
        compartment = env.get("OCI_COMPARTMENT_ID", "").strip() or tenancy
        require(compartment.startswith(("ocid1.tenancy.oc1.", "ocid1.compartment.oc1.")), "INVALID_COMPARTMENT")
        for key, kind in (("OCI_SUBNET_ID", "subnet"), ("OCI_IMAGE_ID", "image")):
            require(env[key].startswith(f"ocid1.{kind}.oc1.{REGION}."), "WRONG_REGION_OR_RESOURCE_TYPE")
        image_mode = env.get("OCI_IMAGE_MODE", "").strip().lower() or "platform"
        require(image_mode in {"platform", "custom"}, "INVALID_IMAGE_MODE")
        ssh_key = env["OCI_SSH_PUBLIC_KEY"].strip()
        require("\n" not in ssh_key and "PRIVATE KEY" not in ssh_key, "SSH_PUBLIC_KEY_REQUIRED")
        require(bool(re.fullmatch(r"(?:ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)) [A-Za-z0-9+/=]+(?: [^\r\n]*)?", ssh_key)), "INVALID_SSH_PUBLIC_KEY")
        from cryptography.hazmat.primitives.serialization import load_ssh_public_key
        try:
            load_ssh_public_key(ssh_key.encode())
        except (ValueError, TypeError):
            raise GuardError("INVALID_SSH_PUBLIC_KEY_ENCODING") from None
        require(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", env["GITHUB_REPOSITORY"]) is not None, "INVALID_GITHUB_REPOSITORY")
        return cls(tenancy, env["OCI_USER"].strip(), env["OCI_FINGERPRINT"].strip(),
                   env["OCI_PRIVATE_KEY"], compartment, env["OCI_SUBNET_ID"].strip(),
                   env["OCI_IMAGE_ID"].strip(), ssh_key,
                   digest(env["GITHUB_REPOSITORY"].lower() + ":" + NAME)[:32],
                   env.get("OCI_FREE_CONFIG_APPROVED", "").lower() == "true",
                   env.get("OCI_KEY_PASSPHRASE", ""), image_mode=image_mode)

    def intent_hash(self):
        return digest(json.dumps({"tenancy": self.tenancy, "compartment": self.compartment,
            "subnet": self.subnet, "image": self.image, "image_mode": self.image_mode,
            "ssh": self.ssh_key,
            "shape": SHAPE, "boot_gb": BOOT_GB, "boot_vpus": BOOT_VPUS,
            "campaign": self.campaign}, sort_keys=True))


class OciGateway:
    def __init__(self, settings):
        import oci
        self.oci, self.settings = oci, settings
        self.no_retry = oci.retry.NoneRetryStrategy()
        signer = oci.signer.Signer(tenancy=settings.tenancy, user=settings.user,
            fingerprint=settings.fingerprint, private_key_file_location=None,
            private_key_content=settings.key, pass_phrase=settings.pass_phrase or None)
        config = {"region": REGION, "tenancy": settings.tenancy,
                  "user": settings.user, "fingerprint": settings.fingerprint,
                  "key_content": settings.key}
        options = {"signer": signer, "retry_strategy": self.no_retry, "timeout": (10, 30)}
        self.identity = oci.identity.IdentityClient(config, **options)
        self.compute = oci.core.ComputeClient(config, **options)
        self.network = oci.core.VirtualNetworkClient(config, **options)
        self.block = oci.core.BlockstorageClient(config, **options)
        self.limits = oci.limits.LimitsClient(config, **options)

    def all(self, method, *args, **kwargs):
        # The SDK pagination helper adds an outer DEFAULT_RETRY_STRATEGY even
        # when each API call receives NoneRetryStrategy. Follow pages directly
        # so a permission error or 429 is surfaced immediately.
        items, seen = [], set()
        for _ in range(1000):
            response = method(*args, **kwargs, retry_strategy=self.no_retry)
            require(isinstance(response.data, list), "UNKNOWN_LIST_RESPONSE")
            items.extend(response.data)
            page = response.headers.get("opc-next-page")
            if not page:
                return items
            require(page not in seen, "PAGINATION_TOKEN_REPEATED")
            seen.add(page)
            kwargs["page"] = page
        raise GuardError("PAGINATION_LIMIT_EXCEEDED")

    def inspect_image(self, compartment_ids, ad):
        s = self.settings
        require(s.image_mode in {"platform", "custom"}, "INVALID_IMAGE_MODE")
        image = self.compute.get_image(s.image, retry_strategy=self.no_retry).data
        require(image.lifecycle_state == "AVAILABLE", "IMAGE_NOT_AVAILABLE")
        require(image.listing_type in {None, "NONE"}, "COMMUNITY_OR_MARKETPLACE_IMAGE_NOT_ALLOWED")
        custom = s.image_mode == "custom"
        if custom:
            # Only an existing image in this tenancy's enumerated compartments.
            # Never import images, add shape compatibility, or change firmware.
            require(image.compartment_id in compartment_ids, "CUSTOM_IMAGE_MUST_BELONG_TO_THIS_TENANCY")
            operating_system = str(image.operating_system or "").strip()
            require(bool(operating_system), "CUSTOM_IMAGE_OS_METADATA_REQUIRED")
            require("windows" not in operating_system.lower(), "WINDOWS_CUSTOM_IMAGE_NOT_ALLOWED")
            label = operating_system + " " + str(image.display_name or "")
            require(not re.search(r"(?i)(?<![a-z0-9])(?:aarch64|arm64)(?![a-z0-9])", label),
                    "ARM_CUSTOM_IMAGE_CANNOT_RUN_ON_AMD_E2")
            require(image.launch_mode in {"NATIVE", "PARAVIRTUALIZED", "CUSTOM"},
                    "CUSTOM_IMAGE_LAUNCH_MODE_NOT_SUPPORTED")
        else:
            require(image.compartment_id is None and image.operating_system == "Canonical Ubuntu",
                    "ONLY_OFFICIAL_UBUNTU_PLATFORM_IMAGE_ALLOWED")
            require(image.operating_system_version in {"22.04", "24.04"}, "UNAPPROVED_UBUNTU_VERSION")
            require("pro" not in image.display_name.lower(), "PAID_IMAGE_VARIANT_NOT_ALLOWED")
        size = image.size_in_mbs
        require(isinstance(size, (int, float)) and not isinstance(size, bool)
                and math.isfinite(size) and size > 0, "UNKNOWN_IMAGE_BOOT_VOLUME_SIZE")
        minimum_boot_gb = math.ceil(size / 1024)
        require(minimum_boot_gb <= BOOT_GB, "IMAGE_REQUIRES_LARGER_BOOT_VOLUME")
        shapes = self.all(self.compute.list_shapes, s.compartment, availability_domain=ad, image_id=s.image)
        require(any(x.shape == SHAPE for x in shapes), "IMAGE_NOT_COMPATIBLE_WITH_E2_MICRO")
        launch_mode = getattr(image, "launch_mode", None)
        firmware = getattr(getattr(image, "launch_options", None), "firmware", None)
        billable_size = getattr(image, "billable_size_in_gbs", None) if custom else None
        if not (isinstance(billable_size, (int, float)) and not isinstance(billable_size, bool)
                and math.isfinite(billable_size) and billable_size >= 0):
            billable_size = None
        # Do not log the image's free-form name, OS labels, OCID, or any contents.
        return {"image_mode": s.image_mode, "image_minimum_boot_gb": minimum_boot_gb,
                "image_launch_mode": launch_mode if launch_mode in {"NATIVE", "PARAVIRTUALIZED", "CUSTOM", "EMULATED", "ACCELERATEDPV"} else None,
                "image_firmware": firmware if firmware in {"BIOS", "UEFI_64"} else None,
                "custom_image_storage_billable_gb": billable_size,
                "image_review_required": custom,
                "image_warnings": ["CUSTOM_IMAGE_STORAGE_AND_LICENSE_COSTS_REQUIRE_OWNER_REVIEW",
                                   "CUSTOM_IMAGE_X86_BOOT_FIRMWARE_AND_SSH_SETUP_REQUIRE_OWNER_REVIEW"] if custom else []}

    def inspect(self):
        s = self.settings
        regions = self.all(self.identity.list_region_subscriptions, s.tenancy)
        require(any(x.region_name == REGION and x.is_home_region and x.status == "READY" for x in regions), "SINGAPORE_MUST_BE_READY_HOME_REGION")
        ads = self.all(self.identity.list_availability_domains, s.tenancy)
        target_ads = [x.name for x in ads if x.name.upper().endswith("AD-1")]
        require(len(target_ads) == 1, "UNIQUE_AD1_REQUIRED")
        ad = target_ads[0]
        comps = self.all(self.identity.list_compartments, s.tenancy,
                         compartment_id_in_subtree=True, access_level="ANY")
        compartment_ids = [s.tenancy] + [x.id for x in comps if x.lifecycle_state == "ACTIVE"]
        require(s.compartment in compartment_ids, "TARGET_COMPARTMENT_NOT_IN_ACTIVE_INVENTORY")
        instances = []
        for cid in compartment_ids:
            instances.extend(self.all(self.compute.list_instances, cid))
        tagged = [x for x in instances if (x.freeform_tags or {}).get("retry-campaign") == s.campaign]
        named = [x for x in instances if x.display_name == NAME]
        if tagged:
            require(len(tagged) == 1, "MULTIPLE_MANAGED_INSTANCES")
            target = tagged[0]
            require(target.shape == SHAPE and (target.freeform_tags or {}).get("managed-by") == MANAGED_BY, "MANAGED_INSTANCE_CHANGED")
            require(target.lifecycle_state not in {"TERMINATED", "TERMINATING"}, "MANAGED_INSTANCE_WAS_TERMINATED_NO_REPLACEMENT_ALLOWED")
            return {"existing_target": True, "instance_hash": digest(target.id), "capacity": "NOT_NEEDED"}
        require(not named, "TARGET_NAME_ALREADY_EXISTS_WITHOUT_CAMPAIGN_TAG")
        active = [x for x in instances if x.shape == SHAPE and x.lifecycle_state != "TERMINATED"]
        require(len(active) == 1 and active[0].lifecycle_state == "RUNNING"
                and active[0].availability_domain == ad, "EXACTLY_ONE_RUNNING_BASELINE_E2_REQUIRED")
        quotas = []
        for cid in dict.fromkeys([s.tenancy, s.compartment]):
            quota = self.limits.get_resource_availability("compute", "standard-e2-micro-core-count", cid,
                availability_domain=ad, retry_strategy=self.no_retry).data
            require(isinstance(quota.available, (int, float)) and quota.available >= 1, "NO_REMAINING_E2_LIMIT_OR_QUOTA")
            quotas.append(quota.available)
        volumes, backups = {}, {}
        for cid in compartment_ids:
            for domain in ads:
                for volume in self.all(self.block.list_boot_volumes,
                                       availability_domain=domain.name, compartment_id=cid):
                    volumes[volume.id] = volume
            for volume in self.all(self.block.list_volumes, compartment_id=cid):
                volumes[volume.id] = volume
            for method in (self.block.list_boot_volume_backups, self.block.list_volume_backups):
                for backup in self.all(method, cid):
                    if backup.lifecycle_state != "TERMINATED":
                        backups[backup.id] = backup
        storage = 0
        for volume in volumes.values():
            if volume.lifecycle_state != "TERMINATED":
                require(isinstance(volume.size_in_gbs, (int, float)), "UNKNOWN_VOLUME_SIZE")
                storage += volume.size_in_gbs
        require(storage + BOOT_GB <= STORAGE_GB_LIMIT, "STORAGE_WOULD_EXCEED_200_GB")
        require(len(backups) <= 5, "BACKUPS_ALREADY_EXCEED_FREE_COUNT")
        subnet = self.network.get_subnet(s.subnet, retry_strategy=self.no_retry).data
        require(subnet.lifecycle_state == "AVAILABLE", "SUBNET_NOT_AVAILABLE")
        require(subnet.availability_domain in {None, ad}, "SUBNET_IN_DIFFERENT_AD")
        require(subnet.prohibit_public_ip_on_vnic is False, "EXISTING_SUBNET_DOES_NOT_ALLOW_PUBLIC_IP")
        image_plan = self.inspect_image(compartment_ids, ad)
        details = self.oci.core.models.CreateComputeCapacityReportDetails(
            compartment_id=s.tenancy, availability_domain=ad,
            shape_availabilities=[self.oci.core.models.CreateCapacityReportShapeAvailabilityDetails(instance_shape=SHAPE)])
        response = self.compute.create_compute_capacity_report(details, retry_strategy=self.no_retry)
        rows = response.data.shape_availabilities
        require(bool(rows) and all(x.instance_shape == SHAPE for x in rows), "UNKNOWN_CAPACITY_RESPONSE")
        statuses = [x.availability_status for x in rows]
        require(all(x in {"AVAILABLE", "OUT_OF_HOST_CAPACITY"} for x in statuses), "UNSUPPORTED_OR_UNKNOWN_CAPACITY_STATUS")
        require(not any(x.availability_status == "AVAILABLE" and x.available_count == 0 for x in rows), "CONTRADICTORY_CAPACITY_RESPONSE")
        return {"existing_target": False, "ad": ad, "existing_e2_count": len(active),
                "limit_available": min(quotas), "existing_storage_gb": storage,
                "proposed_storage_gb": storage + BOOT_GB, "backup_count": len(backups),
                "capacity": "AVAILABLE" if "AVAILABLE" in statuses else "OUT_OF_HOST_CAPACITY",
                "capacity_request_id": response.headers.get("opc-request-id"), **image_plan}

    def launch(self, plan, retry_token):
        s = self.settings
        body = self.oci.core.models.LaunchInstanceDetails(
            compartment_id=s.compartment, availability_domain=plan["ad"],
            display_name=NAME, shape=SHAPE,
            source_details=self.oci.core.models.InstanceSourceViaImageDetails(
                image_id=s.image, boot_volume_size_in_gbs=BOOT_GB,
                boot_volume_vpus_per_gb=BOOT_VPUS),
            create_vnic_details=self.oci.core.models.CreateVnicDetails(
                subnet_id=s.subnet, assign_public_ip=True),
            metadata={"ssh_authorized_keys": s.ssh_key},
            freeform_tags={"managed-by": MANAGED_BY, "retry-campaign": s.campaign})
        response = self.compute.launch_instance(body, opc_retry_token=retry_token,
            retry_strategy=self.no_retry)
        return {"instance_hash": digest(response.data.id), "state": response.data.lifecycle_state,
                "request_id": response.headers.get("opc-request-id")}


def run(operation, store, gateway_factory, settings_factory, confirmation="", clock=time.time):
    state = store.load()
    require(state.get("version") == 1 and state.get("mode") in {"paused", "active", "halted", "uncertain", "completed"}, "INVALID_PERSISTENT_STATE")
    if operation == "status":
        return {"status": state["mode"], "attempts": state.get("attempts", 0)}
    if operation == "pause":
        if state["mode"] != "completed":
            state["mode"] = "paused"
            store.save(state)
        return {"status": state["mode"]}
    if operation == "tick" and state["mode"] != "active":
        return {"status": "SKIPPED_" + state["mode"].upper()}
    if operation != "plan" and state["mode"] == "completed":
        return {"status": "ALREADY_COMPLETED_NO_MORE_CREATION"}
    if operation == "start":
        require(confirmation == CONFIRMATION, "EXPLICIT_MANUAL_CONFIRMATION_REQUIRED")
    now = clock()
    if operation == "tick" and now >= state.get("expires_at", 0):
        state["mode"] = "paused"
        store.save(state)
        return {"status": "CAMPAIGN_EXPIRED_AFTER_30_DAYS"}
    # A cancelled job may have been accepted by OCI. Reconcile by reading, never replay launch.
    pending = state.get("launch_pending", False)
    if operation != "plan" and now < state.get("next_allowed_at", 0) and not pending:
        return {"status": "BACKOFF_OR_MINIMUM_30_MIN_INTERVAL"}
    try:
        settings = settings_factory()
        if operation != "plan":
            require(settings.free_approved, "OWNER_MUST_APPROVE_FREE_CONFIGURATION")
        gateway = gateway_factory(settings)
        plan = gateway.inspect()
        if operation == "plan":
            return {"status": "READ_ONLY_PLAN", "shape": SHAPE, "region": REGION,
                    "name": NAME, "boot_gb": BOOT_GB, "boot_vpus": BOOT_VPUS,
                    "free_configuration_approved": settings.free_approved,
                    "intent_hash": settings.intent_hash(), **plan}
        if plan["existing_target"]:
            finished = {**state, "mode": "completed", "launch_pending": False,
                        "instance_hash": plan["instance_hash"], "finished_at": now}
            store.save(finished)
            return {"status": "EXISTING_TARGET_CONFIRMED_NO_MORE_CREATION", "notify": True}
        require(not pending, "PREVIOUS_LAUNCH_AMBIGUOUS_REQUIRES_HUMAN_REVIEW")
        if operation == "tick":
            require(state.get("intent_hash") == settings.intent_hash(), "CONFIG_CHANGED_RUN_MANUAL_PLAN_AND_START")
        else:
            state.update(mode="active", intent_hash=settings.intent_hash(),
                         expires_at=now + 30 * 24 * 3600)
        state["next_allowed_at"] = now + INTERVAL_SECONDS
        if plan["capacity"] == "OUT_OF_HOST_CAPACITY":
            state["last_result"] = "OUT_OF_HOST_CAPACITY"
            store.save(state)
            return {"status": "OUT_OF_HOST_CAPACITY_WAIT_NEXT_SCHEDULE", **plan}
        require(plan["capacity"] == "AVAILABLE", "UNEXPECTED_PLAN_CAPACITY")
        state.update(launch_pending=True, last_result="LAUNCH_IN_FLIGHT",
                     attempts=state.get("attempts", 0) + 1, retry_token=str(uuid.uuid4()))
        # This write MUST succeed before the one and only launch API call.
        store.save(state)
        try:
            launched = gateway.launch(plan, state["retry_token"])
        except Exception as error:
            kind = classify_error(error)
            state["last_error"] = safe_error(error)
            if kind == "capacity":
                state.update(launch_pending=False, last_result="OUT_OF_HOST_CAPACITY")
            elif kind == "throttled":
                state.update(launch_pending=False, next_allowed_at=now + 3600,
                             last_result="RATE_LIMITED_BACKOFF_ONE_HOUR")
            else:
                # Includes transport timeouts, generic 5xx, permission and quota errors.
                # Do not issue a second create after an uncertain outcome.
                state.update(mode="uncertain", last_result="LAUNCH_ERROR_REVIEW_REQUIRED")
            store.save(state)
            return {"status": state["last_result"], "error": state["last_error"]}
        # Keep the local pending flag until the completed journal write is acknowledged.
        # If that write fails after OCI accepted the request, the error path must
        # preserve pending=true rather than accidentally permitting a new launch.
        finished = {**state, "mode": "completed", "launch_pending": False,
                    "instance_hash": launched["instance_hash"], "finished_at": now,
                    "last_result": "CREATION_ACCEPTED"}
        store.save(finished)
        return {"status": "CREATION_ACCEPTED_NO_MORE_CREATION", "notify": True,
                "lifecycle_state": launched["state"], "request_id": launched["request_id"]}
    except Exception as error:
        if operation == "plan":
            raise
        state.update(mode="uncertain" if state.get("launch_pending") else "halted",
                     last_error=safe_error(error))
        store.save(state)
        return {"status": "STOPPED_FOR_REVIEW", "error": state["last_error"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation", choices=["plan", "start", "pause", "status", "tick"], default="plan")
    args = parser.parse_args()
    require(os.environ.get("GITHUB_ACTIONS") == "true", "ONLY_GITHUB_ACTIONS_ALLOWED")
    require(os.environ.get("GITHUB_REF") == "refs/heads/main", "ONLY_MAIN_BRANCH_ALLOWED")
    from github_state import GitHubState
    store = GitHubState.from_env()
    result = run(args.operation, store, OciGateway, Settings.from_env,
                 os.environ.get("CONFIRMATION", ""))
    # Mask metadata identifiers as well as the private key; never print a launch body.
    print(json.dumps(result, ensure_ascii=False, indent=2))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a") as out:
            out.write("## E2.Micro result\n\n```json\n" + json.dumps(result, indent=2) + "\n```\n")
    if result.get("notify"):
        try:
            store.notify()
        except Exception as error:
            print("Notification failed; creation is already permanently stopped: " + type(error).__name__)
    return 1 if result["status"] in {"STOPPED_FOR_REVIEW", "LAUNCH_ERROR_REVIEW_REQUIRED"} else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"status": "FAILED_CLOSED", "error": safe_error(error)}))
        raise SystemExit(1)
