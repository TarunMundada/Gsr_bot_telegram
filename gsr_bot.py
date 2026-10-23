import sys
import os
import json
import numpy as np
import pandas as pd
import yfinance as yf
import requests

# --- CONFIGURATION & ENV VARS ---
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

GSR_HIGH = float(os.getenv("GSR_HIGH", "90.0"))
GSR_LOW = float(os.getenv("GSR_LOW", "70.0"))
GSR_MOVE_PCT = float(os.getenv("GSR_MOVE_PCT", "2.0"))
GSR_Z = float(os.getenv("GSR_Z", "2.0"))
GSR_COOLDOWN_HRS = int(os.getenv("GSR_COOLDOWN_HRS", "24"))

HISTORY_FILE = "history.json"
STATE_FILE = "state.json"

TROY_OUNCE_TO_GRAMS = 31.1034768

def send_telegram_message(text):
    if not BOT_TOKEN or not CHAT_ID:
        print("[ERROR] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set.")
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
    res = requests.post(url, json=payload)
    return res.status_code == 200

def fetch_market_data():
    """
    Fetches Gold, Silver, and USD/INR rates using yfinance.
    Returns: dict with gold_inr_10g, silver_inr_1kg, gsr, and data_date
    """
    tickers = yf.Tickers("GC=F SI=F USDINR=X")
    
    # Download latest 1-day data
    df = yf.download(["GC=F", "SI=F", "USDINR=X"], period="5d", interval="1d", progress=False)['Close']
    df = df.dropna()
    
    latest = df.iloc[-1]
    gold_usd_oz = float(latest["GC=F"])
    silver_usd_oz = float(latest["SI=F"])
    usd_inr = float(latest["USDINR=X"])
    
    # Calculate GSR
    gsr = gold_usd_oz / silver_usd_oz
    
    # Convert to INR units (Gold per 10g, Silver per 1kg)
    gold_inr_gram = (gold_usd_oz / TROY_OUNCE_TO_GRAMS) * usd_inr
    gold_inr_10g = gold_inr_gram * 10
    
    silver_inr_gram = (silver_usd_oz / TROY_OUNCE_TO_GRAMS) * usd_inr
    silver_inr_1kg = silver_inr_gram * 1000
    
    return {
        "date": str(df.index[-1].date()),
        "gsr": round(gsr, 2),
        "gold_inr_10g": round(gold_inr_10g, 2),
        "silver_inr_1kg": round(silver_inr_1kg, 2),
        "usd_inr": round(usd_inr, 2)
    }

def update_history():
    """Backfills 1 year of daily historical data if history.json doesn't exist."""
    if not os.path.exists(HISTORY_FILE):
        print("[INFO] Backfilling 1 year of daily market history...")
        df = yf.download(["GC=F", "SI=F"], period="1y", interval="1d", progress=False)['Close']
        df = df.dropna()
        df['GSR'] = df['GC=F'] / df['SI=F']
        
        history = {}
        for idx, row in df.iterrows():
            history[str(idx.date())] = round(float(row['GSR']), 2)
            
        with open(HISTORY_FILE, "w") as f:
            json.dump(history, f, indent=2)
        return history
    else:
        with open(HISTORY_FILE, "r") as f:
            return json.load(f)

def compute_metrics(history, current_gsr):
    vals = list(history.values())
    if len(vals) < 30:
        return {"z_score": "n/a", "percentile": "n/a", "pct_change_30d": "n/a"}
    
    # 60-day stats
    recent_60 = vals[-60:]
    mean = np.mean(recent_60)
    std = np.std(recent_60)
    z_score = (current_gsr - mean) / std if std != 0 else 0
    
    # 30-day change
    val_30d_ago = vals[-30]
    pct_change_30d = ((current_gsr - val_30d_ago) / val_30d_ago) * 100
    
    # Percentile in 1 year
    percentile = (sum(1 for v in vals if v <= current_gsr) / len(vals)) * 100
    
    return {
        "z_score": round(z_score, 2),
        "percentile": round(percentile, 1),
        "pct_change_30d": round(pct_change_30d, 2)
    }

