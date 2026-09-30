"""
bot.py — the Telegram side of the finance bot.
db.py handles data (MongoDB Atlas), chart_style.py handles chart looks, parser.py reads messages.
This file wires them all together into commands and button taps.

COMMANDS:
  /start /help /menu
  /today /month /networth
  /setbudget /budgets /suggest
  /goals /newgoal /deletegoal /category
  /find /export
  /chart /trend
  /setreminder /reminder off /addrecurring /settings
  /delete (transactions by range) /resetall (wipes everything, restarts onboarding)
"""

import os
import io
import csv
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta, time as dtime
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes,
)
import matplotlib.pyplot as plt

import db
import chart_style
from parser import parse_message

# ============================================================
# TINY HEALTH-CHECK SERVER
# Render's free tier only keeps "Web Services" alive, and a web service
# must answer HTTP requests on a port. This doesn't serve anything real —
# it just says "200 OK" so Render (and UptimeRobot, keeping it awake)
# both see the service as healthy. The actual bot runs in the main thread.
# ============================================================

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass  # keeps request logging out of the deploy logs


def run_health_server():
    port = int(os.getenv("PORT", 8080))
    HTTPServer(("0.0.0.0", port), HealthCheckHandler).serve_forever()


threading.Thread(target=run_health_server, daemon=True).start()

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

CATEGORIES = ["Food", "Travel", "Savings", "Parents", "Bills", "Shopping", "Entertainment", "Health", "Misc"]
PAGE_SIZE = 10

# ---- in-memory state (resets if the bot restarts — that's fine, these are short-lived) ----
pending = {}               # user_id -> transaction dict, waiting for a category button tap
pending_savings = {}       # user_id -> transaction dict, waiting for a goal button tap
conv_state = {}            # user_id -> {"flow": ..., "step": ..., ...} for multi-step text flows
sent_reminders_today = set()  # (user_id, date) pairs already nagged today — avoids double-sends


class FauxUpdate:
    """
    Lets us reuse a command function (built for /today etc.) when the same
    action is triggered from a /menu button instead of a typed command.
    Only needs .message and .effective_user, since that's all our commands use.
    """
    def __init__(self, message, user):
        self.message = message
        self.effective_user = user


# ============================================================
# ONBOARDING (runs once per new user, on their first /start —
# and again after /resetall, since that deletes their settings row)
# ============================================================

async def begin_onboarding(update, user_id):
    db.ensure_settings_row(user_id)
    conv_state[user_id] = {"flow": "onboarding", "step": "salary"}
    buttons = InlineKeyboardMarkup([[InlineKeyboardButton("Skip", callback_data="onb:skip_salary")]])
    await update.message.reply_text(
        "Welcome! A couple of quick questions to set things up (all optional).\n\n"
        "1) What day does your salary usually come in? (1-31)",
        reply_markup=buttons,
    )


async def ask_recurring_yn(target, user_id, state):
    """target can be a real Update (message) or a CallbackQuery."""
    state["step"] = "recurring_yn"
    buttons = InlineKeyboardMarkup([[
        InlineKeyboardButton("Yes", callback_data="onb:recurring_yes"),
        InlineKeyboardButton("No", callback_data="onb:recurring_no"),
    ]])
    text = "2) Any recurring payments or dates to track (rent, subscriptions, gym, etc.)?"
    if isinstance(target, Update):
        await target.message.reply_text(text, reply_markup=buttons)
    else:
        await target.edit_message_text(text, reply_markup=buttons)


async def finish_onboarding(target, user_id):
    db.update_settings(user_id, onboarded=1)
    conv_state.pop(user_id, None)
    text = (
        "All set! Here's how to use the bot:\n\n"
        "- Send an amount to log it: 250 food\n"
        "- Or just 250 and pick a category from the buttons\n"
        "- +45000 salary for income\n"
        "- /menu for everything else"
    )
    if isinstance(target, Update):
        await target.message.reply_text(text)
    else:
        await target.edit_message_text(text)


