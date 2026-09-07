#!/usr/bin/env python3
"""
Food2Door Stuck Order Reminder Cron Job
Runs every 2 minutes on Render
Checks for orders stuck > 10 minutes in Prep or New status
"""

import os
import sys
import asyncio
import aiohttp
from datetime import datetime, timedelta, timezone

# ─── Config ───────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8935028631:***")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://gzzlokhsibryyflddikh.supabase.co")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "sb_publishable_5y_iBXJ4OTMSFP9l0QcWsg_hczQnD2I")
ORDER_CHAT = os.environ.get("ORDER_CHAT", "@f2d_order")
STUCK_MINUTES = int(os.environ.get("STUCK_MINUTES", "10"))

if not BOT_TOKEN:
    print("ERROR: TELEGRAM_BOT_TOKEN required")
    sys.exit(1)

print(f"""
╔══════════════════════════════════════════════════════════════╗
║          Food2Door Stuck Order Cron                           ║
╠══════════════════════════════════════════════════════════════╣
║  Threshold: {STUCK_MINUTES} minutes
║  Chat: {ORDER_CHAT}
╚══════════════════════════════════════════════════════════════╝
""")

# ─── Supabase API ────────────────────────────────────────────────

async def supabase_get(endpoint):
    url = f"{SUPABASE_URL}/rest/v1/{endpoint}"
    headers = {
        "apikey": SUPABASE_ANON_KEY,
        "Content-Type": "application/json",
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            return await resp.json()

async def get_stuck_orders():
    """Fetch orders stuck in New or Prep for too long"""
    now = datetime.now(timezone.utc)
    threshold = timedelta(minutes=STUCK_MINUTES)
    
    # Get all active orders (not Done/Cancelled)
    orders = await supabase_get(
        "orders?select=order_number,status,created_at,status_prep_at,status_packed_at,status_out_at,status_done_at&status.not.eq.Done&status.not.eq.Cancelled&order=created_at.asc"
    )
    
    if not isinstance(orders, list):
        return []
    
    stuck = []
    
    for order in orders:
        order_num = order.get("order_number", "?")
        status = order.get("status", "")
        
        if status == "Done" or status == "Cancelled":
            continue
        
        stuck_flag = False
        reason = ""
        
        if status == "New":
            # Check if Prep hasn't started
            status_prep_at = order.get("status_prep_at")
            if status_prep_at is None:
                created_at = order.get("created_at")
                if created_at:
                    try:
                        # Handle ISO format with timezone
                        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                        if now - dt > threshold:
                            stuck_flag = True
                            reason = f"🆕 New for >{STUCK_MINUTES}min (no Prep started)"
                    except:
                        pass
        
        elif status == "Prep":
            status_prep_at = order.get("status_prep_at")
            if status_prep_at:
                try:
                    dt = datetime.fromisoformat(status_prep_at.replace("Z", "+00:00"))
                    if now - dt > threshold:
                        stuck_flag = True
                        reason = f"🍳 Prep for >{STUCK_MINUTES}min (not packed)"
                except:
                    pass
        
        elif status == "Packed":
            status_out_at = order.get("status_out_at")
            if status_out_at is None:
                status_packed_at = order.get("status_packed_at")
                if status_packed_at:
                    try:
                        dt = datetime.fromisoformat(status_packed_at.replace("Z", "+00:00"))
                        if now - dt > threshold:
                            stuck_flag = True
                            reason = f"📦 Packed for >{STUCK_MINUTES}min (not marked Out)"
                    except:
                        pass
        
        elif status == "Out":
            status_out_at = order.get("status_out_at")
            if status_out_at:
                try:
                    dt = datetime.fromisoformat(status_out_at.replace("Z", "+00:00"))
                    if now - dt > threshold:
                        stuck_flag = True
                        reason = f"🚗 Out for >{STUCK_MINUTES}min (not marked Done)"
                except:
                    pass
        
        if stuck_flag:
            stuck.append((order_num, status, reason))
    
    return stuck

# ─── Telegram API ────────────────────────────────────────────────

async def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": ORDER_CHAT,
        "text": text,
        "parse_mode": "Markdown"
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            return await resp.json()

# ─── Main ─────────────────────────────────────────────────────────

async def main():
    print(f"[{datetime.now().isoformat()}] Running stuck order check...")
    
    stuck = await get_stuck_orders()
    
    if not stuck:
        print("No stuck orders found.")
        return 0
    
    # Build message
    text = f"⏰ *Stuck Order Reminder*\n\n"
    for order_num, status, reason in stuck:
        text += f"• *{order_num}* — {status}: {reason}\n"
    text += f"\nPlease update the status in @f2d_order."
    
    print(f"Found {len(stuck)} stuck order(s):")
    for num, status, reason in stuck:
        print(f"  - {num}: {reason}")
    
    # Send to Telegram
    try:
        result = await send_telegram_message(text)
        if result.get("ok"):
            print(f"✅ Sent reminder to {ORDER_CHAT}")
            print(f"   Message ID: {result.get('result', {}).get('message_id')}")
        else:
            print(f"❌ Failed: {result.get('description', 'Unknown error')}")
            return 1
    except Exception as e:
        print(f"❌ Error sending: {e}")
        return 1
    
    return 0

if __name__ == "__main__":
    import concurrent.futures
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        exit_code = loop.run_until_complete(main())
    finally:
        loop.close()
    sys.exit(exit_code)
