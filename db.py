"""
db.py — everything related to talking to the MongoDB database.
No Telegram code lives here; this file only knows about data.
"""

import os
import re
import calendar
from datetime import datetime, timedelta, date

from dotenv import load_dotenv
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument

load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI")
if not MONGODB_URI:
    raise RuntimeError("MONGODB_URI is not set in the environment.")

client = MongoClient(
    MONGODB_URI,
    serverSelectionTimeoutMS=10000,
    connectTimeoutMS=10000,
)
mongo_db = client["finance_bot"]

transactions = mongo_db["transactions"]
budgets = mongo_db["budgets"]
settings = mongo_db["settings"]
recurring_dates = mongo_db["recurring_dates"]
goals = mongo_db["goals"]
counters = mongo_db["_counters"]

# salary_day value meaning "last working day of the month" instead of a fixed date
LWD_SENTINEL = -1


def init_db():
    """Verify MongoDB access and create indexes used by the bot."""
    client.admin.command("ping")

    transactions.create_index(
        [("user_id", ASCENDING), ("created_at", DESCENDING)]
    )
    transactions.create_index(
        [("user_id", ASCENDING), ("category", ASCENDING), ("created_at", DESCENDING)]
    )
    transactions.create_index([("goal_id", ASCENDING)])

    budgets.create_index(
        [("user_id", ASCENDING), ("category", ASCENDING)],
        unique=True,
    )
    settings.create_index([("user_id", ASCENDING)], unique=True)
    recurring_dates.create_index(
        [("user_id", ASCENDING), ("day_of_month", ASCENDING)]
    )
    goals.create_index(
        [("user_id", ASCENDING), ("status", ASCENDING), ("created_at", ASCENDING)]
    )
    goals.create_index(
        [("user_id", ASCENDING), ("name_lower", ASCENDING), ("status", ASCENDING)]
    )