async def handle_onboarding_text(update: Update, user_id, state, text):
    if state["step"] == "salary":
        if text.isdigit() and 1 <= int(text) <= 31:
            db.update_settings(user_id, salary_day=int(text))
            await ask_recurring_yn(update, user_id, state)
        else:
            await update.message.reply_text("Please send a number 1-31, or tap Skip.")

    elif state["step"] == "recurring_entry":
        parts = text.rsplit(" ", 1)
        if len(parts) != 2 or not parts[1].isdigit() or not (1 <= int(parts[1]) <= 31):
            await update.message.reply_text("Send it as: Label Day  (e.g. Rent 5)")
            return
        db.add_recurring(user_id, parts[0], int(parts[1]))
        buttons = InlineKeyboardMarkup([[
            InlineKeyboardButton("Add another", callback_data="onb:recurring_more"),
            InlineKeyboardButton("Done", callback_data="onb:finish"),
        ]])
        await update.message.reply_text(f"Added: {parts[0]} on day {parts[1]}.", reply_markup=buttons)


# ============================================================
# LOGGING A TRANSACTION (typed amount -> saved entry)
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    settings = db.get_settings(user_id)
    if not settings or not settings["onboarded"]:
        await begin_onboarding(update, user_id)
    else:
        await update.message.reply_text("Welcome back! Send an amount to log it, or /menu to see everything.")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Logging:\n"
        "  250 food\n"
        "  +45000 salary\n"
        "  500 savings  (opens goal picker)\n\n"
        "Everything else: /menu\n\n"
        "All commands: /today /month /networth /budgets /suggest "
        "/goals /deletegoal /category /find /export /chart /trend /settings "
        "/delete /resetall"
    )


async def log_transaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # If we're mid-way through onboarding, adding a goal, or adding a recurring
    # date, this text is an answer to that flow — not a new transaction.
    if user_id in conv_state:
        await handle_conversation_text(update, context, conv_state[user_id])
        return

    data = parse_message(update.message.text)
    if data is None:
        await update.message.reply_text("I didn't get that. Try: 250 lunch food")
        return

    if data["category"] is None:
        pending[user_id] = data
        buttons = [InlineKeyboardButton(cat, callback_data=f"cat:{cat}") for cat in CATEGORIES]
        rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
        await update.message.reply_text(
            f"₹{data['amount']:,.0f} — pick a category:",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return

    await route_transaction(user_id, data, update)


async def handle_conversation_text(update: Update, context, state):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    flow = state["flow"]

    if flow == "onboarding":
        await handle_onboarding_text(update, user_id, state, text)

    elif flow == "new_goal":
        if state["step"] == "name":
            state["goal_name"] = text
            state["step"] = "target"
            await update.message.reply_text("Target amount? (type a number, or 'skip')")
        elif state["step"] == "target":
            target = None
            if text.lower() != "skip":
                try:
                    target = float(text)
                except ValueError:
                    await update.message.reply_text("Please send a number, or 'skip'.")
                    return
            goal_id = db.create_goal(user_id, state["goal_name"], target)
            tx_data = state["data"]
            tx_data["goal_id"] = goal_id
            conv_state.pop(user_id, None)
            await finish_save(user_id, tx_data, update)

    elif flow == "recurring":
        parts = text.rsplit(" ", 1)
        if len(parts) != 2 or not parts[1].isdigit() or not (1 <= int(parts[1]) <= 31):
            await update.message.reply_text("Send it as: Label Day  (e.g. Gym 5)")
            return
        db.add_recurring(user_id, parts[0], int(parts[1]))
        conv_state.pop(user_id, None)
        await update.message.reply_text(f"Added: {parts[0]} on day {parts[1]}.")


async def route_transaction(user_id, data, reply_target):
    """
    Decides what happens next for a parsed transaction.
    Savings needs a goal picked first; everything else saves right away.
    reply_target is either the original Update (typed message) or a CallbackQuery (button tap).
    """
    if data["category"] == "Savings":
        note = data["note"].strip()
        goal = db.get_goal_by_name(user_id, note) if note else None
        if goal:
            data["goal_id"] = goal[0]
            await finish_save(user_id, data, reply_target)
            return

        pending_savings[user_id] = data
        goals = db.get_goals(user_id)
        buttons = [[InlineKeyboardButton(g[1], callback_data=f"goal:{g[0]}")] for g in goals]
        buttons.append([InlineKeyboardButton("+ New goal", callback_data="goal:new")])
        buttons.append([InlineKeyboardButton("General savings", callback_data="goal:none")])
        text = f"₹{data['amount']:,.0f} — which savings goal?"
        markup = InlineKeyboardMarkup(buttons)
        if isinstance(reply_target, Update):
            await reply_target.message.reply_text(text, reply_markup=markup)
        else:
            await reply_target.edit_message_text(text, reply_markup=markup)
    else:
        await finish_save(user_id, data, reply_target)


def build_confirmation_text(user_id, data, tx_id):
    sign = "+" if data["type"] == "income" else "-"
    text = f"Saved: {sign}₹{data['amount']:,.0f} -> {data['category']}"

    if data.get("goal_id"):
        goal = db.get_goal_by_id(data["goal_id"])
        saved = db.goal_progress(data["goal_id"])
        if goal and goal[3]:  # goal[3] = target_amount
            pct = min(saved / goal[3] * 100, 100)
            text += f"\n{goal[2]}: ₹{saved:,.0f} / ₹{goal[3]:,.0f} ({pct:.0f}%)"

    elif data["type"] == "expense":
        budgets = db.get_budgets(user_id)
        limit = budgets.get(data["category"])
        if limit:
            settings = db.get_settings(user_id)
            salary_day = settings["salary_day"] if settings else None
            start, end = db.cycle_bounds(salary_day)
            spent_map = dict(db.expenses_by_category_between(user_id, start, end))
            spent = spent_map.get(data["category"], 0)
            pct = spent / limit * 100
            if pct >= 100:
                text += f"\nOver budget: {data['category']} ₹{spent:,.0f}/₹{limit:,.0f}"
            elif pct >= 80:
                text += f"\n{pct:.0f}% of {data['category']} budget used"

    return text


async def finish_save(user_id, data, reply_target):
    tx_id = db.add_transaction(user_id, data["type"], data["amount"], data["category"],
                                data["note"], data.get("goal_id"))
    text = build_confirmation_text(user_id, data, tx_id)
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("Undo", callback_data=f"undo:{tx_id}")]])
    if isinstance(reply_target, Update):
        await reply_target.message.reply_text(text, reply_markup=markup)
    else:
        await reply_target.edit_message_text(text, reply_markup=markup)