def build_message(data, metrics, alert_header=None):
    """Constructs the comprehensive, visually organized Telegram message."""
    z_val = metrics["z_score"]
    
    # Dynamic Signal Mapping
    if isinstance(z_val, (int, float)):
        if z_val >= GSR_Z:
            signal_title = "🔴 SHORT GSR / FAVOR SILVER"
            status_text = f"Extreme Upper Bound (+{z_val}σ)"
            action_text = (
                "• Bullion Shift: Accumulate Silver / Pause Gold buying\n"
                "• Swap Strategy: High ratio favors converting Gold ➔ Silver\n"
                "• Pair Trade Bias: Short Gold / Long Silver (MCX Spread)"
            )
        elif z_val <= -GSR_Z:
            signal_title = "🟢 LONG GSR / FAVOR GOLD"
            status_text = f"Extreme Lower Bound ({z_val}σ)"
            action_text = (
                "• Bullion Shift: Accumulate Gold / Pause Silver buying\n"
                "• Swap Strategy: Low ratio favors converting Silver ➔ Gold\n"
                "• Pair Trade Bias: Long Gold / Short Silver (MCX Spread)"
            )
        else:
            signal_title = "⚪ NEUTRAL MARKET RANGE"
            status_text = f"Normal Fluctuation ({z_val}σ)"
            action_text = "• Strategy: Maintain baseline DCA / No extreme skew"
    else:
        signal_title = "⚪ INITIALIZING METRICS"
        status_text = "Building History..."
        action_text = "• Awaiting sufficient historical data."

    gold_per_gram = data["gold_inr_10g"] / 10
    silver_per_gram = data["silver_inr_1kg"] / 1000

    header_block = f"🚨 *{alert_header}* 🚨\n───────────────────────────────\n" if alert_header else "🚨 *GSR RELATIVE VALUE ALERT* 🚨\n───────────────────────────────\n"

    msg = (
        f"{header_block}"
        f"Signal: *{signal_title}*\n"
        f"Status: {status_text}\n\n"
        f"📊 *RATIO OVERVIEW*\n"
        f"• GSR Ratio: `{data['gsr']}`\n"
        f"• 30D Momentum: `{metrics['pct_change_30d']}%`\n"
        f"• 60-Day Z-Score: `{metrics['z_score']}`\n\n"
        f"📈 *HISTORICAL POSITIONING*\n"
        f"• 1-Yr Percentile: `{metrics['percentile']}%`\n\n"
        f"💰 *LIVE SPOT PRICES (INR)*\n"
        f"• Gold (24K, 10g): ₹{data['gold_inr_10g']:,} (`₹{gold_per_gram:.2f}/g`)\n"
        f"• Silver (1kg): ₹{data['silver_inr_1kg']:,} (`₹{silver_per_gram:.2f}/g`)\n"
        f"• USD/INR: ₹{data['usd_inr']}\n\n"
        f"💡 *TRADING IMPLICATIONS*\n"
        f"{action_text}\n"
        f"───────────────────────────────\n"
        f"📅 {data['date']}"
    )
    return msg

def run_bot(mode):
    data = fetch_market_data()
    history = update_history()
    
    # Store today's reading
    history[data["date"]] = data["gsr"]
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)
        
    metrics = compute_metrics(history, data["gsr"])
    
    if mode == "test":
        print("Test run completed. Sending message to Telegram...")
        success = send_telegram_message("🔔 *GSR Bot Test Message*\nConnection successful!")
        print("Telegram delivery successful!" if success else "Failed to send Telegram message.")
        test_msg = build_message(data, metrics, alert_header="TEST RUN SUMMARY")
        print("\n" + test_msg)
    
    elif mode == "daily":
        msg = build_message(data, metrics)
        send_telegram_message(msg)
        
    elif mode == "check":
        prev_gsr = list(history.values())[-2] if len(history) > 1 else data["gsr"]
        day_move = abs(((data["gsr"] - prev_gsr) / prev_gsr) * 100)
        
        alerts = []
        if data["gsr"] >= GSR_HIGH:
            alerts.append(f"HIGH GSR: {data['gsr']} >= {GSR_HIGH}")
        elif data["gsr"] <= GSR_LOW:
            alerts.append(f"LOW GSR: {data['gsr']} <= {GSR_LOW}")
            
        if day_move >= GSR_MOVE_PCT:
            alerts.append(f"VOLATILITY: Moved {day_move:.2f}% in 24h")
            
        if isinstance(metrics["z_score"], float) and abs(metrics["z_score"]) >= GSR_Z:
            alerts.append(f"Z-SCORE TRIGGER: {metrics['z_score']}σ")
            
        if alerts:
            alert_header = " | ".join(alerts)
            alert_msg = build_message(data, metrics, alert_header=alert_header)
            send_telegram_message(alert_msg)
        else:
            print("[INFO] Check completed. No threshold triggers met.")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python gsr_bot.py [test|daily|check]")
        sys.exit(1)
    
    mode_arg = sys.argv[1].lower()
    run_bot(mode_arg)