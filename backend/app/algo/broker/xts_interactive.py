"""Lakshmishree Broking — Symphony XTS *Interactive* API client.

The Interactive API is XTS's order-execution product: a session login with an
appKey/secretKey pair (DISTINCT from the market-data pair this platform
already holds), then token-authorised REST calls for orders, positions and
the order book. The endpoint contract below follows Symphony's published XTS
Interactive REST specification, which Lakshmishree resells under their own
host.

⚠️ VERIFICATION STATUS: built against the standard XTS Interactive contract;
UNVERIFIED against Lakshmishree's live gateway until the user supplies the
Interactive credentials and the burn-in checklist runs (M7 rollout). Until
then the client reports "not configured" and the execution router keeps every
order on the paper simulator.

Session policy mirrors the house market-data lesson: ONE login per process,
token reused across calls, re-login only on an auth rejection — never a
login storm. The transport is injectable so the whole client tests against a
stub without a network.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Literal, Optional

import structlog

from ...core.config import settings

log = structlog.get_logger(__name__)

# transport(method, url, headers, json_body) -> (status_code, response_json)
Transport = Callable[..., Any]

_ORDER_POLL_ATTEMPTS = 6
_ORDER_POLL_DELAY_S = 0.5

_FINAL_ORDER_STATES = {"Filled", "Rejected", "Cancelled"}


class XtsInteractiveError(Exception):
    """The gateway gave a DEFINITIVE error (rejection, validation, auth)."""


class XtsTransportError(XtsInteractiveError):
    """The REQUEST ITSELF failed (timeout, connect error, no response) — the
    order's fate is UNKNOWN: it may or may not have reached the exchange.
    Callers must treat this as 'possibly executed', never as 'refused'."""


@dataclass
class OrderResult:
    app_order_id: str
    status: str                     # Filled / Rejected / Cancelled / PendingNew / …
    average_price: Optional[float]  # broker-reported fill; None until filled
    reason: str = ""                # CancelRejectReason on a rejection


async def _default_transport(
    method: str, url: str, headers: dict[str, str], json_body: Optional[dict[str, Any]]
) -> tuple[int, dict[str, Any]]:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.request(method, url, headers=headers, json=json_body)
    except httpx.HTTPError as e:
        # A raw httpx exception escaping here used to unwind straight past
        # the callers' XtsInteractiveError handling — for an ORDER call that
        # meant "refused" semantics for an order that may have executed
        # (repeat-order hazard, found 2026-08-18). Typed so callers can tell
        # unknown-state apart from a definitive rejection.
        raise XtsTransportError(f"transport failure: {e!r}") from e
    try:
        body = resp.json()
    except ValueError:
        body = {"type": "error", "description": resp.text[:300]}
    return resp.status_code, body


class XtsInteractiveClient:
    def __init__(self, transport: Optional[Transport] = None) -> None:
        self._transport = transport or _default_transport
        self._token: Optional[str] = None
        self._user_id: Optional[str] = None
        self._is_investor = True
        self._lock = asyncio.Lock()
        self.last_error: str = ""

    # ------------------------------------------------------------ properties

    @property
    def configured(self) -> bool:
        return bool(
            settings.lakshmishree_interactive_url.strip()
            and settings.lakshmishree_interactive_app_key.strip()
            and settings.lakshmishree_interactive_secret_key.strip()
        )

    @property
    def logged_in(self) -> bool:
        return self._token is not None

    def _base(self) -> str:
        return settings.lakshmishree_interactive_url.rstrip("/")

    # ---------------------------------------------------------------- session

    async def login(self) -> None:
        """POST /interactive/user/session — one session per process."""
        if not self.configured:
            raise XtsInteractiveError(
                "Lakshmishree Interactive API is not configured "
                "(LAKSHMISHREE_INTERACTIVE_URL / _APP_KEY / _SECRET_KEY)."
            )
        async with self._lock:
            if self._token is not None:
                return
            status, body = await self._transport(
                "POST",
                f"{self._base()}/interactive/user/session",
                {"Content-Type": "application/json"},
                {
                    "appKey": settings.lakshmishree_interactive_app_key,
                    "secretKey": settings.lakshmishree_interactive_secret_key,
                    "source": settings.lakshmishree_interactive_source,
                },
            )
            if status != 200 or body.get("type") != "success":
                self.last_error = str(body.get("description", body))[:200]
                raise XtsInteractiveError(f"interactive login failed: {self.last_error}")
            result = body.get("result", {})
            self._token = result.get("token")
            self._user_id = result.get("userID")
            self._is_investor = bool(result.get("isInvestorClient", True))
            if not self._token:
                raise XtsInteractiveError("interactive login returned no token")
            self.last_error = ""
            log.info("algo.broker.login_ok", user=self._user_id)

    async def logout(self) -> None:
        if self._token is None:
            return
        try:
            await self._transport(
                "DELETE",
                f"{self._base()}/interactive/user/session",
                {"authorization": self._token},
                None,
            )
        except Exception as e:  # best-effort — the token dies with the day anyway
            log.warning("algo.broker.logout_failed", error=str(e))
        finally:
            self._token = None
            self._user_id = None

    @staticmethod
    def _auth_rejected(status: int, body: Any) -> bool:
        """The live Lakshmishree gateway signals a dead token as HTTP 200 with
        an error body ("Invalid Token", "Token/Authorization not found"), not
        401/403 — observed 2026-08-17. Treat both spellings as a rejection or
        the one-re-login recovery below never fires in production."""
        if status in (401, 403):
            return True
        if isinstance(body, dict) and body.get("type") != "success":
            desc = str(body.get("description", "")).lower()
            if "token" in desc and (
                "invalid" in desc or "expired" in desc or "not found" in desc
            ):
                return True
        return False

    async def _authed(
        self, method: str, path: str, json_body: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        if self._token is None:
            await self.login()
        assert self._token is not None
        status, body = await self._transport(
            method, f"{self._base()}{path}", {"authorization": self._token}, json_body
        )
        if self._auth_rejected(status, body):
            # Token invalidated (daily expiry / second session) — ONE re-login.
            log.warning("algo.broker.token_rejected_relogin")
            self._token = None
            await self.login()
            status, body = await self._transport(
                method, f"{self._base()}{path}", {"authorization": self._token}, json_body
            )
        if status != 200 or body.get("type") != "success":
            desc = str(body.get("description", body))[:200]
            self.last_error = desc
            raise XtsInteractiveError(f"{method} {path} failed: {desc}")
        return body.get("result", {})

    # ----------------------------------------------------------------- orders

    async def place_market_order(
        self,
        *,
        exchange_instrument_id: int,
        side: Literal["BUY", "SELL"],
        quantity: int,
        unique_id: str,
        product: str = "NRML",
        exchange_segment: str = "NSEFO",
    ) -> str:
        """POST /interactive/orders — market order. Returns AppOrderID.

        ``exchange_segment`` must match the contract's venue: NSEFO for NIFTY
        weeklies, BSEFO for SENSEX (hardcoding NSEFO routed SENSEX orders to
        the wrong exchange — fixed 2026-08-18).

        ``product`` NRML is LOAD-BEARING for overnight carry (Pine parity,
        2026-08-19): MIS would be broker-auto-squared around 15:20 and close
        the position out from under the engine. Never change the default to
        MIS while ``overnight_carry`` exists."""
        payload: dict[str, Any] = {
            "exchangeSegment": exchange_segment,
            "exchangeInstrumentID": exchange_instrument_id,
            "productType": product,
            "orderType": "MARKET",
            "orderSide": side,
            "timeInForce": "DAY",
            "disclosedQuantity": 0,
            "orderQuantity": quantity,
            "limitPrice": 0,
            "stopPrice": 0,
            "orderUniqueIdentifier": unique_id[:20],
        }
        if not self._is_investor and self._user_id:
            payload["clientID"] = self._user_id
        result = await self._authed("POST", "/interactive/orders", payload)
        app_order_id = str(result.get("AppOrderID", ""))
        if not app_order_id:
            # The gateway said SUCCESS — an order exists somewhere. Try to
            # recover its id from today's order book by the unique tag we
            # sent; failing that, this is unknown-state, not a refusal.
            try:
                for row in await self.order_book():
                    if str(row.get("OrderUniqueIdentifier", "")) == unique_id[:20]:
                        app_order_id = str(row.get("AppOrderID", ""))
                        if app_order_id:
                            log.warning(
                                "algo.broker.order_id_recovered_from_book",
                                order_id=app_order_id,
                            )
                            break
            except Exception:
                pass
        if not app_order_id:
            raise XtsTransportError(
                "order ACCEPTED but no AppOrderID returned and the order book "
                "lookup failed — treat as possibly executed"
            )
        log.info(
            "algo.broker.order_placed",
            order_id=app_order_id, side=side, qty=quantity,
            instrument=exchange_instrument_id,
        )
        return app_order_id

    async def cancel_order(self, app_order_id: str) -> None:
        """DELETE /interactive/orders — cancel a pending order. A terminal
        order (already filled/rejected) makes the gateway error; the caller
        decides whether that matters."""
        await self._authed("DELETE", f"/interactive/orders?appOrderID={app_order_id}")
        log.info("algo.broker.order_cancelled", order_id=app_order_id)

    async def balance(self) -> dict[str, Any]:
        """GET /interactive/user/balance — raw result (BalanceList envelope)."""
        return await self._authed("GET", "/interactive/user/balance")

    async def positions_net(self) -> list[dict[str, Any]]:
        """GET /interactive/portfolio/positions (NetWise) — position rows."""
        result = await self._authed(
            "GET", "/interactive/portfolio/positions?dayOrNet=NetWise"
        )
        rows = result.get("positionList", []) if isinstance(result, dict) else result
        return rows if isinstance(rows, list) else []

    async def order_book(self) -> list[dict[str, Any]]:
        """GET /interactive/orders — every order placed today (all states)."""
        result = await self._authed("GET", "/interactive/orders")
        return result if isinstance(result, list) else []

    async def order_result(self, app_order_id: str) -> OrderResult:
        """GET /interactive/orders?appOrderID=… — latest state + avg fill."""
        result = await self._authed(
            "GET", f"/interactive/orders?appOrderID={app_order_id}"
        )
        # The order history arrives as a list of state snapshots; the LAST one
        # is current. A bare dict is tolerated for gateway variations.
        snap: dict[str, Any]
        if isinstance(result, list):
            snap = result[-1] if result else {}
        else:
            snap = result
        status = str(snap.get("OrderStatus", ""))
        # The v2 docs name this AverageTradedPrice; the SDK's order book rows
        # carry OrderAverageTradedPrice — accept either.
        avg_raw = snap.get("OrderAverageTradedPrice", snap.get("AverageTradedPrice"))
        try:
            avg = float(avg_raw) if avg_raw not in (None, "", "0", 0) else None
        except (TypeError, ValueError):
            avg = None
        reason = str(
            snap.get("CancelRejectReason") or snap.get("OrderStatusDescription") or ""
        ).strip()
        return OrderResult(
            app_order_id=app_order_id, status=status, average_price=avg, reason=reason
        )

    async def await_fill(
        self, app_order_id: str, *, fallback_price: float
    ) -> tuple[float, str]:
        """Poll briefly for a terminal state; a MARKET order on liquid NIFTY
        weeklies fills in well under the budget. Returns (fill_price, status).
        On a rejection the caller must treat the position as NOT taken."""
        last = OrderResult(app_order_id, "", None)
        for _ in range(_ORDER_POLL_ATTEMPTS):
            last = await self.order_result(app_order_id)
            if last.status in _FINAL_ORDER_STATES:
                break
            await asyncio.sleep(_ORDER_POLL_DELAY_S)
        if last.status == "Filled" and last.average_price:
            return last.average_price, last.status
        if last.status in ("Rejected", "Cancelled"):
            raise XtsInteractiveError(
                f"order {app_order_id} ended {last.status}: "
                f"{last.reason or self.last_error or 'no reason given'}"
            )
        # Still pending after the poll budget — assume the market order will
        # fill; use the decision price and flag loudly for reconciliation.
        log.warning(
            "algo.broker.fill_unconfirmed",
            order_id=app_order_id, status=last.status, fallback=fallback_price,
        )
        return fallback_price, last.status or "Unknown"


_client: Optional[XtsInteractiveClient] = None


def get_interactive_client() -> XtsInteractiveClient:
    global _client
    if _client is None:
        _client = XtsInteractiveClient()
    return _client
