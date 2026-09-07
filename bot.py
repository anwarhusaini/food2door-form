#!/usr/bin/env python3
"""
Food2Door Telegram Order Bot
Status flow: New → Prep → Packed → Out → Done
- Manager (@f2d_order): New→Prep, Packed→Out, Out→Done
- Kitchen (@f2d_kitchen): Prep→Packed
"""

import os
import json
import time
import hashlib
import sqlite3
import asyncio
import aiohttp
from datetime import datetime, timedelta, timezone
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ─── Configuration ───────────────────────────────────────────────
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8935028631:***")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://gzzlokhsibryyflddikh.supabase.co")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "sb_publishable_5y_iBXJ4OTMSFP9l0QcWsg_hczQnD2I")
ORDER_CHAT = os.environ.get("ORDER_CHAT", "@f2d_order")
KITCHEN_CHAT = os.environ.get("KITCHEN_CHAT", "@f2d_kitchen")
DB_PATH = os.environ.get("DB_PATH", "/tmp/food2door_bot.db")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "10"))
STUCK_MINUTES = int(os.environ.get("STUCK_MINUTES", "10"))

print(f"""
╔══════════════════════════════════════════════════════════════╗
║          Food2Door Telegram Order Bot                        ║
╠══════════════════════════════════════════════════════════════╣
║  Supabase:  {SUPABASE_URL}
║  Order Chat: {ORDER_CHAT}
║  Kitchen Chat: {KITCHEN_CHAT}
║  DB: {DB_PATH}
║  Poll: {POLL_INTERVAL}s | Stuck: {STUCK_MINUTES}min
╚══════════════════════════════════════════════════════════════╝
""")

# ─── Status definitions ──────────────────────────────────────────
STATUS_FLOW = {
    "New":     {"next": "Prep",     "by": "manager", "btn": "Start Prep 🍳"},
    "Prep":    {"next": "Packed",   "by": "kitchen", "btn": "✅ Mark Packed"},
    "Packed":  {"next": "Out",      "by": "manager", "btn": "Mark Out 🚗"},
    "Out":     {"next": "Done",     "by": "manager", "btn": "✅ Mark Done"},
    "Done":    {"next": None,       "by": None,      "btn": "✅ Completed"},
    "Cancelled": {"next": None,     "by": None,      "btn": "❌ Cancelled"},
}

# ─── Supabase API helper ─────────────────────────────────────────

