"""Stripe-for-WooCommerce integration tools (read-first, approval-gated refunds).

This module works through WooCommerce's REST API rather than calling Stripe's
own API directly, so it never needs your Stripe secret key. It can:
- Detect whether a Stripe payment gateway is active and configured.
- Summarize gateway settings with API keys/secrets redacted.
- List recent transactions (WooCommerce orders paid via Stripe) read-only.
- Issue a refund through WooCommerce after explicit user approval.
"""

from __future__ import annotations

import logging
from typing import Any

from tools.wordpress_client import WordPressClient, WordPressClientError

LOGGER = logging.getLogger(__name__)

WC_NAMESPACE = "wc/v3"
# Gateway ids commonly registered by Stripe plugins for WooCommerce.
_STRIPE_GATEWAY_IDS = ("stripe", "stripe_cc", "stripe_sepa", "eh_stripe_pay", "woocommerce_payments")
_SECRET_FRAGMENTS = ("secret", "key", "token", "client_id", "webhook")

READ_ACTIONS = ("status", "transactions", "settings")


class StripeTools:
    """Read Stripe gateway status/transactions and process approved refunds."""

    def __init__(self, client: WordPressClient) -> None:
        self.client = client

    def read(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        action = (action or "status").lower()
        if action == "transactions":
            return self.recent_transactions(payload)
        if action == "settings":
            return self.settings_summary(payload)
        return self.status(payload)

    # -- detection / status ---------------------------------------------
    def _stripe_gateways(self) -> list[dict[str, Any]]:
        gateways = self.client.request_api(f"{WC_NAMESPACE}/payment_gateways")
        if not isinstance(gateways, list):
            return []
        return [g for g in gateways if _is_stripe_gateway(g)]

    def status(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            stripe_gateways = self._stripe_gateways()
        except WordPressClientError as exc:
            return {
                "summary": (
                    "Could not read WooCommerce payment gateways. Confirm WooCommerce is active "
                    f"and the user can manage payments. Details: {exc}"
                ),
                "active": False,
            }
        if not stripe_gateways:
            return {
                "summary": "No Stripe payment gateway was detected in WooCommerce.",
                "active": False,
                "recommended_next_steps": [
                    "Install the official 'WooCommerce Stripe Payment Gateway' plugin.",
                    "Enable the Stripe method under WooCommerce > Settings > Payments.",
                ],
            }
        enabled = [g for g in stripe_gateways if str(g.get("enabled")).lower() in {"true", "1"}]
        gateway = stripe_gateways[0]
        return {
            "summary": (
                f"Stripe gateway detected ({gateway.get('title') or gateway.get('id')}); "
                f"{len(enabled)} enabled of {len(stripe_gateways)} Stripe method(s)."
            ),
            "active": bool(enabled),
            "gateways": [
                {
                    "id": g.get("id"),
                    "title": g.get("title"),
                    "enabled": g.get("enabled"),
                    "method_title": g.get("method_title"),
                    "test_mode": _detect_test_mode(g),
                }
                for g in stripe_gateways
            ],
        }

    def settings_summary(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        gateways = self._stripe_gateways()
        if not gateways:
            return {"summary": "No Stripe gateway is configured in WooCommerce.", "active": False}
        summaries = []
        for gateway in gateways:
            settings = gateway.get("settings", {})
            summaries.append(
                {
                    "id": gateway.get("id"),
                    "title": gateway.get("title"),
                    "enabled": gateway.get("enabled"),
                    "test_mode": _detect_test_mode(gateway),
                    "settings": _redact_secrets(settings),
                }
            )
        return {
            "summary": (
                f"Summarized {len(summaries)} Stripe gateway configuration(s). "
                "API keys and secrets are redacted."
            ),
            "active": True,
            "gateways": summaries,
        }

    def recent_transactions(self, payload: dict[str, Any]) -> dict[str, Any]:
        per_page = payload.get("per_page", 10)
        try:
            count = max(1, min(int(per_page), 50))
        except (TypeError, ValueError):
            count = 10
        orders = self.client.request_api(
            f"{WC_NAMESPACE}/orders",
            params={
                "per_page": count,
                "_fields": "id,number,status,total,currency,payment_method,payment_method_title,date_created,transaction_id",
            },
        )
        orders = orders if isinstance(orders, list) else []
        stripe_orders = [o for o in orders if "stripe" in str(o.get("payment_method", "")).lower()]
        return {
            "summary": (
                f"Found {len(stripe_orders)} recent Stripe transaction(s) out of {len(orders)} order(s). "
                "Read-only; full payment details live in the Stripe dashboard."
            ),
            "count": len(stripe_orders),
            "transactions": [
                {
                    "order_id": o.get("id"),
                    "number": o.get("number"),
                    "status": o.get("status"),
                    "total": o.get("total"),
                    "currency": o.get("currency"),
                    "payment_method_title": o.get("payment_method_title"),
                    "transaction_id": o.get("transaction_id"),
                    "date_created": o.get("date_created"),
                }
                for o in stripe_orders
            ],
        }

    # -- write (approval-gated) -----------------------------------------
    def refund(self, payload: dict[str, Any]) -> dict[str, Any]:
        order_id = payload.get("order_id") or payload.get("id")
        if not order_id:
            raise WordPressClientError("I need the order ID to process a refund.")
        body: dict[str, Any] = {"api_refund": True}
        if payload.get("amount") is not None:
            body["amount"] = str(payload["amount"])
        if payload.get("reason"):
            body["reason"] = str(payload["reason"])
        result = self.client.request_api(
            f"{WC_NAMESPACE}/orders/{int(order_id)}/refunds", method="POST", json_body=body
        )
        amount = result.get("amount") if isinstance(result, dict) else None
        return {
            "summary": (
                f"Refund processed for order #{order_id}"
                + (f" (amount: {amount})." if amount else " (full amount).")
            ),
            "refund": {
                "id": result.get("id") if isinstance(result, dict) else None,
                "order_id": order_id,
                "amount": amount,
                "reason": result.get("reason") if isinstance(result, dict) else None,
            },
        }


def _is_stripe_gateway(gateway: dict[str, Any]) -> bool:
    if not isinstance(gateway, dict):
        return False
    gid = str(gateway.get("id", "")).lower()
    title = str(gateway.get("title", "")).lower()
    return any(token in gid for token in _STRIPE_GATEWAY_IDS) or "stripe" in title


def _detect_test_mode(gateway: dict[str, Any]) -> bool | None:
    settings = gateway.get("settings", {}) if isinstance(gateway, dict) else {}
    for key in ("testmode", "test_mode", "sandbox"):
        if key in settings:
            value = settings[key]
            value = value.get("value") if isinstance(value, dict) else value
            return str(value).lower() in {"yes", "true", "1", "on"}
    return None


def _redact_secrets(settings: Any) -> dict[str, Any]:
    if not isinstance(settings, dict):
        return {}
    redacted: dict[str, Any] = {}
    for key, value in settings.items():
        raw = value.get("value") if isinstance(value, dict) else value
        if any(fragment in key.lower() for fragment in _SECRET_FRAGMENTS):
            redacted[key] = "***redacted***" if raw else ""
        else:
            redacted[key] = raw
    return redacted
