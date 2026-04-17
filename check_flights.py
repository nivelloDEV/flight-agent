import os
import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

# ── Configuration ─────────────────────────────────────────────────────────────
SERPAPI_KEY   = os.environ["SERPAPI_KEY"]

SMTP_HOST     = os.environ["SMTP_HOST"]
SMTP_PORT     = int(os.environ.get("SMTP_PORT", 587))
SMTP_USER     = os.environ["SMTP_USER"]
SMTP_PASSWORD = os.environ["SMTP_PASSWORD"]
EMAIL_TO      = os.environ["EMAIL_TO"]

# Search parameters
ORIGIN        = "CPH"         # Copenhagen
DESTINATION   = "ICN"         # Seoul Incheon
DEPART_DATE   = "2026-10-27"
RETURN_DATE   = "2026-11-01"
ADULTS        = 2
CURRENCY      = "SEK"
# ──────────────────────────────────────────────────────────────────────────────


def search_flights():
    """Search Google Flights via SerpApi for round-trip flights."""
    params = {
        "engine":           "google_flights",
        "departure_id":     ORIGIN,
        "arrival_id":       DESTINATION,
        "outbound_date":    DEPART_DATE,
        "return_date":      RETURN_DATE,
        "adults":           ADULTS,
        "type":             "1",          # 1 = round trip
        "travel_class":     "1",          # 1 = economy
        "currency":         CURRENCY,
        "hl":               "en",
        "api_key":          SERPAPI_KEY,
    }
    resp = requests.get("https://serpapi.com/search", params=params)
    resp.raise_for_status()
    return resp.json()


def parse_flights(data):
    """Extract relevant info from best_flights and other_flights."""
    results = []

    for flight in data.get("best_flights", []) + data.get("other_flights", []):
        legs      = flight.get("flights", [])
        if not legs:
            continue

        price     = flight.get("price")
        total_dur = flight.get("total_duration", 0)  # minutes

        # Outbound: first leg
        out       = legs[0]
        out_dep   = out["departure_airport"]["time"]
        out_arr   = legs[-1]["arrival_airport"]["time"]
        out_stops = len(legs) - 1
        airlines  = list(dict.fromkeys(l["airline"] for l in legs))

        # Baggage — extensions often mention baggage info
        bag_info = []
        for leg in legs:
            for ext in leg.get("extensions", []):
                if "bag" in ext.lower() or "luggage" in ext.lower():
                    bag_info.append(ext)
        bags = bag_info[0] if bag_info else "Check airline website"

        results.append({
            "airlines":   ", ".join(airlines),
            "price":      price,
            "currency":   CURRENCY,
            "out_dep":    out_dep,
            "out_arr":    out_arr,
            "out_stops":  out_stops,
            "duration":   total_dur,
            "bags":       bags,
        })

    # Sort by price, take top 10
    results.sort(key=lambda x: x["price"] or 999999)
    return results[:10]


def fmt_dur(minutes):
    """Convert minutes to e.g. 14h 30m."""
    h = minutes // 60
    m = minutes % 60
    return f"{h}h {m}m" if m else f"{h}h"


def fmt_dt(dt_str):
    """Format datetime string to readable form."""
    try:
        dt = datetime.fromisoformat(dt_str)
        return dt.strftime("%d %b %H:%M")
    except Exception:
        return dt_str


