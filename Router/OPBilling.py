from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Path, Query, status

from Schemas.billing_schema import (
    CreateBillRequest,
    RecordPaymentRequest,
)
from Services.billing_service import create_bill_service
from Services.bill_query_service import (
    get_bill_service,
    list_bills_service,
)
from Services.payment_service import record_payment_service
from Services.bill_pdf_service import (
    generate_ip_bill_pdf,
    generate_op_bill_pdf,
)

router = APIRouter(prefix="/api/bills", tags=["Billing"])


@router.post("", status_code=status.HTTP_201_CREATED)
def create_bill(payload: CreateBillRequest):
    return create_bill_service(payload)


@router.get("")
def list_bills(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=100),
    search: Optional[str] = Query(default=None, max_length=100),
    bill_type: Optional[Literal["OP", "IP"]] = None,
    status_filter: Optional[str] = Query(default=None, alias="status"),
    date_filter: Optional[str] = Query(default=None),
    date_from: Optional[date] = Query(default=None),
    date_to: Optional[date] = Query(default=None),
):
    return list_bills_service(
        page=page,
        page_size=page_size,
        search=search,
        bill_type=bill_type,
        status_filter=status_filter,
        date_filter=date_filter,
        date_from=date_from,
        date_to=date_to,
    )


# Keep the static PDF paths before /{bill_id}, so they cannot be interpreted
# as a bill_id route segment. These two endpoints intentionally validate type.
@router.get("/{bill_id}/ip-pdf")
def get_ip_bill_pdf(bill_id: int = Path(gt=0)):
    return generate_ip_bill_pdf(bill_id)


@router.get("/{bill_id}/op-pdf")
def get_op_bill_pdf(bill_id: int = Path(gt=0)):
    return generate_op_bill_pdf(bill_id)


# Backwards compatibility for the existing frontend/API that calls /{id}/pdf.
# This alias remains IP-only; OP frontend should use /{id}/op-pdf.
@router.get("/{bill_id}/pdf")
def get_ip_bill_pdf_legacy(bill_id: int = Path(gt=0)):
    return generate_ip_bill_pdf(bill_id)


@router.get("/{bill_id}")
def get_bill(bill_id: int = Path(gt=0)):
    return get_bill_service(bill_id)


@router.post("/{bill_id}/payments", status_code=status.HTTP_201_CREATED)
def record_payment(
    payload: RecordPaymentRequest,
    bill_id: int = Path(gt=0),
):
    return record_payment_service(bill_id, payload)
