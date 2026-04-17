import os
import requests
import smtplib
import json
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from pathlib import Path

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

# Alert threshold — only send email if price is below this (set to None to always send)
PRICE_ALERT_THRESHOLD = None   # e.g. 8000 to only alert when price < 8000 SEK

# Price history file
HISTORY_FILE  = "price_history.json"

# Retry settings
MAX_RETRIES   = 3
RETRY_DELAY   = 10  # seconds between retries
# ──────────────────────────────────────────────────────────────────────────────


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


def api_get(params, context=""):
    """GET request to SerpApi with retry logic."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get("https://serpapi.com/search", params=params, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"   ⚠️ Attempt {attempt}/{MAX_RETRIES} failed{' (' + context + ')' if context else ''}: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
    raise RuntimeError(f"All {MAX_RETRIES} attempts failed{' for ' + context if context else ''}")


def search_outbound():
    params = {
        "engine":        "google_flights",
        "departure_id":  ORIGIN,
        "arrival_id":    DESTINATION,
        "outbound_date": DEPART_DATE,
        "return_date":   RETURN_DATE,
        "adults":        ADULTS,
        "type":          "1",
        "travel_class":  "1",
        "stops":         "2",    # max 1 stop
        "currency":      CURRENCY,
        "hl":            "en",
        "api_key":       SERPAPI_KEY,
    }
    return api_get(params, "outbound search")


def search_return(departure_token):
    params = {
        "engine":          "google_flights",
        "departure_id":    ORIGIN,
        "arrival_id":      DESTINATION,
        "outbound_date":   DEPART_DATE,
        "return_date":     RETURN_DATE,
        "adults":          ADULTS,
        "type":            "1",
        "travel_class":    "1",
        "stops":           "2",    # max 1 stop
        "currency":        CURRENCY,
        "hl":              "en",
        "departure_token": departure_token,
        "api_key":         SERPAPI_KEY,
    }
    return api_get(params, "return search")


def google_flights_url():
    """Build a direct Google Flights link for the search."""
    base = "https://www.google.com/travel/flights"
    return (f"{base}?q=Flights+from+{ORIGIN}+to+{DESTINATION}"
            f"&curr={CURRENCY}&hl=en")


def extract_legs(flight_entry):
    legs     = flight_entry.get("flights", [])
    dep      = legs[0]["departure_airport"]["time"] if legs else "N/A"
    arr      = legs[-1]["arrival_airport"]["time"]  if legs else "N/A"
    stops    = len(legs) - 1
    duration = flight_entry.get("total_duration", 0)
    airlines = list(dict.fromkeys(l["airline"] for l in legs))

    # Aircraft types
    aircraft = list(dict.fromkeys(
        l.get("airplane", "") for l in legs if l.get("airplane")
    ))

    # Legroom
    legrooms = [l.get("legroom", "") for l in legs if l.get("legroom")]

    # CO2
    co2_list = []
    for leg in legs:
        ce = leg.get("carbon_emissions", {})
        if ce.get("this_flight"):
            co2_list.append(ce["this_flight"])
    co2_total = sum(co2_list) // 1000 if co2_list else None  # grams → kg

    # Layovers
    layovers_raw = flight_entry.get("layovers", [])
    layover_strs = []
    for i, leg in enumerate(legs[:-1]):
        code = leg["arrival_airport"].get("id", "")
        name = leg["arrival_airport"].get("name", "")
        if i < len(layovers_raw):
            lay_min = layovers_raw[i].get("duration", 0)
            lay_str = fmt_dur(lay_min) if lay_min else ""
        else:
            lay_str = ""
        entry = f"{code} ({name})"
        if lay_str:
            entry += f" — {lay_str} layover"
        layover_strs.append(entry)

    # Baggage
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
        "layovers": layover_strs,
        "duration": duration,
        "airlines": ", ".join(airlines),
        "aircraft": ", ".join(aircraft) if aircraft else "—",
        "legroom":  legrooms[0] if legrooms else "—",
        "co2_kg":   co2_total,
        "bags":     bags,
    }


def parse_outbound(data):
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
    results.sort(key=lambda x: x[2])
    return results[:5], price_insights


def parse_return(data):
    results = []
    for flight in data.get("best_flights", []) + data.get("other_flights", []):
        legs = flight.get("flights", [])
        if len(legs) - 1 > MAX_STOPS:
            continue
        ret   = extract_legs(flight)
        price = flight.get("price", 0)
        results.append((ret, price))
    results.sort(key=lambda x: x[1])
    return results[:3]


# ── Price history ──────────────────────────────────────────────────────────────

def load_history():
    p = Path(HISTORY_FILE)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return []


def save_history(history, price):
    today = datetime.now().strftime("%Y-%m-%d")
    history = [h for h in history if h["date"] != today]  # remove today if exists
    history.append({"date": today, "price": price})
    history = sorted(history, key=lambda x: x["date"])[-30:]  # keep last 30 days
    Path(HISTORY_FILE).write_text(json.dumps(history, indent=2))
    return history


def price_trend(history):
    """Return trend string compared to yesterday."""
    if len(history) < 2:
        return ""
    today_price     = history[-1]["price"]
    yesterday_price = history[-2]["price"]
    diff = today_price - yesterday_price
    if diff < 0:
        return f"📉 {abs(diff):,} {CURRENCY} cheaper than yesterday"
    elif diff > 0:
        return f"📈 {diff:,} {CURRENCY} more expensive than yesterday"
    else:
        return "➡️ Same price as yesterday"


def build_history_html(history):
    if not history:
        return ""
    rows = ""
    prices = [h["price"] for h in history]
    min_p  = min(prices)
    max_p  = max(prices)
    for h in reversed(history[-10:]):  # last 10 days
        is_min = h["price"] == min_p
        is_max = h["price"] == max_p
        color  = "#1a6e3c" if is_min else ("#c0392b" if is_max else "#333")
        badge  = " 🏆" if is_min else (" ⚠️" if is_max else "")
        rows += (f'<tr><td style="padding:6px 10px">{h["date"]}</td>'
                 f'<td style="padding:6px 10px;color:{color};font-weight:{"bold" if is_min else "normal"}">'
                 f'{h["price"]:,} {CURRENCY}{badge}</td></tr>')
    return f"""
    <details style="margin-bottom:16px">
      <summary style="cursor:pointer;font-weight:bold;color:#1a3c6e">
        📊 Price history (last {min(len(history), 10)} days)
      </summary>
      <table style="margin-top:8px;border-collapse:collapse;font-size:13px">
        <thead><tr style="background:#f0f0f0">
          <th style="padding:6px 10px;text-align:left">Date</th>
          <th style="padding:6px 10px;text-align:left">Lowest price</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </details>"""


# ── Email ──────────────────────────────────────────────────────────────────────

def build_email(combos, price_insights, history):
    today   = datetime.now().strftime("%d %b %Y")
    subject = f"✈️ CPH ⟶ ICN — {today} — {ADULTS} adults, max 1 stop"

    if not combos:
        msg = "No flights found matching your criteria today (max 1 stop). Try again tomorrow."
        return subject, msg, f"<p>{msg}</p>"

    cheapest    = combos[0]
    price_level = price_insights.get("price_level", "").replace("_", " ").title()
    typ_low     = price_insights.get("typical_price_range", [None, None])[0]
    typ_high    = price_insights.get("typical_price_range", [None, None])[1]
    level_color = {"Low": "#1a6e3c", "Typical": "#b8860b", "High": "#c0392b"}.get(price_level, "#555")
    trend       = price_trend(history)
    gf_url      = google_flights_url()

    # ── Plain text ──────────────────────────────────────────────────────────
    lines = [
        "Flight prices CPH → ICN / ICN → CPH",
        f"Dates:  {DEPART_DATE} → {RETURN_DATE}",
        f"Adults: {ADULTS}  |  Max stops: {MAX_STOPS}  |  Currency: {CURRENCY}",
        f"Checked: {today}", "",
    ]
    if price_level:
        lines += [f"Price level: {price_level}  |  Typical: {typ_low}–{typ_high} {CURRENCY}"]
    if trend:
        lines += [trend]
    lines += [
        f"Google Flights: {gf_url}", "",
        "─" * 65,
        f"  CHEAPEST: {cheapest['total_price']:,} {CURRENCY}",
        "─" * 65, "",
    ]
    for i, c in enumerate(combos, 1):
        out_via = ("via " + " → ".join(c["out_layovers"])) if c["out_layovers"] else ""
        ret_via = ("via " + " → ".join(c["ret_layovers"])) if c["ret_layovers"] else ""
        co2_out = f"{c['out_co2_kg']} kg CO₂" if c["out_co2_kg"] else ""
        co2_ret = f"{c['ret_co2_kg']} kg CO₂" if c["ret_co2_kg"] else ""
        lines += [
            f"#{i}  Total: {c['total_price']:,} {CURRENCY}",
            f"    ✈ OUT  {c['out_airlines']}  ({c['out_aircraft']})",
            f"         {fmt_dt(c['out_dep'])} → {fmt_dt(c['out_arr'])}  "
            f"({stops_label(c['out_stops'])}, {fmt_dur(c['out_dur'])})",
        ]
        if out_via:   lines.append(f"         {out_via}")
        if co2_out:   lines.append(f"         {co2_out}")
        lines += [
            f"         Legroom: {c['out_legroom']}  |  Bags: {c['out_bags']}",
            f"    ✈ RET  {c['ret_airlines']}  ({c['ret_aircraft']})",
            f"         {fmt_dt(c['ret_dep'])} → {fmt_dt(c['ret_arr'])}  "
            f"({stops_label(c['ret_stops'])}, {fmt_dur(c['ret_dur'])})",
        ]
        if ret_via:   lines.append(f"         {ret_via}")
        if co2_ret:   lines.append(f"         {co2_ret}")
        lines += [f"         Legroom: {c['ret_legroom']}  |  Bags: {c['ret_bags']}", ""]
    body_text = "\n".join(lines)

    # ── HTML rows ───────────────────────────────────────────────────────────
    rows = ""
    for i, c in enumerate(combos, 1):
        bg   = "#eef7ee" if i == 1 else ("#ffffff" if i % 2 == 0 else "#f9f9f9")
        bold = "font-weight:bold;" if i == 1 else ""

        def via_html(layovers):
            return (f'<br><small style="color:#888">🔁 via {" → ".join(layovers)}</small>'
                    if layovers else "")

        def co2_html(kg):
            return f'<br><small style="color:#555">🌿 {kg} kg CO₂</small>' if kg else ""

        rows += f"""
        <tr style="background:{bg}">
          <td style="padding:10px;{bold}">{i}</td>
          <td style="padding:10px;color:#1a6e3c;font-weight:bold;white-space:nowrap">
            {c['total_price']:,} {CURRENCY}
          </td>
          <td style="padding:10px">
            <strong>🛫 {c['out_airlines']}</strong><br>
            {fmt_dt(c['out_dep'])} → {fmt_dt(c['out_arr'])}<br>
            <small>{stops_label(c['out_stops'])} · {fmt_dur(c['out_dur'])}</small>
            {via_html(c['out_layovers'])}
            {co2_html(c['out_co2_kg'])}<br>
            <small>✈ {c['out_aircraft']} · 💺 {c['out_legroom']}</small><br>
            <small style="color:#666">🧳 {c['out_bags']}</small>
          </td>
          <td style="padding:10px">
            <strong>🛬 {c['ret_airlines']}</strong><br>
            {fmt_dt(c['ret_dep'])} → {fmt_dt(c['ret_arr'])}<br>
            <small>{stops_label(c['ret_stops'])} · {fmt_dur(c['ret_dur'])}</small>
            {via_html(c['ret_layovers'])}
            {co2_html(c['ret_co2_kg'])}<br>
            <small>✈ {c['ret_aircraft']} · 💺 {c['ret_legroom']}</small><br>
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
          <strong>{typ_low:,}–{typ_high:,} {CURRENCY}</strong>
          {f'&nbsp;|&nbsp; {trend}' if trend else ''}
        </div>"""

    history_block = build_history_html(history)

    body_html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:920px;margin:auto;color:#333">
      <h2 style="color:#1a3c6e">✈️ Daily Flight Price Update — {today}</h2>
      <p>
        <strong>Route:</strong> Copenhagen (CPH) ⟶ Seoul Incheon (ICN) ⟶ Copenhagen<br>
        <strong>Outbound:</strong> {DEPART_DATE} &nbsp;|&nbsp;
        <strong>Return:</strong> {RETURN_DATE}<br>
        <strong>Passengers:</strong> {ADULTS} adults &nbsp;|&nbsp;
        <strong>Max stops:</strong> {MAX_STOPS} each way<br>
        <a href="{gf_url}" style="color:#1a3c6e">🔗 Open in Google Flights</a>
      </p>
      {insight_block}
      <div style="background:#e8f4e8;padding:12px;border-radius:6px;margin-bottom:16px">
        💰 <strong>Cheapest today:</strong> {cheapest['total_price']:,} {CURRENCY}
        — Out: {cheapest['out_airlines']} / Ret: {cheapest['ret_airlines']}
      </div>
      {history_block}
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
        ⚠️ Prices from Google Flights via SerpApi. Always verify on the airline
        or booking site before purchasing. Baggage allowance may vary.
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


def send_error_email(error_msg):
    """Send a notification email if the script crashes."""
    try:
        today   = datetime.now().strftime("%d %b %Y %H:%M")
        subject = f"⚠️ Flight agent error — {today}"
        body    = f"The flight price agent encountered an error:\n\n{error_msg}"
        msg            = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = SMTP_USER
        msg["To"]      = EMAIL_TO
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, EMAIL_TO, msg.as_string())
        print("⚠️ Error notification email sent")
    except Exception as e:
        print(f"Could not send error email: {e}")


def main():
    try:
        print(f"🔍 Step 1: Searching outbound {ORIGIN} → {DESTINATION} "
              f"({DEPART_DATE}), max {MAX_STOPS} stop(s)...")

        out_data, price_insights = parse_outbound(search_outbound())
        print(f"   Found {len(out_data)} outbound options")

        combos = []
        for idx, (out, token, out_price) in enumerate(out_data, 1):
            print(f"   Step 2.{idx}: Fetching return flights for outbound #{idx}...")
            try:
                ret_data    = search_return(token)
                ret_options = parse_return(ret_data)
                if ret_options:
                    ret, ret_price = ret_options[0]
                    combos.append({
                        "out_airlines": out["airlines"],
                        "out_dep":      out["dep"],
                        "out_arr":      out["arr"],
                        "out_stops":    out["stops"],
                        "out_layovers": out["layovers"],
                        "out_dur":      out["duration"],
                        "out_aircraft": out["aircraft"],
                        "out_legroom":  out["legroom"],
                        "out_co2_kg":   out["co2_kg"],
                        "out_bags":     out["bags"],
                        "ret_airlines": ret["airlines"],
                        "ret_dep":      ret["dep"],
                        "ret_arr":      ret["arr"],
                        "ret_stops":    ret["stops"],
                        "ret_layovers": ret["layovers"],
                        "ret_dur":      ret["duration"],
                        "ret_aircraft": ret["aircraft"],
                        "ret_legroom":  ret["legroom"],
                        "ret_co2_kg":   ret["co2_kg"],
                        "ret_bags":     ret["bags"],
                        "total_price":  ret_price,
                    })
            except Exception as e:
                print(f"   Warning: could not fetch return for option #{idx}: {e}")

        combos.sort(key=lambda x: x["total_price"])
        print(f"   Built {len(combos)} complete combinations")

        # Price history
        history = load_history()
        if combos:
            cheapest_price = combos[0]["total_price"]
            print(f"   Cheapest: {cheapest_price:,} {CURRENCY}")

            # Check threshold
            if PRICE_ALERT_THRESHOLD and cheapest_price >= PRICE_ALERT_THRESHOLD:
                print(f"   Price {cheapest_price:,} is above threshold {PRICE_ALERT_THRESHOLD:,} — skipping email")
                save_history(history, cheapest_price)
                return

            history = save_history(history, cheapest_price)

        subject, body_text, body_html = build_email(combos, price_insights, history)
        send_email(subject, body_text, body_html)

    except Exception as e:
        print(f"❌ Fatal error: {e}")
        send_error_email(str(e))
        raise


if __name__ == "__main__":
    main()
