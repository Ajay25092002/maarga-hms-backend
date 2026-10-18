from datetime import datetime
from datetime import date
from zoneinfo import ZoneInfo
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg2 import Error as PsycopgError

from Router.Login import get_current_user
from Services.report_service import REPORTS, fetch_report

router = APIRouter(prefix="/api/reports", tags=["Reports"])


def require_report_permission(current=Depends(get_current_user), download=False):
    permissions = current.get("user", {}).get("permissions", {})
    required = "reports.download" if download else "reports.view"
    if permissions.get(required) is not True:
        raise HTTPException(status_code=403, detail="You do not have permission to access reports.")
    return current


@router.get("/catalog")
def report_catalog(current=Depends(get_current_user)):
    permissions = current.get("user", {}).get("permissions", {})
    if permissions.get("reports.view") is not True and permissions.get("reports.download") is not True:
        raise HTTPException(status_code=403, detail="You do not have permission to access reports.")
    return [{"key": key, "label": label} for key, label in REPORTS.items()]


@router.get("/{report_type}")
def get_report(
    report_type: str,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    search: Optional[str] = Query(default=None, max_length=150),
    source: Optional[str] = Query(default=None, max_length=50),
    bill_type: Optional[str] = Query(default=None, pattern="^(OP|IP)$"),
    payment_method: Optional[str] = Query(default=None, max_length=50),
    staff: Optional[str] = Query(default=None, max_length=150),
    service: Optional[str] = Query(default=None, max_length=150),
    service_group: Optional[str] = Query(default=None, max_length=150),
    status_filter: Optional[str] = Query(default=None, alias="status", max_length=30),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=200),
    export_all: bool = False,
    current=Depends(get_current_user),
):
    permissions = current.get("user", {}).get("permissions", {})
    if export_all:
        if permissions.get("reports.download") is not True:
            raise HTTPException(status_code=403, detail="You do not have permission to download reports.")
    elif permissions.get("reports.view") is not True:
        raise HTTPException(status_code=403, detail="You do not have permission to view reports.")
    if report_type not in REPORTS:
        raise HTTPException(status_code=404, detail="Report not found.")
    # Every report request is bounded to an explicit date range, including
    # direct API calls. The UI supplies the last seven Indian calendar days by default.
    if date_from is None or date_to is None:
        raise HTTPException(
            status_code=422,
            detail="Both date_from and date_to are required for a report.",
        )
    if (date_from is None) != (date_to is None):
        raise HTTPException(
            status_code=422,
            detail="Provide both a start date and an end date.",
        )
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="Start date must be on or before end date.")
    today_india = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    if date_from > today_india or date_to > today_india:
        raise HTTPException(status_code=422, detail="Future report dates are not allowed.")
    filters = {
        "date_from": date_from.isoformat() if date_from else None,
        "date_to": date_to.isoformat() if date_to else None,
        "search": search.strip() if search else None,
        "source": source,
        "bill_type": bill_type,
        "payment_method": payment_method,
        "staff": staff.strip() if staff else None,
        "service": service.strip() if service else None,
        "service_group": service_group.strip() if service_group else None,
        "status": status_filter,
    }
    try:
        return fetch_report(report_type, filters, page, page_size, export_all)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (PsycopgError, RuntimeError):
        raise HTTPException(status_code=500, detail="Unable to load this report. Please try again.") from None