# ============================================================
# CATEGORY DRILL-DOWN (recent entries -> This Week / This Month, paginated)
# ============================================================

def period_bounds_for_range(user_id, range_key):
    if range_key == "week":
        start = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d 00:00:00")
        return start, "2100-01-01 00:00:00"
    if range_key == "month":
        settings = db.get_settings(user_id)
        salary_day = settings["salary_day"] if settings else None
        return db.cycle_bounds(salary_day)
    return "2000-01-01 00:00:00", "2100-01-01 00:00:00"  # "recent" = all-time, we just limit the count


async def render_category_view(user_id, category, range_key, offset, edit_target):
    start, end = period_bounds_for_range(user_id, range_key)
    limit = 3 if range_key == "recent" else PAGE_SIZE
    rows = db.category_entries(user_id, category, start, end, limit=limit, offset=offset)

    if not rows:
        text = f"{category} — no entries in this range."
    else:
        lines = [f"{category}", ""]
        for amount, note, created_at in rows:
            note_part = f" ({note})" if note else ""
            lines.append(f"{created_at[:10]} — ₹{amount:,.0f}{note_part}")
        text = "\n".join(lines)

    buttons = []
    if range_key == "recent":
        buttons.append([
            InlineKeyboardButton("This Week", callback_data=f"catview:{category}:week:0"),
            InlineKeyboardButton("This Month", callback_data=f"catview:{category}:month:0"),
        ])
    else:
        nav = []
        if offset > 0:
            nav.append(InlineKeyboardButton("< Prev", callback_data=f"catview:{category}:{range_key}:{max(0, offset - PAGE_SIZE)}"))
        if len(rows) == PAGE_SIZE:
            nav.append(InlineKeyboardButton("Next >", callback_data=f"catview:{category}:{range_key}:{offset + PAGE_SIZE}"))
        if nav:
            buttons.append(nav)
        switch = []
        if range_key != "week":
            switch.append(InlineKeyboardButton("This Week", callback_data=f"catview:{category}:week:0"))
        if range_key != "month":
            switch.append(InlineKeyboardButton("This Month", callback_data=f"catview:{category}:month:0"))
        buttons.append(switch)

    markup = InlineKeyboardMarkup(buttons) if buttons else None
    if isinstance(edit_target, Update):
        await edit_target.message.reply_text(text, reply_markup=markup)
    else:
        await edit_target.edit_message_text(text, reply_markup=markup)


async def category_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /category Food")
        return
    category = " ".join(context.args).capitalize()
    await render_category_view(update.effective_user.id, category, "recent", 0, update)


# ============================================================
# DELETING DATA: /delete (transactions)  /deletegoal  /resetall (everything)
# ============================================================

