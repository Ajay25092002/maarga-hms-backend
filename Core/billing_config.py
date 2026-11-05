"""Central configuration for controlled billing exceptions.

Keep this list manually maintained. Remove an IP number when temporary edit
access is no longer required. Values must match public.bills.ip_number.
"""

TEMPORARY_EDITABLE_IP_BILLS = [
    # Add exact IP bill numbers here, for example:
    "2MG/IP20261030-000018",
    "2MG/IP20261102-000026",
    "2MG/IP20261102-000027",
    "2MG/IP20261030-000014",
    "2MGIP-20261026-000008",
]


def is_temporarily_editable_ip_bill(ip_number: str | None) -> bool:
    """Return whether an IP bill number is explicitly allowlisted."""
    normalized = str(ip_number or "").strip().upper()
    return bool(normalized) and normalized in {
        str(number).strip().upper()
        for number in TEMPORARY_EDITABLE_IP_BILLS
        if str(number).strip()
    }
