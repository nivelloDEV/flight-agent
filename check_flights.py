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
MAX_STOPS     = 1             # max 1 stop each way
# ──────────────────────────────────────────────────────────────────────────────


def search_outbound():
    """Step 1: Search outbound flights CPH → ICN."""
    params = {
        "engine":        "google_flights",
        "departure_id":  ORIGIN,
        "arrival_id":    DESTINATION,
        "outbound_date": DEPART_DATE,
        "return_date":   RETURN_DATE,
        "adults":        ADULTS,
        "type":          "1",    # round trip
        "travel_class":  "1",    # economy
        "stops":         "2",    # 2 = max 1 stop
        "currency":      CURRENCY,
        "hl":            "en",
        "api_key":       SERPAPI_KEY,
    }
    resp = requests.get("https://serpapi.com/search", params=params)
    resp.raise_for_status()
    return resp.json()


def search_return(departure_token):
    """Step 2: Search return flights ICN → CPH using departure_token."""
    params = {
        "engine":           "google_flights",
        "departure_id":     ORIGIN,
        "arrival_id":       DESTINATION,
        "outbound_date":    DEPART_DATE,
        "return_date":      RETURN_DATE,
        "adults":           ADULTS,
        "type":             "1",
        "travel_class":     "1",
        "stops":            "2",    # 2 = max 1 stop
        "currency":         CURRENCY,
        "hl":               "en",
        "departure_token":  departure_token,
        "api_key":          SERPAPI_KEY,
    }
    resp = requests.get("https://serpapi.com/search", params=params)
    resp.raise_for_status()
    return resp.json()


def extract_legs(flight_entry):
    """Extract dep/arr times, stops, duration and airlines from a flight entry."""
    legs     = flight_entry.get("flights", [])
    dep      = legs[0]["departure_airport"]["time"] if legs else "N/A"
    arr      = legs[-1]["arrival_airport"]["time"]  if legs else "N/A"
    stops    = len(legs) - 1
    duration = flight_entry.get("total_duration", 0)
    airlines = list(dict.fromkeys(l["airline"] for l in legs))

    bag_info = []
    for leg in legs:
        for ext in leg.get("extensions", []):
            if "bag" in ext.lower() or "luggage" in ext.lower():
                bag_info.append(ext)
    bags = bag_info[0] if bag_info else "Check airline website"

    return {
        "dep":      dep,
        "arr":      arr,
        "stops":    stops,
        "duration": duration,
        "airlines": ", ".join(airlines),
        "bags":     bags,
    }


def parse_outbound(data):
    """
    Return list of (outbound_info, departure_token, price, price_insights).
    Only includes flights with <= MAX_STOPS stops.
    """
    results        = []
    price_insights = data.get("price_insights", {})

    for flight in data.get("best_flights", []) + data.get("other_flights", []):
        legs = flight.get("flights", [])
        if len(legs) - 1 > MAX_STOPS:
            continue
        token = flight.get("departure_token")
        if not token:
            continue
        out   = extract_legs(flight)
        price = flight.get("price", 0)
        results.append((out, token, price))

    # Sort by price, take top 5 (each needs a separate return API call)
    results.sort(key=lambda x: x[2])
    return results[:5], price_insights


def parse_return(data):
    """Return list of return flight options with <= MAX_STOPS stops."""
    results = []
    for flight in data.get("best_flights", []) + data.get("other_flights", []):
        legs = flight.get("flights", [])
        if len(legs) - 1 > MAX_STOPS:
            continue
        ret   = extract_legs(flight)
        price = flight.get("price", 0)
        results.append((ret, price))
    results.sort(key=lambda x: x[1])
    return results[:3]   # top 3 return options per outbound


def fmt_dur(minutes):
    h = minutes // 60
    m = minutes % 60
    return f"{h}h {m}m" if m else f"{h}h"


def fmt_dt(dt_str):
    try:
        return datetime.fromisoformat(dt_str).strftime("%d %b %H:%M")
    except Exception:
        return dt_str


def stops_label(n):
    if n == 0:
        return "Direct ✅"
    return f"{n} stop{'s' if n > 1 else ''}"