async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("Today", callback_data="del:today"),
         InlineKeyboardButton("This Week", callback_data="del:week")],
        [InlineKeyboardButton("This Month", callback_data="del:month"),
         InlineKeyboardButton("Everything", callback_data="del:all")],
    ])
    await update.message.reply_text(
        "What would you like to delete? This only removes transactions "
        "(your budgets, goals, and settings stay as they are). This can't be undone.",
        reply_markup=buttons,
    )


async def deletegoal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    goals_list = db.get_goals(user_id)
    if not goals_list:
        await update.message.reply_text("You don't have any goals to delete.")
        return
    buttons = [[InlineKeyboardButton(name, callback_data=f"delgoal:{goal_id}")]
               for goal_id, name, target, target_date in goals_list]
    await update.message.reply_text("Which goal do you want to delete?", reply_markup=InlineKeyboardMarkup(buttons))


async def resetall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    buttons = InlineKeyboardMarkup([[
        InlineKeyboardButton("Yes, erase everything", callback_data="reset:confirm"),
        InlineKeyboardButton("Cancel", callback_data="reset:cancel"),
    ]])
    await update.message.reply_text(
        "This erases EVERYTHING — every transaction, budget, goal, and setting — "
        "and starts you over like a brand new user. This cannot be undone.\n\n"
        "Are you sure?",
        reply_markup=buttons,
    )


# ============================================================
# BUTTON TAPS (one router for every inline button in the bot)
# ============================================================

async def button_tap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    parts = query.data.split(":")
    action = parts[0]

    if action == "cat":
        data = pending.pop(user_id, None)
        if data is None:
            await query.edit_message_text("This expired, please send the amount again.")
            return
        data["category"] = parts[1]
        await route_transaction(user_id, data, query)

    elif action == "undo":
        db.delete_transaction(user_id, int(parts[1]))
        await query.edit_message_text("Undone.")

    elif action == "catview":
        category, range_key, offset = parts[1], parts[2], int(parts[3])
        await render_category_view(user_id, category, range_key, offset, query)

    elif action == "goal":
        choice = parts[1]
        data = pending_savings.pop(user_id, None)
        if data is None:
            await query.edit_message_text("This expired — please send the amount again.")
            return
        if choice == "new":
            conv_state[user_id] = {"flow": "new_goal", "step": "name", "data": data}
            await query.edit_message_text("What's the goal called? (e.g. Phone, Car, House)")
        elif choice == "none":
            await finish_save(user_id, data, query)
        else:
            data["goal_id"] = int(choice)
            await finish_save(user_id, data, query)

    elif action == "onb":
        state = conv_state.get(user_id)
        if state is None:
            return
        sub = parts[1]
        if sub == "skip_salary":
            await ask_recurring_yn(query, user_id, state)
        elif sub == "recurring_yes":
            state["step"] = "recurring_entry"
            await query.edit_message_text("Send it as: Label Day  (e.g. Rent 5)")
        elif sub == "recurring_no":
            await finish_onboarding(query, user_id)
        elif sub == "recurring_more":
            state["step"] = "recurring_entry"
            await query.edit_message_text("Send the next one as: Label Day")
        elif sub == "finish":
            await finish_onboarding(query, user_id)

    elif action == "menu":
        target = parts[1]
        faux = FauxUpdate(query.message, query.from_user)
        context.args = []
        handler_map = {
            "today": today_cmd, "month": month_cmd, "budgets": budgets_cmd,
            "goals": goals_cmd, "chart": chart_cmd, "trend": trend_cmd,
            "networth": networth_cmd, "settings": settings_cmd,
        }
        handler = handler_map.get(target)
        if handler:
            await handler(faux, context)

    elif action == "del":
        range_key = parts[1]
        labels = {"today": "today's entries", "week": "this week's entries",
                  "month": "this month's entries", "all": "ALL your entries, ever"}
        buttons = InlineKeyboardMarkup([[
            InlineKeyboardButton("Yes, delete", callback_data=f"delconfirm:{range_key}"),
            InlineKeyboardButton("Cancel", callback_data="delcancel"),
        ]])
        await query.edit_message_text(
            f"Delete {labels[range_key]}? This can't be undone.",
            reply_markup=buttons,
        )

    elif action == "delconfirm":
        range_key = parts[1]
        if range_key == "all":
            count = db.delete_all_transactions(user_id)
        else:
            if range_key == "today":
                start = datetime.now().strftime("%Y-%m-%d") + " 00:00:00"
                end = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d") + " 00:00:00"
            elif range_key == "week":
                start = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d") + " 00:00:00"
                end = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d") + " 00:00:00"
            else:  # month
                settings = db.get_settings(user_id)
                salary_day = settings["salary_day"] if settings else None
                start, end = db.cycle_bounds(salary_day)
            count = db.delete_transactions_between(user_id, start, end)
        await query.edit_message_text(f"Deleted {count} entr{'y' if count == 1 else 'ies'}.")

    elif action == "delcancel":
        await query.edit_message_text("Cancelled — nothing was deleted.")

    elif action == "delgoal":
        goal_id = int(parts[1])
        goal = db.get_goal_by_id(goal_id)
        if goal is None or goal[1] != user_id:
            await query.edit_message_text("Couldn't find that goal.")
            return
        saved = db.goal_progress(goal_id)
        buttons = InlineKeyboardMarkup([[
            InlineKeyboardButton("Yes, delete", callback_data=f"delgoalconfirm:{goal_id}"),
            InlineKeyboardButton("Cancel", callback_data="delcancel"),
        ]])
        extra = (f" It has ₹{saved:,.0f} saved — deleting it also removes those contribution entries."
                 if saved else "")
        await query.edit_message_text(f"Delete goal '{goal[2]}'?{extra}", reply_markup=buttons)

    elif action == "delgoalconfirm":
        goal_id = int(parts[1])
        db.delete_goal(user_id, goal_id)
        await query.edit_message_text("Goal deleted.")

    elif action == "reset":
        sub = parts[1]
        if sub == "confirm":
            db.delete_everything(user_id)
            conv_state.pop(user_id, None)
            pending.pop(user_id, None)
            pending_savings.pop(user_id, None)
            await query.edit_message_text("Everything has been erased. Let's set you up again.")
            await begin_onboarding(query, user_id)  # query.message works the same way update.message does
        else:
            await query.edit_message_text("Cancelled — nothing was erased.")


