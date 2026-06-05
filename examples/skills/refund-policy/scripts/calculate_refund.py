#!/usr/bin/env python3
"""Calculate refund amount based on purchase date and item price.

Usage: python calculate_refund.py <days_since_purchase> <price>

Output: JSON with refund_type, refund_amount, and notes.
"""

import json
import sys
from datetime import datetime, timedelta


def calculate(days: int, price: float) -> dict:
    if days < 0:
        return {
            "refund_type": "error",
            "refund_amount": 0.0,
            "notes": "Days since purchase cannot be negative.",
        }

    if days <= 14:
        return {
            "refund_type": "full",
            "refund_amount": round(price, 2),
            "notes": f"Full refund: within 14-day window ({days} days).",
        }
    elif days <= 30:
        fee = round(price * 0.15, 2)
        amount = round(price - fee, 2)
        return {
            "refund_type": "pro_rated",
            "refund_amount": amount,
            "notes": f"Pro-rated refund ({days} days): 15% restocking fee (${fee:.2f}).",
        }
    elif days <= 90:
        return {
            "refund_type": "store_credit",
            "refund_amount": round(price, 2),
            "notes": f"Store credit only ({days} days): requires manager approval.",
        }
    else:
        return {
            "refund_type": "none",
            "refund_amount": 0.0,
            "notes": f"Outside refund window ({days} days): escalate to manager.",
        }


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python calculate_refund.py <days> <price>", file=sys.stderr)
        sys.exit(1)

    days = int(sys.argv[1])
    price = float(sys.argv[2])
    result = calculate(days, price)
    print(json.dumps(result, indent=2))
