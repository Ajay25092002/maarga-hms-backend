from datetime import datetime
from decimal import Decimal

import psycopg2
from fastapi import HTTPException, status
from psycopg2.extras import RealDictCursor

from Core.logger import get_logger
from Schemas.billing_schema import CreateBillRequest
from Utils.billing_helpers import (
    ensure_payment_table,
    generate_bill_number,
    get_bill_items,
    get_db_connection,
    serialize_row,
)

logger = get_logger(__name__)


def create_bill_service(payload: CreateBillRequest):
    connection = None

    try:
        connection = get_db_connection()

        with connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                ensure_payment_table(cursor)

                cursor.execute("""
                    SELECT id, patient_id, registration_number
                    FROM patients
                    WHERE patient_id = %s
                """, (payload.patient_id,))
                patient = cursor.fetchone()

                if patient is None:
                    raise HTTPException(404, "Patient not found.")

                subtotal = Decimal("0.00")
                item_discount_total = Decimal("0.00")
                prepared_items = []

                for item in payload.items:
                    category = item.category
                    if payload.bill_type == "IP":
                        if not category or not category.strip():
                            raise HTTPException(
                                status_code=422,
                                detail=(
                                    "Every IP bill item must have a category."
                                ),
                            )
                        category = category.strip()
                    elif category is not None:
                        category = category.strip() or None

                    amount = (item.rate * item.quantity).quantize(
                        Decimal("0.01")
                    )

                    if item.discount_amount > amount:
                        raise HTTPException(
                            422,
                            "An item discount cannot exceed its amount.",
                        )

                    net_amount = (
                        amount - item.discount_amount
                    ).quantize(Decimal("0.01"))

                    subtotal += amount
                    item_discount_total += item.discount_amount

                    prepared_items.append({
                        "category": category,
                        "service_date": item.service_date,
                        "service_code": item.service_code,
                        "description": item.description,
                        "rate": item.rate,
                        "quantity": item.quantity,
                        "amount": amount,
                        "discount_amount": item.discount_amount,
                        "net_amount": net_amount,
                    })

                subtotal = subtotal.quantize(Decimal("0.01"))
                item_discount_total = item_discount_total.quantize(
                    Decimal("0.01")
                )
                remaining = subtotal - item_discount_total

                if payload.discount_amount > remaining:
                    raise HTTPException(
                        422,
                        "The bill discount cannot exceed the amount remaining after item discounts.",
                    )

                total_amount = (
                    remaining - payload.discount_amount
                ).quantize(Decimal("0.01"))

                cursor.execute("""
                    INSERT INTO bills (
                        bill_number, bill_type, patient_db_id,
                        registration_number, bill_date, department,
                        doctor_name, subtotal, discount_amount,
                        total_amount, notes, admission_date,
                        discharge_date, bed_number, rate_category,
                        patient_category
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    RETURNING *
                """, (
                    generate_bill_number(payload.bill_type),
                    payload.bill_type,
                    patient["id"],
                    patient["registration_number"],
                    payload.bill_date or datetime.now(),
                    payload.department,
                    payload.doctor_name,
                    subtotal,
                    payload.discount_amount,
                    total_amount,
                    payload.notes,
                    payload.admission_date,
                    payload.discharge_date,
                    payload.bed_number,
                    payload.rate_category,
                    payload.patient_category,
                ))
                bill = dict(cursor.fetchone())

                for item in prepared_items:
                    cursor.execute("""
                        INSERT INTO bill_items (
                            bill_id, category, service_date, service_code,
                            description, rate, quantity, amount,
                            discount_amount, net_amount
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        bill["id"],
                        item["category"],
                        item["service_date"],
                        item["service_code"],
                        item["description"],
                        item["rate"],
                        item["quantity"],
                        item["amount"],
                        item["discount_amount"],
                        item["net_amount"],
                    ))

                result = serialize_row(bill)
                result["patient_id"] = patient["patient_id"]
                result["amount_paid"] = 0
                result["outstanding_amount"] = float(total_amount)
                result["payment_status"] = (
                    "Paid" if total_amount == 0 else "Unpaid"
                )
                result["items"] = get_bill_items(cursor, bill["id"])
                return result

    except HTTPException:
        raise
    except psycopg2.Error:
        logger.exception("Database error while creating a bill.")
        raise HTTPException(500, "Unable to create the bill.")
    except Exception:
        logger.exception("Unexpected error while creating a bill.")
        raise HTTPException(500, "An unexpected error occurred while creating the bill.")
    finally:
        if connection is not None:
            connection.close()