# ============================================================
# SUMMARIES: /today  /month  /networth
# ============================================================

async def send_summary(update, user_id, label, start, end):
    opening = (db.sum_between(user_id, "income", "2000-01-01 00:00:00", start)
               - db.sum_between(user_id, "expense", "2000-01-01 00:00:00", start))
    expenses = db.expenses_by_category_between(user_id, start, end)
    income = db.sum_between(user_id, "income", start, end)
    spent = sum(a for _, a in expenses)
    closing = opening + income - spent

    lines = [label, "", f"Opening balance: ₹{opening:,.0f}", ""]
    for category, amount in expenses[:8]:
        lines.append(f"  {category}: ₹{amount:,.0f}")
    lines += ["", f"Spent: ₹{spent:,.0f}", f"Earned: ₹{income:,.0f}", f"Closing balance: ₹{closing:,.0f}"]
    await update.message.reply_text("\n".join(lines))


async def today_cmd(update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    start = datetime.now().strftime("%Y-%m-%d") + " 00:00:00"
    end = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d") + " 00:00:00"
    await send_summary(update, user_id, "Today", start, end)


async def month_cmd(update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    settings = db.get_settings(user_id)
    salary_day = settings["salary_day"] if settings else None
    start, end = db.cycle_bounds(salary_day)
    await send_summary(update, user_id, "This Month", start, end)


async def networth_cmd(update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    balance = (db.sum_between(user_id, "income", "2000-01-01 00:00:00", "2100-01-01 00:00:00")
               - db.sum_between(user_id, "expense", "2000-01-01 00:00:00", "2100-01-01 00:00:00"))
    goals = db.get_goals(user_id)
    goal_total = sum(db.goal_progress(g[0]) for g in goals)
    total = balance + goal_total
    lines = [
        "Net Worth Snapshot", "",
        f"Cash balance: ₹{balance:,.0f}",
        f"In savings goals: ₹{goal_total:,.0f}",
        f"Total: ₹{total:,.0f}",
    ]
    await update.message.reply_text("\n".join(lines))


# ============================================================
# BUDGETS: /setbudget  /budgets  /suggest
# ============================================================

async def set_budget_cmd(update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 2:
        await update.message.reply_text("Usage: /setbudget Food 5000")
        return
    category = context.args[0].capitalize()
    try:
        limit = float(context.args[1])
    except ValueError:
        await update.message.reply_text("Amount must be a number.")
        return
    db.set_budget(update.effective_user.id, category, limit)
    await update.message.reply_text(f"Budget set: {category} -> ₹{limit:,.0f}/month")


async def budgets_cmd(update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    settings = db.get_settings(user_id)
    salary_day = settings["salary_day"] if settings else None
    start, end = db.cycle_bounds(salary_day)
    budgets = db.get_budgets(user_id)
    if not budgets:
        await update.message.reply_text("No budgets set yet. Try /setbudget Food 5000")
        return
    spent_map = dict(db.expenses_by_category_between(user_id, start, end))
    lines = ["Budgets", ""]
    for category, limit in budgets.items():
        spent = spent_map.get(category, 0)
        pct = spent / limit * 100 if limit else 0
        flag = "OVER" if pct >= 100 else "near limit" if pct >= 80 else "ok"
        lines.append(f"{category}: ₹{spent:,.0f} / ₹{limit:,.0f} ({pct:.0f}%) [{flag}]")
    await update.message.reply_text("\n".join(lines))


async def suggest_cmd(update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    settings = db.get_settings(user_id)
    salary_day = settings["salary_day"] if settings else None
    start, end = db.cycle_bounds(salary_day)
    categories = db.all_categories_spent(user_id, start, end)
    existing = db.get_budgets(user_id)

    if not categories:
        await update.message.reply_text("Not enough data yet — log a few weeks of expenses first.")
        return

    lines = ["Suggested monthly budgets (based on your last 3 months)", ""]
    suggested_any = False
    for category in categories:
        if category in existing:
            continue
        avg = db.category_avg_last_n_months(user_id, category, 3)
        rounded = round(avg, -2) if avg >= 100 else round(avg)
        lines.append(f"{category}: ~₹{avg:,.0f}  ->  /setbudget {category} {rounded:.0f}")
        suggested_any = True

    if not suggested_any:
        await update.message.reply_text("You already have budgets set for all your spending categories.")
        return
    await update.message.reply_text("\n".join(lines))


# ============================================================
# GOALS: /goals  /newgoal
# ============================================================

async def goals_cmd(update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    goals = db.get_goals(user_id)
    if not goals:
        await update.message.reply_text("No goals yet. Try /newgoal Phone 40000, or just send '500 savings'.")
        return

    lines = ["Savings Goals", ""]
    for goal_id, name, target, target_date in goals:
        saved = db.goal_progress(goal_id)
        if target:
            pct = min(saved / target * 100, 100)
            filled = int(pct // 10)
            bar = "#" * filled + "-" * (10 - filled)
            lines.append(f"{name}: ₹{saved:,.0f} / ₹{target:,.0f} ({pct:.0f}%)  [{bar}]")
        else:
            lines.append(f"{name}: ₹{saved:,.0f} saved (no target set)")
    await update.message.reply_text("\n".join(lines))


async def newgoal_cmd(update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /newgoal Phone 40000  (target amount is optional)")
        return
    user_id = update.effective_user.id
    if len(context.args) >= 2 and context.args[-1].replace(".", "", 1).isdigit():
        name = " ".join(context.args[:-1])
        target = float(context.args[-1])
    else:
        name = " ".join(context.args)
        target = None
    db.create_goal(user_id, name, target)
    extra = f" — target ₹{target:,.0f}" if target else ""
    await update.message.reply_text(f"Goal created: {name}{extra}")


# ============================================================
# SEARCH & EXPORT: /find  /export
# ============================================================

async def find_cmd(update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /find zomato")
        return
    keyword = " ".join(context.args)
    rows = db.find_transactions(update.effective_user.id, keyword)
    if not rows:
        await update.message.reply_text(f"No entries matching '{keyword}'.")
        return
    lines = [f"Results for '{keyword}'", ""]
    for amount, category, note, created_at in rows:
        note_part = f" ({note})" if note else ""
        lines.append(f"{created_at[:10]} — ₹{amount:,.0f} — {category}{note_part}")
    await update.message.reply_text("\n".join(lines))


async def export_cmd(update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if context.args:
        try:
            year, mon = context.args[0].split("-")
            year, mon = int(year), int(mon)
            start = f"{year}-{mon:02d}-01 00:00:00"
            next_month, next_year = (mon + 1, year) if mon < 12 else (1, year + 1)
            end = f"{next_year}-{next_month:02d}-01 00:00:00"
        except ValueError:
            await update.message.reply_text("Usage: /export 2026-09  (or no argument for this month)")
            return
    else:
        settings = db.get_settings(user_id)
        salary_day = settings["salary_day"] if settings else None
        start, end = db.cycle_bounds(salary_day)

    rows = db.export_range(user_id, start, end)
    if not rows:
        await update.message.reply_text("No entries in that range.")
        return

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Date", "Type", "Category", "Amount", "Note"])
    writer.writerows(rows)
    file_bytes = io.BytesIO(buf.getvalue().encode())
    file_bytes.name = f"export_{start[:7]}.csv"
    await update.message.reply_document(document=file_bytes)


# ============================================================
# CHARTS: /chart (pie)  /trend (month-on-month bar)
# ============================================================

async def chart_cmd(update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    settings = db.get_settings(user_id)
    salary_day = settings["salary_day"] if settings else None
    start, end = db.cycle_bounds(salary_day)
    expenses = db.expenses_by_category_between(user_id, start, end)
    if not expenses:
        await update.message.reply_text("No expenses logged this cycle yet.")
        return

    labels = [c for c, _ in expenses]
    values = [a for _, a in expenses]

    fig, ax = chart_style.new_figure()
    ax.pie(values, labels=labels, autopct="%1.0f%%", colors=chart_style.PALETTE,
           wedgeprops={"linewidth": 1, "edgecolor": "white"})
    ax.set_title("Expenses This Month", fontsize=13, fontweight="bold")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    buf.seek(0)
    plt.close(fig)
    await update.message.reply_photo(photo=buf)


async def trend_cmd(update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    months = int(context.args[0]) if context.args and context.args[0].isdigit() else 3
    settings = db.get_settings(user_id)
    salary_day = settings["salary_day"] if settings else None
    cycles = db.last_n_cycles(salary_day, months)

    per_month = []
    all_categories = set()
    for label, start, end in cycles:
        data = dict(db.expenses_by_category_between(user_id, start, end))
        per_month.append((label, data))
        all_categories.update(data.keys())
    all_categories = sorted(all_categories)

    if not all_categories:
        await update.message.reply_text("Not enough data yet.")
        return

    fig, ax = chart_style.new_figure(figsize=(7, 5))
    bar_width = 0.8 / len(per_month)
    x_positions = range(len(all_categories))

    for i, (label, data) in enumerate(per_month):
        values = [data.get(cat, 0) for cat in all_categories]
        offsets = [x + i * bar_width for x in x_positions]
        ax.bar(offsets, values, width=bar_width, label=label,
               color=chart_style.PALETTE[i % len(chart_style.PALETTE)])

    ax.set_xticks([x + bar_width * (len(per_month) - 1) / 2 for x in x_positions])
    ax.set_xticklabels(all_categories, rotation=20, ha="right")
    ax.set_title("Spending by Month", fontsize=13, fontweight="bold")
    ax.legend(frameon=False)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    buf.seek(0)
    plt.close(fig)
    await update.message.reply_photo(photo=buf)


# ============================================================
# SETTINGS: /setreminder  /reminder  /addrecurring  /settings
# ============================================================

async def set_reminder_cmd(update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or ":" not in context.args[0]:
        await update.message.reply_text("Usage: /setreminder 21:30")
        return
    time_str = context.args[0]
    try:
        hh, mm = time_str.split(":")
        hh, mm = int(hh), int(mm)
        assert 0 <= hh < 24 and 0 <= mm < 60
    except (ValueError, AssertionError):
        await update.message.reply_text("Please use 24-hour HH:MM, e.g. 21:30")
        return
    db.update_settings(update.effective_user.id, reminder_time=time_str)
    await update.message.reply_text(f"Daily reminder set for {time_str}.")


async def reminder_off_cmd(update, context: ContextTypes.DEFAULT_TYPE):
    db.update_settings(update.effective_user.id, reminder_time=None)
    await update.message.reply_text("Daily reminder turned off.")


async def addrecurring_cmd(update, context: ContextTypes.DEFAULT_TYPE):
    conv_state[update.effective_user.id] = {"flow": "recurring"}
    await update.message.reply_text("Send it as: Label Day  (e.g. Gym 5)")


async def settings_cmd(update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    settings = db.get_settings(user_id)
    recurring = db.get_recurring(user_id)

    lines = ["Your Settings", ""]
    lines.append(f"Salary day: {settings['salary_day'] if settings and settings['salary_day'] else 'not set'}")
    lines.append(f"Daily reminder: {settings['reminder_time'] if settings and settings['reminder_time'] else 'off'}")
    if recurring:
        lines.append("")
        lines.append("Recurring dates:")
        for _, label, day in recurring:
            lines.append(f"  {label} — day {day}")
    lines.append("")
    lines.append("Change with /setreminder HH:MM or /addrecurring")
    lines.append("Delete data with /delete, /deletegoal, or /resetall")
    await update.message.reply_text("\n".join(lines))


# ============================================================
# /menu — a button-first home screen
# ============================================================

async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("Today", callback_data="menu:today"),
         InlineKeyboardButton("This Month", callback_data="menu:month")],
        [InlineKeyboardButton("Budgets", callback_data="menu:budgets"),
         InlineKeyboardButton("Goals", callback_data="menu:goals")],
        [InlineKeyboardButton("Pie Chart", callback_data="menu:chart"),
         InlineKeyboardButton("Trend", callback_data="menu:trend")],
        [InlineKeyboardButton("Net Worth", callback_data="menu:networth"),
         InlineKeyboardButton("Settings", callback_data="menu:settings")],
    ])
    await update.message.reply_text("What would you like to see?", reply_markup=buttons)


# ============================================================
# BACKGROUND JOBS: daily reminder + weekly digest
# ============================================================

async def daily_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Runs every minute. Fires for anyone whose reminder time has passed in the
    last 10 minutes (not just an exact match) — this covers brief gaps where
    Render's free tier was asleep or mid-restart right at the target minute.
    """
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    for user_id, reminder_time in db.all_users_with_reminders():
        key = (user_id, today)
        if key in sent_reminders_today:
            continue
        try:
            hh, mm = map(int, reminder_time.split(":"))
            target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        except ValueError:
            continue
        if target <= now <= target + timedelta(minutes=10) and not db.has_entries_today(user_id):
            try:
                await context.bot.send_message(user_id, "Don't forget to log today's spending!")
                sent_reminders_today.add(key)
            except Exception:
                pass  # user may have blocked the bot — safe to ignore


async def weekly_digest_job(context: ContextTypes.DEFAULT_TYPE):
    """Runs every Sunday evening for every onboarded user."""
    for user_id in db.all_onboarded_users():
        end = datetime.now().strftime("%Y-%m-%d") + " 23:59:59"
        start = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d") + " 00:00:00"
        expenses = db.expenses_by_category_between(user_id, start, end)
        if not expenses:
            continue

        total = sum(a for _, a in expenses)
        lines = ["Weekly Digest", "", f"You spent ₹{total:,.0f} this week.", ""]
        for category, amount in expenses[:5]:
            weekly_avg = db.category_avg_last_n_months(user_id, category, 3) / 4
            if weekly_avg > 0 and amount > weekly_avg * 1.3:
                lines.append(f"{category}: ₹{amount:,.0f}  (higher than usual)")
            else:
                lines.append(f"{category}: ₹{amount:,.0f}")
        try:
            await context.bot.send_message(user_id, "\n".join(lines))
        except Exception:
            pass


# ============================================================
# APP SETUP
# ============================================================

db.init_db()
app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("help", help_cmd))
app.add_handler(CommandHandler("menu", menu_cmd))
app.add_handler(CommandHandler("today", today_cmd))
app.add_handler(CommandHandler("month", month_cmd))
app.add_handler(CommandHandler("networth", networth_cmd))
app.add_handler(CommandHandler("setbudget", set_budget_cmd))
app.add_handler(CommandHandler("budgets", budgets_cmd))
app.add_handler(CommandHandler("suggest", suggest_cmd))
app.add_handler(CommandHandler("goals", goals_cmd))
app.add_handler(CommandHandler("newgoal", newgoal_cmd))
app.add_handler(CommandHandler("deletegoal", deletegoal_cmd))
app.add_handler(CommandHandler("category", category_cmd))
app.add_handler(CommandHandler("find", find_cmd))
app.add_handler(CommandHandler("export", export_cmd))
app.add_handler(CommandHandler("chart", chart_cmd))
app.add_handler(CommandHandler("trend", trend_cmd))
app.add_handler(CommandHandler("setreminder", set_reminder_cmd))
app.add_handler(CommandHandler("reminder", reminder_off_cmd))  # /reminder off (only "off" is supported)
app.add_handler(CommandHandler("addrecurring", addrecurring_cmd))
app.add_handler(CommandHandler("settings", settings_cmd))
app.add_handler(CommandHandler("delete", delete_cmd))
app.add_handler(CommandHandler("resetall", resetall_cmd))

app.add_handler(CallbackQueryHandler(button_tap))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, log_transaction))

app.job_queue.run_repeating(daily_reminder_job, interval=60, first=10)
app.job_queue.run_daily(weekly_digest_job, time=dtime(hour=18, minute=0), days=(6,))  # Sunday

print("Bot is running...")
app.run_polling()