async def supabase_request(endpoint, method="GET", body=None):
    url = f"{SUPABASE_URL}/rest/v1/{endpoint}"
    headers = {
        "apikey": SUPABASE_ANON_KEY,
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    async with aiohttp.ClientSession() as session:
        if method == "GET":
            async with session.get(url, headers=headers) as r:
                return await r.json()
        elif method == "POST":
            async with session.post(url, headers=headers, json=body) as r:
                return await r.json()
        elif method == "PATCH":
            async with session.patch(url, headers=headers, json=body) as r:
                return await r.json()
        elif method == "DELETE":
            async with session.delete(url, headers=headers) as r:
                return await r.json()

async def get_orders(status_filter=None):
    """Fetch orders from Supabase"""
    query_parts = []
    if status_filter:
        if isinstance(status_filter, list):
            query_parts.append(f"status.in.({','.join(status_filter)})")
        else:
            query_parts.append(f"status.eq.{status_filter}")
    else:
        query_parts.append("status.not.eq.Done")
        query_parts.append("status.not.eq.Cancelled")
    
    qs = "&".join(query_parts) if query_parts else ""
    data = await supabase_request(f"orders?select=*&{qs}&order=created_at.asc")
    if isinstance(data, list):
        return data
    return []

async def get_order_by_id(order_id):
    data = await supabase_request(f"orders?select=*&id=eq.{order_id}")
    if isinstance(data, list) and data:
        return data[0]
    return None

async def get_order_by_number(order_number):
    data = await supabase_request(f"orders?select=*&order_number=eq.{order_number}")
    if isinstance(data, list) and data:
        return data[0]
    return None

async def update_order_status(order_id, new_status, timestamp_col=None):
    """Update order status. timestamp_col: status_prep_at, status_packed_at, etc."""
    status_map = {
        "Prep": "Prep",
        "Packed": "Packed",
        "Out": "Out",
        "Done": "Done",
    }
    supabase_status = status_map.get(new_status, new_status)
    
    payload = {"status": supabase_status}
    if timestamp_col:
        payload[timestamp_col] = datetime.now(timezone.utc).isoformat()
    
    result = await supabase_request(f"orders?id=eq.{order_id}", "PATCH", payload)
    return result

def mark_posted(order_id, msg_id):
    """Track Telegram message ID for an order"""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT OR REPLACE INTO telegram_posts 
           (order_id, telegram_msg_id, posted_at) 
           VALUES (?, ?, ?)""",
        (order_id, msg_id, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    conn.close()

def was_posted(order_id):
    conn = sqlite3.connect(DB_PATH)
    r = conn.execute("SELECT 1 FROM telegram_posts WHERE order_id = ?", (order_id,)).fetchone()
    conn.close()
    return r is not None

# ─── Keyboard builders ───────────────────────────────────────────

def status_keyboard(status, role):
    """Build keyboard for a single order's status message"""
    buttons = []
    
    if role == "kitchen" and status == "Prep":
        buttons.append([InlineKeyboardButton("✅ Mark Packed 📦", callback_data="packed")])
    elif role == "manager":
        if status == "New":
            buttons.append([InlineKeyboardButton("Start Prep 🍳", callback_data="prep")])
        elif status == "Packed":
            buttons.append([InlineKeyboardButton("Mark Out for Delivery 🚗", callback_data="out")])
        elif status == "Out":
            buttons.append([InlineKeyboardButton("✅ Mark Delivered ✅", callback_data="done")])
    
    return InlineKeyboardMarkup(buttons) if buttons else None

def orders_list_keyboard(order_num, status, role):
    """Build keyboard for an order in the list view"""
    buttons = []
    
    if role == "kitchen" and status == "Prep":
        buttons.append([InlineKeyboardButton("✅ Pack This Order 📦", callback_data=f"order_{order_num}_packed")])
    elif role == "manager":
        if status == "New":
            buttons.append([InlineKeyboardButton("Start Prep →", callback_data=f"order_{order_num}_prep")])
        elif status == "Packed":
            buttons.append([InlineKeyboardButton("Mark Out →", callback_data=f"order_{order_num}_out")])
        elif status == "Out":
            buttons.append([InlineKeyboardButton("Mark Done →", callback_data=f"order_{order_num}_done")])
    
    return InlineKeyboardMarkup(buttons) if buttons else None

# ─── Command handlers ────────────────────────────────────────────

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    title = chat.title or "Direct Message"
    
    is_kitchen = KITCHEN_CHAT in (chat.username or f"@{chat.id}")
    is_manager = ORDER_CHAT in (chat.username or f"@{chat.id}")
    
    role = "kitchen" if is_kitchen else ("manager" if is_manager else "unknown")
    
    text = (
        f"🍽️ *Food2Door Order Bot*\n\n"
        f"Chat: {title}\n"
        f"Your role: {role}\n\n"
        f"_Use /orders to see active orders._"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def orders_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    is_kitchen = KITCHEN_CHAT in (chat.username or f"@{chat.id}")
    is_manager = ORDER_CHAT in (chat.username or f"@{chat.id}")
    role = "kitchen" if is_kitchen else "manager"
    
    orders = await get_orders()
    
    if not orders:
        await update.message.reply_text("📭 No active orders.")
        return
    
    # Group by status for summary
    by_status = {}
    for o in orders:
        s = o.get("status", "Unknown")
        by_status.setdefault(s, []).append(o)
    
    text = f"📋 *Active Orders* ({len(orders)}):\n\n"
    
    for status, order_list in by_status.items():
        emoji = {"New": "🆕", "Prep": "🍳", "Packed": "📦", "Out": "🚗", "Done": "✅"}.get(status, "❓")
        text += f"*{status}* {emoji} ({len(order_list)}):\n"
        for o in order_list[:5]:
            num = o.get("order_number", "?")
            customer = o.get("customer_name", "?")
            total = o.get("total") or 0
            text += f"  • *{num}* — {customer} (RM {total:.2f})\n"
        if len(order_list) > 5:
            text += f"  ... and {len(order_list) - 5} more\n"
        text += "\n"
    
    # Send one message per order with action buttons
    for o in orders[:10]:
        num = o.get("order_number", "?")
        customer = o.get("customer_name", "?")
        phone = o.get("phone") or "N/A"
        address = o.get("delivery_address") or "N/A"
        total = o.get("total") or 0
        status = o.get("status", "Unknown")
        notes = o.get("notes") or ""
        items = o.get("items") or []
        
        emoji = {"New": "🆕", "Prep": "🍳", "Packed": "📦", "Out": "🚗", "Done": "✅"}.get(status, "❓")
        
        # Format items list
        items_text = ""
        if items:
            items_text = "\n".join([f"  • {it.get('name', '?')} x{it.get('qty', 1)} — RM {it.get('price', 0):.2f}" for it in items[:5]])
            if len(items) > 5:
                items_text += f"\n  ... and {len(items) - 5} more items"
        
        msg_text = (
            f"*{num}* {emoji}\n"
            f"Customer: {customer}\n"
            f"Phone: {phone}\n"
            f"Address: {address}\n"
            f"Total: RM {total:.2f}\n"
            f"Status: {status}"
        )
        if items_text:
            msg_text += f"\n\nItems:\n{items_text}"
        if notes:
            msg_text += f"\n\nNotes: {notes}"
        
        keyboard = orders_list_keyboard(num, status, role)
        await update.message.reply_text(msg_text, reply_markup=keyboard, parse_mode="Markdown")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /status <order_number>")
        return
    
    num = context.args[0]
    order = await get_order_by_number(num)
    
    if not order:
        await update.message.reply_text(f"❌ Order *{num}* not found.", parse_mode="Markdown")
        return
    
    chat = update.effective_chat
    is_kitchen = KITCHEN_CHAT in (chat.username or f"@{chat.id}")
    is_manager = ORDER_CHAT in (chat.username or f"@{chat.id}")
    role = "kitchen" if is_kitchen else "manager"
    
    status = order.get("status", "Unknown")
    keyboard = status_keyboard(status, role)
    
    text = (
        f"*{num}*\n"
        f"Customer: {order.get('customer_name', '?')}\n"
        f"Phone: {order.get('phone') or 'N/A'}\n"
        f"Address: {order.get('delivery_address') or 'N/A'}\n"
        f"Total: RM {order.get('total') or 0:.2f}\n"
        f"Status: {status}"
    )
    
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")

# ─── Callback handler ────────────────────────────────────────────

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all inline button presses"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    user = query.from_user
    chat = query.message.chat if query.message else None
    chat_username = chat.username if chat else ""
    
    # Determine role
    is_kitchen = KITCHEN_CHAT in chat_username or KITCHEN_CHAT.replace("@", "") in str(chat.id)
    is_manager = ORDER_CHAT in chat_username or ORDER_CHAT.replace("@", "") in str(chat.id)
    role = "kitchen" if is_kitchen else ("manager" if is_manager else "unknown")
    
    # ─── Handle "info" button ───
    if data == "info":
        await query.edit_message_reply_markup(reply_markup=None)
        return
    
    # ─── Handle kitchen "packed" action (from status message) ───
    if data == "packed" and role == "kitchen":
        text = query.message.text if query.message else ""
        lines = text.split("\n") if text else []
        order_num = lines[0].replace("*", "").strip() if lines else None
        
        if not order_num:
            await query.answer("Could not identify order.", show_alert=True)
            return
        
        order = await get_order_by_number(order_num)
        if not order:
            await query.answer("Order not found.", show_alert=True)
            return
        
        current = order.get("status", "")
        if current != "Prep":
            await query.answer(f"Can only pack orders in Prep (currently: {current}).", show_alert=True)
            return
        
        # Update status
        await update_order_status(order.get("id"), "Packed", "status_packed_at")
        
        # Update message
        new_keyboard = status_keyboard("Packed", role)
        new_text = text.replace("Status: Prep", "Status: Packed").replace("🍳", "📦")
        
        await query.edit_message_text(text=new_text, reply_markup=new_keyboard, parse_mode="Markdown")
        
        # Notify order group
        try:
            await context.bot.send_message(
                chat_id=ORDER_CHAT,
                text=(
                    f"📦 *Order {order_num} is packed and ready!*\n\n"
                    f"Kitchen has marked it as Packed.\n"
                    f"Atan/Bella, please mark as Out for delivery when dispatched."
                ),
                parse_mode="Markdown"
            )
        except Exception as e:
            print(f"Error notifying order group: {e}")
        
        print(f"Kitchen packed order {order_num} (by {user.first_name})")
        return
    
    # ─── Handle kitchen "packed" from order list ───
    if data.startswith("order_") and data.endswith("_packed") and role == "kitchen":
        parts = data.split("_")
        order_num = parts[1] if len(parts) >= 2 else None
        
        if not order_num:
            await query.answer("Invalid data.", show_alert=True)
            return
        
        order = await get_order_by_number(order_num)
        if not order or order.get("status") != "Prep":
            await query.answer("Order not in Prep status.", show_alert=True)
            return
        
        await update_order_status(order.get("id"), "Packed", "status_packed_at")
        
        new_keyboard = orders_list_keyboard(order_num, "Packed", role)
        await query.edit_message_reply_markup(reply_markup=new_keyboard)
        
        try:
            await context.bot.send_message(
                chat_id=ORDER_CHAT,
                text=(
                    f"📦 *Order {order_num} is packed!*\n"
                    f"Kitchen ready it for delivery."
                ),
                parse_mode="Markdown"
            )
        except Exception as e:
            print(f"Error notifying: {e}")
        
        print(f"Kitchen packed order {order_num} from list (by {user.first_name})")
        return
    
    # ─── Handle manager actions from status message ───
    if data in ("prep", "out", "done") and role == "manager":
        text = query.message.text if query.message else ""
        lines = text.split("\n") if text else []
        order_num = lines[0].replace("*", "").strip() if lines else None
        
        if not order_num:
            await query.answer("Could not identify order.", show_alert=True)
            return
        
        order = await get_order_by_number(order_num)
        if not order:
            await query.answer("Order not found.", show_alert=True)
            return
        
        current = order.get("status", "")
        
        valid = {
            "prep":  ("New",        "Prep",  "status_prep_at"),
            "out":   ("Packed",     "Out",   "status_out_at"),
            "done":  ("Out",        "Done",  "status_done_at"),
        }
        
        if data not in valid:
            await query.answer("Invalid action.", show_alert=True)
            return
        
        required_status, new_status, ts_col = valid[data]
        
        if current != required_status:
            await query.answer(
                f"Can only {data} from {required_status} (currently: {current}).",
                show_alert=True
            )
            return
        
        await update_order_status(order.get("id"), new_status, ts_col)
        
        emoji_map = {"New": "🆕", "Prep": "🍳", "Packed": "📦", "Out": "🚗", "Done": "✅"}
        new_text = text.replace(
            f"Status: {current}",
            f"Status: {new_status}"
        ).replace(
            emoji_map.get(current, "❓"),
            emoji_map.get(new_status, "✅")
        )
        new_keyboard = status_keyboard(new_status, role)
        
        await query.edit_message_text(text=new_text, reply_markup=new_keyboard, parse_mode="Markdown")
        
        if data == "prep":
            try:
                await context.bot.send_message(
                    chat_id=KITCHEN_CHAT,
                    text=(
                        f"🍳 *New Order to Prep*\n\n"
                        f"*{order_num}* — {order.get('customer_name', 'Unknown')}\n"
                        f"RM {order.get('total') or 0:.2f}\n"
                        f"Atan/Bella has started prep."
                    ),
                    parse_mode="Markdown"
                )
            except Exception as e:
                print(f"Error notifying kitchen: {e}")
        
        elif data == "done":
            try:
                await context.bot.send_message(
                    chat_id=ORDER_CHAT,
                    text=(
                        f"✅ *Order {order_num} completed!*\n\n"
                        f"Delivered. Thank you!"
                    ),
                    parse_mode="Markdown"
                )
            except Exception as e:
                print(f"Error notifying: {e}")
        
        print(f"Manager: order {order_num} {current}→{new_status} (by {user.first_name})")
        return
    
    # ─── Handle manager actions from order list ───
    if data.startswith("order_") and role == "manager":
        parts = data.split("_")
        if len(parts) >= 3:
            order_num = parts[1]
            action = parts[2]
            
            order = await get_order_by_number(order_num)
            if not order:
                await query.answer("Order not found.", show_alert=True)
                return
            
            current = order.get("status", "")
            
            valid = {
                "prep":  ("New",        "Prep",  "status_prep_at"),
                "out":   ("Packed",     "Out",   "status_out_at"),
                "done":  ("Out",        "Done",  "status_done_at"),
            }
            
            if action not in valid:
                await query.answer("Invalid action.", show_alert=True)
                return
            
            required_status, new_status, ts_col = valid[action]
            
            if current != required_status:
                await query.answer(
                    f"Can only {action} from {required_status} (currently: {current}).",
                    show_alert=True
                )
                return
            
            await update_order_status(order.get("id"), new_status, ts_col)
            
            new_keyboard = orders_list_keyboard(order_num, new_status, role)
            await query.edit_message_reply_markup(reply_markup=new_keyboard)
            
            if action == "prep":
                try:
                    await context.bot.send_message(
                        chat_id=KITCHEN_CHAT,
                        text=(
                            f"🍳 *New Order to Prep*\n\n"
                            f"*{order_num}* — {order.get('customer_name', 'Unknown')}\n"
                            f"RM {order.get('total') or 0:.2f}"
                        ),
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    print(f"Error: {e}")
            
            elif action == "done":
                try:
                    await context.bot.send_message(
                        chat_id=ORDER_CHAT,
                        text=f"✅ *Order {order_num} completed!*",
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    print(f"Error: {e}")
            
            print(f"Manager (list): order {order_num} {current}→{new_status} (by {user.first_name})")
            return
    
    await query.answer("Unknown action.", show_alert=True)


# ─── Background tasks ────────────────────────────────────────────

async def post_new_orders(context: ContextTypes.DEFAULT_TYPE):
    """Post newly detected orders to Telegram groups"""
    bot = context.bot
    orders = await get_orders()
    
    for order in orders:
        oid = order.get("id")
        if was_posted(oid):
            continue
        
        num = order.get("order_number", "?")
        customer = order.get("customer_name", "Unknown")
        phone = order.get("phone") or "N/A"
        address = order.get("delivery_address") or "N/A"
        total = order.get("total") or 0
        status = order.get("status", "New")
        
        emoji = {"New": "🆕", "Prep": "🍳", "Packed": "📦", "Out": "🚗", "Done": "✅"}.get(status, "❓")
        
        text = (
            f"*{num}* {emoji}\n"
            f"Customer: {customer}\n"
            f"Phone: {phone}\n"
            f"Address: {address}\n"
            f"Total: RM {total:.2f}\n"
            f"Status: {status}"
        )
        
        try:
            msg = await bot.send_message(
                chat_id=ORDER_CHAT,
                text=text,
                parse_mode="Markdown"
            )
            mark_posted(oid, msg.message_id)
            print(f"Posted {num} to order group (msg {msg.message_id})")
        except Exception as e:
            print(f"Error posting to order group: {e}")
        
        if status in ("New", "Prep"):
            try:
                kitchen_text = (
                    f"🍳 *New Order*\n\n"
                    f"*{num}* — {customer}\n"
                    f"RM {total:.2f}\n"
                    f"Phone: {phone}"
                )
                await bot.send_message(chat_id=KITCHEN_CHAT, text=kitchen_text, parse_mode="Markdown")
                print(f"Posted {num} to kitchen group")
            except Exception as e:
                print(f"Error posting to kitchen: {e}")

async def check_stuck_orders(context: ContextTypes.DEFAULT_TYPE):
    """Remind about orders stuck > threshold"""
    bot = context.bot
    now = datetime.now(timezone.utc)
    threshold = timedelta(minutes=STUCK_MINUTES)
    
    orders = await get_orders(["New", "Prep", "Packed", "Out"])
    stuck = []
    
    for order in orders:
        oid = order.get("id")
        num = order.get("order_number", "?")
        status = order.get("status", "")
        
        stuck_flag = False
        reason = ""
        
        if status == "New":
            ts_prep = order.get("status_prep_at")
            if not ts_prep:
                created = order.get("created_at")
                if created:
                    try:
                        dt = datetime.fromisoformat(created.replace("Z", "+00:00") if "Z" not in created else created)
                        if now - dt > threshold:
                            stuck_flag = True
                            reason = f"🆕 New for >{STUCK_MINUTES}min"
                    except:
                        pass
        
        elif status == "Prep":
            ts_prep = order.get("status_prep_at")
            if ts_prep:
                try:
                    ts_str = ts_prep if "Z" in str(ts_prep) else str(ts_prep) + "Z"
                    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if now - dt > threshold:
                        stuck_flag = True
                        reason = f"🍳 Prep for >{STUCK_MINUTES}min"
                except:
                    pass
        
        elif status == "Packed":
            ts_out = order.get("status_out_at")
            if not ts_out:
                ts_packed = order.get("status_packed_at")
                if ts_packed:
                    try:
                        ts_str = ts_packed if "Z" in str(ts_packed) else str(ts_packed) + "Z"
                        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        if now - dt > threshold:
                            stuck_flag = True
                            reason = f"📦 Packed for >{STUCK_MINUTES}min"
                    except:
                        pass
        
        elif status == "Out":
            ts_out = order.get("status_out_at")
            if ts_out:
                try:
                    ts_str = ts_out if "Z" in str(ts_out) else str(ts_out) + "Z"
                    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if now - dt > threshold:
                        stuck_flag = True
                        reason = f"🚗 Out for >{STUCK_MINUTES}min"
                except:
                    pass
        
        if stuck_flag:
            stuck.append((num, status, reason))
    
    if stuck:
        text = f"⏰ *Stuck Order Reminder*\n\n"
        for num, status, reason in stuck:
            text += f"• *{num}* — {status}: {reason}\n"
        text += "\nPlease update the status."
        
        try:
            await bot.send_message(chat_id=ORDER_CHAT, text=text, parse_mode="Markdown")
            print(f"Sent stuck reminder for {len(stuck)} order(s)")
        except Exception as e:
            print(f"Error sending reminder: {e}")
    else:
        print("No stuck orders.")


# ─── Database ────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS telegram_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT NOT NULL UNIQUE,
            telegram_msg_id INTEGER,
            posted_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.commit()
    conn.close()
    print(f"DB ready: {DB_PATH}")


# ─── Main ─────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN required")
        return 1
    
    init_db()
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("orders", orders_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))
    
    app.job_queue.run_repeating(post_new_orders, interval=POLL_INTERVAL, first=POLL_INTERVAL, name="post_orders")
    app.job_queue.run_repeating(check_stuck_orders, interval=60, first=60, name="stuck_check")
    
    print(f"Bot starting. Poll={POLL_INTERVAL}s, stuck_check=60s")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
    return 0

if __name__ == "__main__":
    exit(main())
