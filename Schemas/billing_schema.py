
from datetime import date, datetime
from zoneinfo import ZoneInfo
from decimal import Decimal
from typing import Literal, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)



class BillItemInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_id: int = Field(gt=0)
    service_date: Optional[date] = None

    # Per-service IP room context. Names are accepted for compatibility/display,
    # but the backend resolves room_type_name from room_types to avoid trusting
    # client-supplied master data.
    room_type_id: Optional[int] = Field(default=None, gt=0)
    room_type_name: Optional[str] = Field(default=None, max_length=100)
    bed_number: Optional[str] = Field(default=None, max_length=100)

    quantity: Decimal = Field(
        default=Decimal("1"),
        gt=0,
        max_digits=10,
        decimal_places=2,
    )

    discount_amount: Decimal = Field(
        default=Decimal("0"),
        ge=0,
        max_digits=12,
        decimal_places=2,
    )

    gst_amount: Decimal = Field(
        default=Decimal("0"),
        ge=0,
        max_digits=12,
        decimal_places=2,
    )



class CreateBillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bill_type: Literal["OP", "IP"]
    patient_id: str = Field(min_length=1, max_length=50)

    bill_date: Optional[datetime] = None
    department: Optional[str] = Field(default=None, max_length=100)

    # Professional details
    doctor_id: Optional[int] = Field(default=None, gt=0)
    doctor_name: Optional[str] = Field(
        default=None,
        max_length=150,
    )

    psychologist_id: Optional[int] = Field(default=None, gt=0)
    psychologist_name: Optional[str] = Field(
        default=None,
        max_length=150,
    )

    # Pricing context
    # IP bills use room_type_id for room-specific pricing.
    room_type_id: Optional[int] = Field(default=None, gt=0)

    # Bill-level amounts and notes
    discount_amount: Decimal = Field(
        default=Decimal("0"),
        ge=0,
        max_digits=12,
        decimal_places=2,
    )

    notes: Optional[str] = None

    # IP admission details
    admission_date: Optional[datetime] = None
    discharge_date: Optional[datetime] = None
    bed_number: Optional[str] = Field(
        default=None,
        max_length=100,
    )

    rate_category: Optional[str] = Field(
        default=None,
        max_length=100,
    )

   

    items: list[BillItemInput] = Field(min_length=1)

    @field_validator("patient_id")
    @classmethod
    def validate_patient_id(cls, value: str) -> str:
        value = value.strip()

        if not value:
            raise ValueError("Patient ID cannot be empty.")

        return value

    @field_validator("doctor_name", "psychologist_name")
    @classmethod
    def normalize_professional_names(
        cls,
        value: Optional[str],
    ) -> Optional[str]:
        if value is None:
            return None

        value = value.strip()
        return value or None

    @field_validator("department", "bed_number", "rate_category")
    @classmethod
    def normalize_optional_text(
        cls,
        value: Optional[str],
    ) -> Optional[str]:
        if value is None:
            return None

        value = value.strip()
        return value or None

    @field_validator("notes")
    @classmethod
    def normalize_notes(
        cls,
        value: Optional[str],
    ) -> Optional[str]:
        if value is None:
            return None

        value = value.strip()
        return value or None

    @field_validator("items")
    @classmethod
    def validate_items(
        cls,
        items: list[BillItemInput],
    ) -> list[BillItemInput]:
        if not items:
            raise ValueError("At least one bill item is required.")

        return items

    @model_validator(mode="after")
    def validate_bill_context(self):
        today_india = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        for item in self.items:
            if item.service_date and item.service_date > today_india:
                raise ValueError("Service date cannot be in the future.")
        for label, value in (("Admission", self.admission_date), ("Discharge", self.discharge_date)):
            if value is not None:
                candidate = value if value.tzinfo else value.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
                if candidate.astimezone(ZoneInfo("Asia/Kolkata")).date() > today_india:
                    raise ValueError(f"{label} date cannot be in the future.")
        if self.bill_type == "IP":
            if self.room_type_id is None:
                raise ValueError(
                    "Room type is required for IP billing."
                )

            if self.admission_date and self.discharge_date:
                india_tz = ZoneInfo("Asia/Kolkata")

                # Pydantic may parse one value as timezone-aware (for example,
                # an ISO timestamp ending in "Z") and the other as naive.
                # Normalize both to the hospital's timezone before comparing.
                admission = (
                    self.admission_date.replace(tzinfo=india_tz)
                    if self.admission_date.tzinfo is None
                    else self.admission_date.astimezone(india_tz)
                )
                discharge = (
                    self.discharge_date.replace(tzinfo=india_tz)
                    if self.discharge_date.tzinfo is None
                    else self.discharge_date.astimezone(india_tz)
                )

                if discharge < admission:
                    raise ValueError(
                        "Discharge date cannot be earlier than "
                        "admission date."
                    )

        return self


class RecordPaymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Decimal = Field(
        gt=0,
        max_digits=12,
        decimal_places=2,
    )

    payment_date: Optional[datetime] = None

    payment_method: Optional[str] = Field(
        default=None,
        max_length=50,
    )

    reference_number: Optional[str] = Field(
        default=None,
        max_length=150,
    )

    notes: Optional[str] = None

    @field_validator("payment_method", "reference_number")
    @classmethod
    def normalize_payment_text(
        cls,
        value: Optional[str],
    ) -> Optional[str]:
        if value is None:
            return None

        value = value.strip()
        return value or None

    @field_validator("notes")
    @classmethod
    def normalize_payment_notes(
        cls,
        value: Optional[str],
    ) -> Optional[str]:
        if value is None:
            return None

        value = value.strip()
        return value or None


class UpdateBillRequest(CreateBillRequest):
    """Full editable bill snapshot; item dates are supplied per service."""
    pass
