---
name: refund-policy
description: Process customer refunds, returns, and exchanges. Use when a customer asks for a refund, wants to return an item, or disputes a charge.
license: MIT
metadata:
  author: lovia
  version: "1.0"
---

# Refund Policy

## When to Use

- Customer asks for money back ("refund", "return", "money back", "cancel order")
- Customer disputes a charge
- Customer wants to exchange an item

## When NOT to Use

- The customer has a general billing question (use general support)
- The item is a digital download with a separate policy
- The order is older than 90 days (escalate to manager)

## Policy

| Time Frame       | Refund Type    | Notes                               |
| ---------------- | -------------- | ----------------------------------- |
| 0–14 days        | Full refund    | No questions asked.                 |
| 15–30 days       | Pro-rated      | Subtract 15% restocking fee.        |
| 31–90 days       | Store credit   | Requires manager approval.          |
| 90+ days         | No refund      | Escalate — do not process directly. |

## Procedure

1. **Verify the order** — ask for the order ID and confirm it exists in the system.
2. **Check the time frame** — calculate days since purchase. Use `scripts/calculate_refund.py` if needed.
3. **Determine refund type** — apply the table above.
4. **Process the refund** — issue via the payment system.
5. **Send confirmation** — use the template in `assets/refund-email.txt`.

## International Orders

International orders have additional rules. See `references/international-orders.md` for details before processing a non-domestic refund.

## Important Rules

- Always be polite and empathetic — refunds are stressful for customers.
- Never promise a refund amount before verifying the order.
- Log every refund action for audit purposes.
- If in doubt, escalate to a human manager.