def build_email(offers, price_insights):
    """Build plain-text and HTML email."""
    today   = datetime.now().strftime("%d %b %Y")
    subject = f"✈️ CPH → ICN Flights — {today} — {ADULTS} adults"

    if not offers:
        body_text = "No flights found for your search today. Try again tomorrow."
        return subject, body_text, f"<p>{body_text}</p>"

    cheapest      = offers[0]
    lowest_price  = price_insights.get("lowest_price", cheapest["price"])
    price_level   = price_insights.get("price_level", "").replace("_", " ").title()
    typical_low   = price_insights.get("typical_price_range", [None, None])[0]
    typical_high  = price_insights.get("typical_price_range", [None, None])[1]

    # ── Plain text ──────────────────────────────────────────────────────────
    lines = [
        f"Flight prices CPH → ICN / ICN → CPH",
        f"Dates:  {DEPART_DATE} → {RETURN_DATE}",
        f"Adults: {ADULTS}  |  Currency: {CURRENCY}",
        f"Checked: {today}",
        "",
    ]
    if price_level:
        lines += [
            f"Price insight: {price_level}",
            f"Typical range: {typical_low}–{typical_high} {CURRENCY}",
            "",
        ]
    lines += [
        f"{'─'*60}",
        f"  CHEAPEST: {cheapest['price']} {CURRENCY} ({cheapest['airlines']})",
        f"{'─'*60}",
        "",
    ]
    for i, o in enumerate(offers, 1):
        stops = "Direct" if o["out_stops"] == 0 else f"{o['out_stops']} stop(s)"
        lines += [
            f"#{i}  {o['airlines']}  —  {o['price']} {o['currency']}",
            f"    DEP: {fmt_dt(o['out_dep'])} → ARR: {fmt_dt(o['out_arr'])}",
            f"    {stops}  |  Total duration: {fmt_dur(o['duration'])}",
            f"    Bags: {o['bags']}",
            "",
        ]
    body_text = "\n".join(lines)

    # ── Price level badge color ─────────────────────────────────────────────
    level_color = {
        "Low":     "#1a6e3c",
        "Typical": "#b8860b",
        "High":    "#c0392b",
    }.get(price_level, "#555")

    # ── HTML ────────────────────────────────────────────────────────────────
    rows = ""
    for i, o in enumerate(offers, 1):
        stops = "Direct ✅" if o["out_stops"] == 0 else f"{o['out_stops']} stop(s)"
        bg    = "#eef7ee" if i == 1 else ("#ffffff" if i % 2 == 0 else "#f9f9f9")
        bold  = "font-weight:bold;" if i == 1 else ""
        rows += f"""
        <tr style="background:{bg}">
          <td style="padding:10px;{bold}">{i}</td>
          <td style="padding:10px">{o['airlines']}</td>
          <td style="padding:10px;color:#1a6e3c;font-weight:bold">{o['price']} {o['currency']}</td>
          <td style="padding:10px">
            🛫 {fmt_dt(o['out_dep'])}<br>
            🛬 {fmt_dt(o['out_arr'])}<br>
            <small>{stops} · {fmt_dur(o['duration'])}</small>
          </td>
          <td style="padding:10px;font-size:13px">{o['bags']}</td>
        </tr>"""

    insight_block = ""
    if price_level:
        insight_block = f"""
        <div style="background:#f0f4ff;padding:12px;border-radius:6px;margin-bottom:16px;border-left:4px solid {level_color}">
          📊 <strong>Price level today:</strong>
          <span style="color:{level_color};font-weight:bold">{price_level}</span>
          &nbsp;|&nbsp; Typical range: <strong>{typical_low}–{typical_high} {CURRENCY}</strong>
        </div>"""

    body_html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:860px;margin:auto;color:#333">
      <h2 style="color:#1a3c6e">✈️ Daily Flight Price Update — {today}</h2>
      <p>
        <strong>Route:</strong> Copenhagen (CPH) ⟶ Seoul Incheon (ICN) ⟶ Copenhagen<br>
        <strong>Outbound:</strong> {DEPART_DATE} &nbsp;|&nbsp;
        <strong>Return:</strong> {RETURN_DATE}<br>
        <strong>Passengers:</strong> {ADULTS} adults
      </p>
      {insight_block}
      <div style="background:#e8f4e8;padding:12px;border-radius:6px;margin-bottom:16px">
        💰 <strong>Cheapest today:</strong> {cheapest['price']} {CURRENCY} — {cheapest['airlines']}
      </div>
      <table border="0" cellspacing="0" cellpadding="0"
             style="width:100%;border-collapse:collapse;border:1px solid #ddd;border-radius:6px;overflow:hidden">
        <thead>
          <tr style="background:#1a3c6e;color:white;text-align:left">
            <th style="padding:10px">#</th>
            <th style="padding:10px">Airline(s)</th>
            <th style="padding:10px">Price (2 adults)</th>
            <th style="padding:10px">Times &amp; Stops</th>
            <th style="padding:10px">Baggage</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
      <p style="color:#999;font-size:12px;margin-top:16px">
        ⚠️ Prices sourced from Google Flights via SerpApi. Always verify and book directly
        with the airline or a booking site. Baggage allowance may vary — check airline terms.
      </p>
    </body></html>"""

    return subject, body_text, body_html


def send_email(subject, body_text, body_html):
    """Send email via SMTP."""
    msg            = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_USER
    msg["To"]      = EMAIL_TO

    msg.attach(MIMEText(body_text, "plain"))
    msg.attach(MIMEText(body_html,  "html"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, EMAIL_TO, msg.as_string())

    print(f"✅ Email sent to {EMAIL_TO}")


def main():
    print(f"🔍 Searching Google Flights: {ORIGIN} → {DESTINATION} "
          f"({DEPART_DATE} – {RETURN_DATE}), {ADULTS} adults...")

    data           = search_flights()
    offers         = parse_flights(data)
    price_insights = data.get("price_insights", {})

    print(f"   Found {len(offers)} offers. "
          f"Cheapest: {offers[0]['price'] if offers else 'N/A'} {CURRENCY}")

    subject, body_text, body_html = build_email(offers, price_insights)
    send_email(subject, body_text, body_html)


if __name__ == "__main__":
    main()
