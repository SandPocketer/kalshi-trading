"""Pure calculations for complete weather-event paper baskets."""

from decimal import Decimal

OPEN = {"active", "open"}


def complete_weather_event(event: dict) -> bool:
    """Validate contiguous integer Fahrenheit ranges including both tails."""
    if event["event_ticker"].split("-")[0] not in {"KXHIGHLAX", "KXHIGHNY", "KXHIGHCHI"} or not event["mutually_exclusive"]:
        return False
    markets = event["markets"]
    if len(markets) < 3 or len({market["ticker"] for market in markets}) != len(markets):
        return False
    if any(market["event_ticker"] != event["event_ticker"] for market in markets):
        return False
    lower = [market["cap_strike"] for market in markets if market["strike_type"] == "less"]
    upper = [market["floor_strike"] for market in markets if market["strike_type"] == "greater"]
    middle = [(market["floor_strike"], market["cap_strike"])
              for market in markets if market["strike_type"] == "between"]
    if len(lower) != 1 or len(upper) != 1 or len(middle) != len(markets) - 2:
        return False
    bounds = lower + upper + [value for interval in middle for value in interval]
    if any(not isinstance(value, (int, float)) or not float(value).is_integer()
           for value in bounds):
        return False
    next_floor = lower[0]
    for floor, cap in sorted(middle):
        if floor != next_floor or cap < floor:
            return False
        next_floor = cap + 1
    return next_floor == upper[0] + 1


def scan_basket(event: dict, fee: Decimal, timestamp: int) -> dict:
    result = {"event_ticker": event["event_ticker"], "ts_ms": timestamp}
    if not complete_weather_event(event):
        return {**result, "status": "unverified coverage"}
    markets = event["markets"]
    if any(market["status"] not in OPEN for market in markets):
        return {**result, "status": "some legs are closed"}
    asks = [Decimal(market["yes_ask_dollars"]) for market in markets]
    sizes = [Decimal(market["yes_ask_size_fp"]) for market in markets]
    if (any(not ask.is_finite() or not 0 < ask <= 1 for ask in asks)
            or any(not size.is_finite() or size < 1 for size in sizes)):
        return {**result, "status": "insufficient displayed size or missing ask"}
    cost = sum(asks)
    fees = fee * len(markets)
    net = Decimal(1) - cost - fees
    return {
        **result,
        "status": "candidate" if net > 0 else "no edge",
        "legs": {market["ticker"]: str(ask) for market, ask in zip(markets, asks)},
        "ask_cost": str(cost),
        "assumed_fees": str(fees),
        "net_if_filled": str(net),
        "minimum_ask_size": str(min(sizes)),
    }
