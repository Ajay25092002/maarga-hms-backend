from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class BillItemInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: Optional[str] = Field(default=None, max_length=100)
    service_date: Optional[date] = None
    service_code: Optional[str] = Field(default=None, max_length=100)
    description: str = Field(min_length=1, max_length=1000)
    rate: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
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

    @field_validator("description", "category")
    @classmethod
    def validate_text_fields(cls, value):
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("Value cannot be empty.")
        return value


class CreateBillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bill_type: Literal["OP", "IP"]
    patient_id: str = Field(min_length=1, max_length=50)
    bill_date: Optional[datetime] = None
    department: Optional[str] = Field(default=None, max_length=100)
    doctor_name: Optional[str] = Field(default=None, max_length=150)
    discount_amount: Decimal = Field(
        default=Decimal("0"),
        ge=0,
        max_digits=12,
        decimal_places=2,
    )
    notes: Optional[str] = None
    admission_date: Optional[datetime] = None
    discharge_date: Optional[datetime] = None
    bed_number: Optional[str] = Field(default=None, max_length=100)
    rate_category: Optional[str] = Field(default=None, max_length=100)
    patient_category: Optional[str] = Field(default=None, max_length=100)
    items: list[BillItemInput] = Field(min_length=1)

    @field_validator("patient_id")
    @classmethod
    def validate_patient_id(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Patient ID cannot be empty.")
        return value

    @field_validator("items")
    @classmethod
    def validate_items(cls, items):
        if not items:
            raise ValueError("At least one bill item is required.")

        for item in items:
            amount = item.rate * item.quantity
            if item.discount_amount > amount:
                raise ValueError(
                    "An item discount cannot exceed its amount."
                )

        return items


class RecordPaymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Decimal = Field(
        gt=0,
        max_digits=12,
        decimal_places=2,
    )
    payment_date: Optional[datetime] = None
    payment_method: Optional[str] = Field(default=None, max_length=50)
    reference_number: Optional[str] = Field(default=None, max_length=150)
    notes: Optional[str] = None