def _next_id(sequence_name):
    """Return the next integer ID, matching the old SQLite-style IDs."""
    doc = counters.find_one_and_update(
        {"_id": sequence_name},
        {"$inc": {"value": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return doc["value"]


# ---------------- transactions ----------------

def add_transaction(user_id, type_, amount, category, note, goal_id=None):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tx_id = _next_id("transactions")
    transactions.insert_one({
        "_id": tx_id,
        "id": tx_id,
        "user_id": user_id,
        "type": type_,
        "amount": float(amount),
        "category": category,
        "note": note,
        "goal_id": goal_id,
        "created_at": now,
    })
    return tx_id


def delete_transaction(user_id, tx_id):
    transactions.delete_one({
        "_id": int(tx_id),
        "user_id": user_id,
    })


def delete_transactions_between(user_id, start, end):
    result = transactions.delete_many({
        "user_id": user_id,
        "created_at": {"$gte": start, "$lt": end},
    })
    return result.deleted_count


def delete_all_transactions(user_id):
    result = transactions.delete_many({"user_id": user_id})
    return result.deleted_count


def sum_between(user_id, type_, start, end):
    """Total amount of a type between two timestamps (end exclusive)."""
    result = transactions.aggregate([
        {
            "$match": {
                "user_id": user_id,
                "type": type_,
                "created_at": {"$gte": start, "$lt": end},
            }
        },
        {
            "$group": {
                "_id": None,
                "total": {"$sum": "$amount"},
            }
        },
    ])
    row = next(result, None)
    return row["total"] if row else 0


def expenses_by_category_between(user_id, start, end):
    result = transactions.aggregate([
        {
            "$match": {
                "user_id": user_id,
                "type": "expense",
                "created_at": {"$gte": start, "$lt": end},
            }
        },
        {
            "$group": {
                "_id": "$category",
                "amount": {"$sum": "$amount"},
            }
        },
        {"$sort": {"amount": -1}},
    ])
    return [(row["_id"], row["amount"]) for row in result]


def category_entries(user_id, category, start, end, limit=50, offset=0):
    cursor = (
        transactions.find(
            {
                "user_id": user_id,
                "category": category,
                "created_at": {"$gte": start, "$lt": end},
            },
            {"amount": 1, "note": 1, "created_at": 1, "_id": 0},
        )
        .sort("created_at", DESCENDING)
        .skip(int(offset))
        .limit(int(limit))
    )
    return [
        (doc.get("amount", 0), doc.get("note"), doc.get("created_at"))
        for doc in cursor
    ]


def category_avg_last_n_months(user_id, category, n=3):
    """Average monthly spend on a category over the last n months."""
    end = datetime.now().replace(day=1)
    start = end - timedelta(days=30 * n)
    start_str = start.strftime("%Y-%m-%d")

    result = transactions.aggregate([
        {
            "$match": {
                "user_id": user_id,
                "category": category,
                "type": "expense",
                "created_at": {"$gte": start_str},
            }
        },
        {
            "$group": {
                "_id": None,
                "total": {"$sum": "$amount"},
            }
        },
    ])
    row = next(result, None)
    return (row["total"] if row else 0) / n


def all_categories_spent(user_id, start, end):
    return [c for c, _ in expenses_by_category_between(user_id, start, end)]


def find_transactions(user_id, keyword, limit=20):
    pattern = re.escape(keyword)
    cursor = (
        transactions.find(
            {
                "user_id": user_id,
                "$or": [
                    {"note": {"$regex": pattern, "$options": "i"}},
                    {"category": {"$regex": pattern, "$options": "i"}},
                ],
            },
            {
                "amount": 1,
                "category": 1,
                "note": 1,
                "created_at": 1,
                "_id": 0,
            },
        )
        .sort("created_at", DESCENDING)
        .limit(int(limit))
    )
    return [
        (
            doc.get("amount", 0),
            doc.get("category"),
            doc.get("note"),
            doc.get("created_at"),
        )
        for doc in cursor
    ]


def export_range(user_id, start, end):
    cursor = (
        transactions.find(
            {
                "user_id": user_id,
                "created_at": {"$gte": start, "$lt": end},
            },
            {
                "created_at": 1,
                "type": 1,
                "category": 1,
                "amount": 1,
                "note": 1,
                "_id": 0,
            },
        )
        .sort("created_at", ASCENDING)
    )
    return [
        (
            doc.get("created_at"),
            doc.get("type"),
            doc.get("category"),
            doc.get("amount", 0),
            doc.get("note"),
        )
        for doc in cursor
    ]


# ---------------- budgets ----------------

def set_budget(user_id, category, limit):
    budgets.update_one(
        {"user_id": user_id, "category": category},
        {
            "$set": {
                "monthly_limit": float(limit),
            },
            "$setOnInsert": {
                "user_id": user_id,
                "category": category,
            },
        },
        upsert=True,
    )


def get_budgets(user_id):
    return {
        doc["category"]: doc["monthly_limit"]
        for doc in budgets.find(
            {"user_id": user_id},
            {"category": 1, "monthly_limit": 1, "_id": 0},
        )
    }


def delete_budget(user_id, category):
    budgets.delete_one({
        "user_id": user_id,
        "category": category,
    })


# ---------------- settings & onboarding ----------------

def get_settings(user_id):
    row = settings.find_one({"user_id": user_id}, {"_id": 0})
    if row is None:
        return None
    return {
        "salary_day": row.get("salary_day"),
        "reminder_time": row.get("reminder_time"),
        "onboarded": bool(row.get("onboarded", 0)),
    }


def ensure_settings_row(user_id):
    settings.update_one(
        {"user_id": user_id},
        {
            "$setOnInsert": {
                "user_id": user_id,
                "salary_day": None,
                "reminder_time": None,
                "onboarded": 0,
            }
        },
        upsert=True,
    )


def update_settings(user_id, **fields):
    """update_settings(123, salary_day=5) sets just that field."""
    ensure_settings_row(user_id)
    if fields:
        settings.update_one(
            {"user_id": user_id},
            {"$set": fields},
        )


def all_users_with_reminders():
    return [
        (doc["user_id"], doc["reminder_time"])
        for doc in settings.find(
            {"reminder_time": {"$ne": None}},
            {"user_id": 1, "reminder_time": 1, "_id": 0},
        )
    ]


def all_onboarded_users():
    return [
        doc["user_id"]
        for doc in settings.find(
            {"onboarded": 1},
            {"user_id": 1, "_id": 0},
        )
    ]


def has_entries_today(user_id):
    today = datetime.now().strftime("%Y-%m-%d")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    return transactions.count_documents({
        "user_id": user_id,
        "created_at": {"$gte": today, "$lt": tomorrow},
    }) > 0


# ---------------- recurring dates ----------------

def add_recurring(user_id, label, day_of_month):
    recurring_id = _next_id("recurring_dates")
    recurring_dates.insert_one({
        "_id": recurring_id,
        "id": recurring_id,
        "user_id": user_id,
        "label": label,
        "day_of_month": int(day_of_month),
    })


def get_recurring(user_id):
    cursor = recurring_dates.find(
        {"user_id": user_id},
        {"id": 1, "label": 1, "day_of_month": 1, "_id": 0},
    ).sort("id", ASCENDING)
    return [
        (doc["id"], doc["label"], doc["day_of_month"])
        for doc in cursor
    ]


# ---------------- goals ----------------

def create_goal(user_id, name, target_amount=None, target_date=None):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    goal_id = _next_id("goals")
    goals.insert_one({
        "_id": goal_id,
        "id": goal_id,
        "user_id": user_id,
        "name": name,
        "name_lower": name.lower(),
        "target_amount": float(target_amount) if target_amount is not None else None,
        "target_date": target_date,
        "status": "active",
        "created_at": now,
    })
    return goal_id


def get_goals(user_id, status="active"):
    cursor = goals.find(
        {"user_id": user_id, "status": status},
        {
            "id": 1,
            "name": 1,
            "target_amount": 1,
            "target_date": 1,
            "_id": 0,
        },
    ).sort("created_at", ASCENDING)
    return [
        (
            doc["id"],
            doc["name"],
            doc.get("target_amount"),
            doc.get("target_date"),
        )
        for doc in cursor
    ]


def get_goal_by_name(user_id, name):
    doc = goals.find_one(
        {
            "user_id": user_id,
            "name_lower": name.lower(),
            "status": "active",
        },
        {
            "id": 1,
            "name": 1,
            "target_amount": 1,
            "target_date": 1,
            "_id": 0,
        },
    )
    if doc is None:
        return None
    return (
        doc["id"],
        doc["name"],
        doc.get("target_amount"),
        doc.get("target_date"),
    )


def get_goal_by_id(goal_id):
    doc = goals.find_one(
        {"id": int(goal_id)},
        {
            "id": 1,
            "user_id": 1,
            "name": 1,
            "target_amount": 1,
            "target_date": 1,
            "_id": 0,
        },
    )
    if doc is None:
        return None
    return (
        doc["id"],
        doc["user_id"],
        doc["name"],
        doc.get("target_amount"),
        doc.get("target_date"),
    )


def goal_progress(goal_id):
    result = transactions.aggregate([
        {"$match": {"goal_id": int(goal_id)}},
        {
            "$group": {
                "_id": None,
                "total": {"$sum": "$amount"},
            }
        },
    ])
    row = next(result, None)
    return row["total"] if row else 0


def delete_goal(user_id, goal_id):
    """Removes the goal and every contribution ever made toward it."""
    goals.delete_one({"id": int(goal_id), "user_id": user_id})
    transactions.delete_many({"goal_id": int(goal_id), "user_id": user_id})


# ---------------- full account reset ----------------

def delete_everything(user_id):
    """Wipes every trace of this user. Deleting the settings row is what makes
    them look 'brand new' again — /start checks for that row to decide
    whether onboarding should run."""
    transactions.delete_many({"user_id": user_id})
    budgets.delete_many({"user_id": user_id})
    settings.delete_many({"user_id": user_id})
    recurring_dates.delete_many({"user_id": user_id})
    goals.delete_many({"user_id": user_id})


# ---------------- financial "month" cycle helpers ----------------
# These let each person's "month" run from their salary day instead of the 1st.

def last_working_day(year, month):
    """
    Last weekday (Mon-Fri) of the given month.
    Note: this only skips weekends — there's no generic calendar for
    company or public holidays, so it won't account for those.
    """
    last_day_num = calendar.monthrange(year, month)[1]
    d = date(year, month, last_day_num)
    while d.weekday() >= 5:  # 5 = Saturday, 6 = Sunday
        d -= timedelta(days=1)
    return d


def cycle_bounds(salary_day, ref_date=None):
    """
    Returns (start_str, end_str) for the financial cycle containing ref_date.
    salary_day can be: None (plain calendar month), a day number 1-31,
    or LWD_SENTINEL (last working day of each month).
    """
    ref = ref_date or date.today()

    if salary_day is None:
        start = ref.replace(day=1)

    elif salary_day == LWD_SENTINEL:
        this_month_lwd = last_working_day(ref.year, ref.month)
        if ref >= this_month_lwd:
            start = this_month_lwd
        else:
            prev_month_end = ref.replace(day=1) - timedelta(days=1)
            start = last_working_day(prev_month_end.year, prev_month_end.month)

    else:
        day = min(salary_day, 28)
        if ref.day >= day:
            start = ref.replace(day=day)
        else:
            prev_month_end = ref.replace(day=1) - timedelta(days=1)
            start = prev_month_end.replace(day=day)

    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)

    return (
        start.strftime("%Y-%m-%d") + " 00:00:00",
        end.strftime("%Y-%m-%d") + " 00:00:00",
    )


def last_n_cycles(salary_day, n=3):
    """List of (label, start, end) for the last n financial months, oldest first."""
    cycles = []
    ref = date.today()
    for _ in range(n):
        start, end = cycle_bounds(salary_day, ref)
        label = datetime.strptime(start[:10], "%Y-%m-%d").strftime("%b %Y")
        cycles.append((label, start, end))
        ref = (
            datetime.strptime(start[:10], "%Y-%m-%d")
            - timedelta(days=1)
        ).date()
    return list(reversed(cycles))