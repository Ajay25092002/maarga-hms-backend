
from datetime import date, datetime
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

        service_ids = [item.service_id for item in items]

        if len(service_ids) != len(set(service_ids)):
            raise ValueError(
                "A service can appear only once in a bill. "
                "Combine quantities for duplicate services."
            )

        return items

    @model_validator(mode="after")
    def validate_bill_context(self):
        if self.bill_type == "IP":
            if self.room_type_id is None:
                raise ValueError(
                    "Room type is required for IP billing."
                )

            if self.admission_date and self.discharge_date:
                if self.discharge_date < self.admission_date:
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
