# OPBilling.py
from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, status

from Router.Login import get_current_user
from Schemas.billing_schema import (
    CreateBillRequest,
    UpdateBillRequest,
    RecordPaymentRequest,
)
from Services.billing_service import create_bill_service, update_bill_service, add_nurse_ip_service
from Services.bill_query_service import (
    get_bill_service,
    list_bills_service,
    list_bill_creators_service,
)
from Services.payment_service import record_payment_service
from Services.ip_room_history_service import list_ip_room_history
from Services.bill_audit_service import list_bill_audit_history, search_bill_history
from Services.bill_pdf_service import (
    generate_ip_bill_pdf,
    generate_op_bill_pdf,
)

router = APIRouter(
    prefix="/api/bills",
    tags=["Billing"],
)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
)
def create_bill(
    payload: CreateBillRequest,
    current_user: dict = Depends(get_current_user),
):
    """Create a bill and record the authenticated staff member."""
    user_id = current_user["user"]["id"]
    return create_bill_service(payload, created_by=user_id)


@router.get("/creators")
def list_bill_creators():
    """List users who have created bills for the billed-by filter."""
    return list_bill_creators_service()


@router.get("")
def list_bills(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=100),
    search: Optional[str] = Query(default=None, max_length=100),
    bill_type: Optional[Literal["OP", "IP"]] = Query(default=None),
    status_filter: Optional[str] = Query(
        default=None,
        alias="status",
    ),
    date_filter: Optional[str] = Query(default=None),
    date_from: Optional[date] = Query(default=None),
    date_to: Optional[date] = Query(default=None),
    billed_by: Optional[int] = Query(default=None, ge=1),
    current_user: dict = Depends(get_current_user),
):
    """List bills with search, pagination, date, and payment filters."""
    if bill_type == "IP":
        permissions = current_user.get("user", {}).get("permissions", {})
        has_ip_view = (
            permissions.get("bills.ip.view") is True
            if isinstance(permissions, dict)
            else "bills.ip.view" in permissions
        )
        has_nurse_view = (
            permissions.get("nurse.view") is True
            if isinstance(permissions, dict)
            else "nurse.view" in permissions
        )
        if not (has_ip_view or has_nurse_view):
            from fastapi import HTTPException
            raise HTTPException(status_code=403, detail="You do not have permission to view IP bills or the Nurse section.")
    return list_bills_service(
        page=page,
        page_size=page_size,
        search=search,
        bill_type=bill_type,
        status_filter=status_filter,
        date_filter=date_filter,
        date_from=date_from,
        date_to=date_to,
        billed_by=billed_by,
    )


# Keep specific PDF routes before the general bill-ID route.
@router.get("/{bill_id}/ip-pdf")
def get_ip_bill_pdf(
    bill_id: int = Path(gt=0),
):
    """Generate an IP bill PDF."""
    return generate_ip_bill_pdf(bill_id)


@router.get("/{bill_id}/op-pdf")
def get_op_bill_pdf(
    bill_id: int = Path(gt=0),
):
    """Generate an OP bill PDF."""
    return generate_op_bill_pdf(bill_id)


# Backward-compatible legacy endpoint. This remains IP-only.
@router.get("/{bill_id}/pdf")
def get_ip_bill_pdf_legacy(
    bill_id: int = Path(gt=0),
):
    """Legacy IP PDF endpoint."""
    return generate_ip_bill_pdf(bill_id)


@router.post("/{bill_id}/nurse-services", status_code=status.HTTP_201_CREATED)
def add_nurse_service(
    payload: dict,
    bill_id: int = Path(gt=0),
    current_user: dict = Depends(get_current_user),
):
    """Restricted append-only endpoint for nurse service additions."""
    permissions = current_user.get("user", {}).get("permissions", {})
    required_permission = "nurse.add_services"
    allowed = (
        permissions.get(required_permission) is True
        if isinstance(permissions, dict)
        else required_permission in permissions
    )
    if not allowed:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to add services from the Nurse section.",
        )
    allow_all_service_groups = (
        permissions.get("nurse.all_service_groups") is True
        if isinstance(permissions, dict)
        else "nurse.all_service_groups" in permissions
    )
    user_id = current_user["user"]["id"]
    try:
        service_id = int(payload.get("service_id"))
        quantity = payload.get("quantity", 1)
        service_date_value = payload.get("service_date")
        from datetime import date
        service_date = date.fromisoformat(service_date_value) if service_date_value else None
        rate = payload.get("rate")
        special_note = payload.get("special_note")
        if special_note is not None and not isinstance(special_note, str):
            raise ValueError("special_note must be text")
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=422,
            detail="Provide a valid service_id, quantity, service_date, rate and special_note.",
        ) from None
    return add_nurse_ip_service(
        bill_id,
        service_id,
        quantity,
        service_date,
        user_id,
        allow_all_service_groups=allow_all_service_groups,
        rate=rate,
        special_note=special_note,
    )