def build_email(combos, price_insights):
    """
    combos = list of dicts with keys:
      out_airlines, out_dep, out_arr, out_stops, out_dur, out_bags,
      ret_airlines, ret_dep, ret_arr, ret_stops, ret_dur, ret_bags,
      total_price
    """
    today   = datetime.now().strftime("%d %b %Y")
    subject = f"✈️ CPH ⟶ ICN Flights — {today} — {ADULTS} adults, max 1 stop"

    if not combos:
        msg = "No flights found matching your criteria today (max 1 stop). Try again tomorrow."
        return subject, msg, f"<p>{msg}</p>"

    cheapest    = combos[0]
    price_level = price_insights.get("price_level", "").replace("_", " ").title()
    typ_low     = price_insights.get("typical_price_range", [None, None])[0]
    typ_high    = price_insights.get("typical_price_range", [None, None])[1]

    level_color = {"Low": "#1a6e3c", "Typical": "#b8860b", "High": "#c0392b"}.get(price_level, "#555")

    # ── Plain text ──────────────────────────────────────────────────────────
    lines = [
        "Flight prices CPH → ICN / ICN → CPH",
        f"Dates:  {DEPART_DATE} → {RETURN_DATE}",
        f"Adults: {ADULTS}  |  Max stops: {MAX_STOPS}  |  Currency: {CURRENCY}",
        f"Checked: {today}", "",
    ]
    if price_level:
        lines += [f"Price level: {price_level}  |  Typical: {typ_low}–{typ_high} {CURRENCY}", ""]
    lines += [
        "─" * 65,
        f"  CHEAPEST: {cheapest['total_price']} {CURRENCY}",
        f"  OUT: {cheapest['out_airlines']}  RET: {cheapest['ret_airlines']}",
        "─" * 65, "",
    ]
    for i, c in enumerate(combos, 1):
        lines += [
            f"#{i}  Total: {c['total_price']} {CURRENCY}",
            f"    ✈ OUT  {c['out_airlines']}",
            f"         {fmt_dt(c['out_dep'])} → {fmt_dt(c['out_arr'])}  "
            f"({stops_label(c['out_stops'])}, {fmt_dur(c['out_dur'])})",
            f"         Bags: {c['out_bags']}",
            f"    ✈ RET  {c['ret_airlines']}",
            f"         {fmt_dt(c['ret_dep'])} → {fmt_dt(c['ret_arr'])}  "
            f"({stops_label(c['ret_stops'])}, {fmt_dur(c['ret_dur'])})",
            f"         Bags: {c['ret_bags']}", "",
        ]
    body_text = "\n".join(lines)

    # ── HTML rows ───────────────────────────────────────────────────────────
    rows = ""
    for i, c in enumerate(combos, 1):
        bg   = "#eef7ee" if i == 1 else ("#ffffff" if i % 2 == 0 else "#f9f9f9")
        bold = "font-weight:bold;" if i == 1 else ""
        rows += f"""
        <tr style="background:{bg}">
          <td style="padding:10px;{bold}">{i}</td>
          <td style="padding:10px;color:#1a6e3c;font-weight:bold;white-space:nowrap">
            {c['total_price']} {CURRENCY}
          </td>
          <td style="padding:10px">
            <strong>🛫 {c['out_airlines']}</strong><br>
            {fmt_dt(c['out_dep'])} → {fmt_dt(c['out_arr'])}<br>
            <small>{stops_label(c['out_stops'])} · {fmt_dur(c['out_dur'])}</small><br>
            <small style="color:#666">🧳 {c['out_bags']}</small>
          </td>
          <td style="padding:10px">
            <strong>🛬 {c['ret_airlines']}</strong><br>
            {fmt_dt(c['ret_dep'])} → {fmt_dt(c['ret_arr'])}<br>
            <small>{stops_label(c['ret_stops'])} · {fmt_dur(c['ret_dur'])}</small><br>
            <small style="color:#666">🧳 {c['ret_bags']}</small>
          </td>
        </tr>"""

    insight_block = ""
    if price_level:
        insight_block = f"""
        <div style="background:#f0f4ff;padding:12px;border-radius:6px;
                    margin-bottom:16px;border-left:4px solid {level_color}">
          📊 <strong>Price level today:</strong>
          <span style="color:{level_color};font-weight:bold">{price_level}</span>
          &nbsp;|&nbsp; Typical range:
          <strong>{typ_low}–{typ_high} {CURRENCY}</strong>
        </div>"""

    body_html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:900px;margin:auto;color:#333">
      <h2 style="color:#1a3c6e">✈️ Daily Flight Price Update — {today}</h2>
      <p>
        <strong>Route:</strong> Copenhagen (CPH) ⟶ Seoul Incheon (ICN) ⟶ Copenhagen<br>
        <strong>Outbound:</strong> {DEPART_DATE} &nbsp;|&nbsp;
        <strong>Return:</strong> {RETURN_DATE}<br>
        <strong>Passengers:</strong> {ADULTS} adults &nbsp;|&nbsp;
        <strong>Max stops:</strong> {MAX_STOPS} each way
      </p>
      {insight_block}
      <div style="background:#e8f4e8;padding:12px;border-radius:6px;margin-bottom:16px">
        💰 <strong>Cheapest today:</strong> {cheapest['total_price']} {CURRENCY}
        — Out: {cheapest['out_airlines']} / Ret: {cheapest['ret_airlines']}
      </div>
      <table border="0" cellspacing="0" cellpadding="0"
             style="width:100%;border-collapse:collapse;border:1px solid #ddd;
                    border-radius:6px;overflow:hidden">
        <thead>
          <tr style="background:#1a3c6e;color:white;text-align:left">
            <th style="padding:10px">#</th>
            <th style="padding:10px">Total price<br><small>(2 adults)</small></th>
            <th style="padding:10px">✈ Outbound (CPH → ICN)</th>
            <th style="padding:10px">✈ Return (ICN → CPH)</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
      <p style="color:#999;font-size:12px;margin-top:16px">
        ⚠️ Prices from Google Flights via SerpApi. Prices shown are estimates for the
        outbound leg — total round-trip price may differ. Always verify on the airline
        or booking site before purchasing.
      </p>
    </body></html>"""

    return subject, body_text, body_html


def send_email(subject, body_text, body_html):
    msg            = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_USER
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(body_text, "plain"))
    msg.attach(MIMEText(body_html, "html"))
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, EMAIL_TO, msg.as_string())
    print(f"✅ Email sent to {EMAIL_TO}")


def main():
    print(f"🔍 Step 1: Searching outbound flights {ORIGIN} → {DESTINATION} "
          f"({DEPART_DATE}), max {MAX_STOPS} stop(s)...")

    out_data, price_insights = parse_outbound(search_outbound())
    print(f"   Found {len(out_data)} outbound options")

    combos = []
    for idx, (out, token, out_price) in enumerate(out_data, 1):
        print(f"   Step 2.{idx}: Fetching return flights for outbound option #{idx}...")
        try:
            ret_data    = search_return(token)
            ret_options = parse_return(ret_data)
            if ret_options:
                # Pick cheapest return for this outbound
                ret, ret_price = ret_options[0]
                combos.append({
                    "out_airlines": out["airlines"],
                    "out_dep":      out["dep"],
                    "out_arr":      out["arr"],
                    "out_stops":    out["stops"],
                    "out_dur":      out["duration"],
                    "out_bags":     out["bags"],
                    "ret_airlines": ret["airlines"],
                    "ret_dep":      ret["dep"],
                    "ret_arr":      ret["arr"],
                    "ret_stops":    ret["stops"],
                    "ret_dur":      ret["duration"],
                    "ret_bags":     ret["bags"],
                    "total_price":  ret_price,  # SerpApi returns combined price in return search
                })
        except Exception as e:
            print(f"   Warning: could not fetch return for option #{idx}: {e}")

    combos.sort(key=lambda x: x["total_price"])
    print(f"   Built {len(combos)} complete outbound+return combinations")
    if combos:
        print(f"   Cheapest: {combos[0]['total_price']} {CURRENCY}")

    subject, body_text, body_html = build_email(combos, price_insights)
    send_email(subject, body_text, body_html)


if __name__ == "__main__":
    main()
