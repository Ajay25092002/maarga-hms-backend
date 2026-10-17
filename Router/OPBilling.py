# OPBilling.py
from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Path, Query, status

from Router.Login import get_current_user
from Schemas.billing_schema import (
    CreateBillRequest,
    UpdateBillRequest,
    RecordPaymentRequest,
)
from Services.billing_service import create_bill_service, update_bill_service
from Services.bill_query_service import (
    get_bill_service,
    list_bills_service,
    list_bill_creators_service,
)
from Services.payment_service import record_payment_service
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
):
    """List bills with search, pagination, date, and payment filters."""
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


@router.get("/{bill_id}")
def get_bill(
    bill_id: int = Path(gt=0),
):
    """Retrieve bill details, service items, and payment history."""
    return get_bill_service(bill_id)



@router.put("/{bill_id}")
def update_bill(
    payload: UpdateBillRequest,
    bill_id: int = Path(gt=0),
    current_user: dict = Depends(get_current_user),
):
    """Update an existing OP/IP bill and its service items."""
    user_id = current_user["user"]["id"]
    return update_bill_service(bill_id, payload, updated_by=user_id)


@router.post(
    "/{bill_id}/payments",
    status_code=status.HTTP_201_CREATED,
)
def record_payment(
    payload: RecordPaymentRequest,
    bill_id: int = Path(gt=0),
):
    """Record a payment against an existing bill."""
    return record_payment_service(
        bill_id,
        payload,
    )
