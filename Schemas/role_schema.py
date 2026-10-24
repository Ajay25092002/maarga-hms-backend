
from datetime import datetime

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)


# Canonical permission catalog for the Hospital Management System.
# Every supported permission is stored explicitly as True or False.
ROLE_PERMISSIONS = {
    # Dashboard
    "dashboard.view": "View Dashboard",

    # Shared patient registry (OP/IP billing remains separately permissioned)
    "patients.view": "View Patients",
    "patients.create": "Register Patients",
    "patients.edit": "Edit Patient Details",

        # Bill History / Audit Trail
    "bills.history.view": "View Bill History and Audit Trail",

    # OP Bills
    "bills.op.view": "View OP Bills",
    "bills.op.create": "Create OP Bills",
    "bills.op.edit": "Edit OP Bills",

    # IP Bills
    "bills.ip.view": "View IP Bills",
    "bills.ip.create": "Create IP Bills",
    "bills.ip.edit": "Edit IP Bills",

    # Nurse module (view bills separately from permission to add services)
    "nurse.view": "View Nurse Section",
    "nurse.add_services": "Add Services to IP Bills (Nurse)",
    "nurse.all_service_groups": "Allow Nurse to Add Services from All IP Service Groups",
    

    # Payments & Receipts
    "payments.view": "View Payments & Receipts",
    "payments.create": "Record Payments",


    # Payment Refunds
    "refunds.view": "View Payment Refunds",
    "refunds.create": "Create Payment Refunds",

    # Reports
    "reports.view": "View Reports",
    "reports.download": "Download Reports",

    # Users
    "users.view": "View Users",
    "users.create": "Create Users",
    "users.edit": "Edit Users and Assign Roles",
    "users.delete": "Delete or Deactivate Users",

    # Roles & Permissions
    "roles.view": "View Roles",
    "roles.create": "Create Roles",
    "roles.edit": "Edit Roles and Permissions",
    "roles.delete": "Delete Roles",

    # Pricing Management
    "pricing.view": "View Pricing Management",
    "pricing.create": "Create Pricing Configuration",
    "pricing.edit": "Edit Pricing Configuration",
    "pricing.import": "Import Pricing from Excel",
}


# Legacy permission keys were used when patient access was split by OP/IP.
# Keep existing roles working by mapping either legacy grant to the shared
# patient-registry permission. New role payloads must use canonical keys.
LEGACY_PATIENT_PERMISSION_MAP = {
    "patients.view": ("patients.op.view", "patients.ip.view"),
    "patients.create": ("patients.op.create", "patients.ip.create"),
    "patients.edit": ("patients.op.edit", "patients.ip.edit"),
}


def normalize_permissions(value: dict | None) -> dict[str, bool]:
    """
    Return the canonical permission map for an existing stored permission map.

    Old role records may still contain patients.op.* / patients.ip.* keys.
    For the shared patient registry, a grant from either legacy key carries
    forward to the corresponding shared permission. Unknown legacy keys are
    not returned.
    """
    stored = value if isinstance(value, dict) else {}
    normalized = {
        key: stored.get(key) is True
        for key in ROLE_PERMISSIONS
    }

    for canonical_key, legacy_keys in LEGACY_PATIENT_PERMISSION_MAP.items():
        normalized[canonical_key] = (
            normalized[canonical_key]
            or any(stored.get(key) is True for key in legacy_keys)
        )

    return normalized


def validate_permissions(
    value: dict[str, bool],
) -> dict[str, bool]:
    """
    Validate supplied permission keys and values.

    - Reject unsupported permission keys.
    - Require Boolean values.
    - Default omitted permissions to False.
    - Return every supported permission explicitly.
    """

    unknown = set(value) - set(ROLE_PERMISSIONS)

    if unknown:
        raise ValueError(
            f"Unsupported permission keys: {', '.join(sorted(unknown))}"
        )

    if any(not isinstance(enabled, bool) for enabled in value.values()):
        raise ValueError(
            "Every permission must be true or false."
        )

    return {
        key: value.get(key, False)
        for key in ROLE_PERMISSIONS
    }


class RoleCreate(BaseModel):
    name: str = Field(
        ...,
        min_length=2,
        max_length=100,
    )
    description: str | None = None
    permissions: dict[str, bool] = Field(
        default_factory=dict
    )
    is_active: bool = True

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()

        if len(value) < 2:
            raise ValueError(
                "Role name must contain at least 2 characters."
            )

        return value

    @field_validator("permissions")
    @classmethod
    def check_permissions(
        cls,
        value: dict[str, bool],
    ) -> dict[str, bool]:
        return validate_permissions(value)


class RoleUpdate(BaseModel):
    name: str = Field(
        ...,
        min_length=2,
        max_length=100,
    )
    description: str | None = None
    permissions: dict[str, bool] = Field(
        default_factory=dict
    )
    is_active: bool = True

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()

        if len(value) < 2:
            raise ValueError(
                "Role name must contain at least 2 characters."
            )

        return value

    @field_validator("permissions")
    @classmethod
    def check_permissions(
        cls,
        value: dict[str, bool],
    ) -> dict[str, bool]:
        return validate_permissions(value)


class RoleResponse(BaseModel):
    id: int
    name: str
    description: str | None = None
    permissions: dict[str, bool]
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PermissionCatalogItem(BaseModel):
    key: str
    label: str