@router.get("/history/search")
def search_bills_for_history(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=100),
    search: Optional[str] = Query(default=None, max_length=100),
    bill_type: Optional[Literal["OP", "IP"]] = Query(default=None),
    current_user: dict = Depends(get_current_user),
):
    """Search bills for the audit-history screen."""
    permissions = current_user.get("user", {}).get("permissions", {})
    allowed = (
        permissions.get("bills.history.view") is True
        if isinstance(permissions, dict)
        else "bills.history.view" in permissions
    )
    if not allowed:
        raise HTTPException(status_code=403, detail="You do not have permission to view bill history.")
    return search_bill_history(page=page, page_size=page_size, search=search, bill_type=bill_type)


@router.get("/history/bill/{bill_id}")
def get_bill_history_header(
    bill_id: int = Path(gt=0),
    current_user: dict = Depends(get_current_user),
):
    """Return OP/IP-specific bill header fields for audit-history viewers."""
    permissions = current_user.get("user", {}).get("permissions", {})
    allowed = (
        permissions.get("bills.history.view") is True
        if isinstance(permissions, dict)
        else "bills.history.view" in permissions
    )
    if not allowed:
        raise HTTPException(status_code=403, detail="You do not have permission to view bill history.")
    bill = get_bill_service(bill_id)
    bill_type = str(bill.get("bill_type") or "").upper()
    fields = {
        "id", "bill_number", "bill_type", "op_number", "ip_number",
        "bill_date", "admission_date", "discharge_date", "total_amount",
        "patient_id", "patient_name", "patient_db_id", "created_at", "updated_at",
    }
    return {key: value for key, value in bill.items() if key in fields and (bill_type == "IP" or key not in {"admission_date", "discharge_date"})}


@router.get("/{bill_id}/history")
def get_bill_history(
    bill_id: int = Path(gt=0),
    current_user: dict = Depends(get_current_user),
):
    """Retrieve the audit timeline for a bill."""
    permissions = current_user.get("user", {}).get("permissions", {})
    allowed = (
        permissions.get("bills.history.view") is True
        if isinstance(permissions, dict)
        else "bills.history.view" in permissions
    )
    if not allowed:
        raise HTTPException(status_code=403, detail="You do not have permission to view bill history.")
    return list_bill_audit_history(bill_id)


@router.get("/{bill_id}/room-history")
def get_ip_room_history(
    bill_id: int = Path(gt=0),
    current_user: dict = Depends(get_current_user),
):
    """Return room/bed assignment periods for an IP bill."""
    bill = get_bill_service(bill_id)
    if str(bill.get("bill_type") or "").upper() != "IP":
        raise HTTPException(status_code=422, detail="Room history is available only for IP bills.")
    permissions = current_user.get("user", {}).get("permissions", {})
    allowed_keys = ("bills.ip.view", "nurse.view")
    allowed = any(
        permissions.get(key) is True if isinstance(permissions, dict) else key in permissions
        for key in allowed_keys
    )
    if not allowed:
        raise HTTPException(status_code=403, detail="You do not have permission to view IP room history.")
    return list_ip_room_history(bill_id)


@router.get("/{bill_id}")
def get_bill(
    bill_id: int = Path(gt=0),
    current_user: dict = Depends(get_current_user),
):
    """Retrieve bill details, service items, and payment history."""
    bill = get_bill_service(bill_id)
    permissions = current_user.get("user", {}).get("permissions", {})
    bill_type = str(bill.get("bill_type") or "").upper()
    if bill_type == "IP":
        allowed_keys = ("bills.ip.view", "nurse.view")
        allowed = any(
            permissions.get(key) is True if isinstance(permissions, dict) else key in permissions
            for key in allowed_keys
        )
        if not allowed:
            from fastapi import HTTPException
            raise HTTPException(status_code=403, detail="You do not have permission to view this IP bill.")
    return bill



@router.put("/{bill_id}")
def update_bill(
    payload: UpdateBillRequest,
    bill_id: int = Path(gt=0),
    current_user: dict = Depends(get_current_user),
):
    """Update an existing OP/IP bill and its service items."""
    permissions = current_user.get("user", {}).get("permissions", {})
    required = "bills.ip.edit" if payload.bill_type == "IP" else "bills.op.edit"
    allowed = permissions.get(required) is True if isinstance(permissions, dict) else required in permissions
    if not allowed:
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="You do not have permission to edit this bill.")
    user_id = current_user["user"]["id"]
    return update_bill_service(bill_id, payload, updated_by=user_id)


@router.post(
    "/{bill_id}/payments",
    status_code=status.HTTP_201_CREATED,
)
def record_payment(
    payload: RecordPaymentRequest,
    bill_id: int = Path(gt=0),
    current_user: dict = Depends(get_current_user),
):
    """Record a payment against an existing bill."""
    user_id = current_user["user"]["id"]
    return record_payment_service(
        bill_id,
        payload,
        created_by=user_id,
    )
