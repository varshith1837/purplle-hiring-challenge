"""
SQLite database layer for the Store Intelligence API.
Handles event storage, session tracking, and POS transaction management.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

import aiosqlite

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("SI_DB_PATH", "data/store_intelligence.db")

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS events (
    event_id        TEXT PRIMARY KEY,
    store_id        TEXT NOT NULL,
    camera_id       TEXT NOT NULL,
    visitor_id      TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    zone_id         TEXT,
    dwell_ms        INTEGER DEFAULT 0,
    is_staff        INTEGER DEFAULT 0,
    confidence      REAL NOT NULL,
    queue_depth     INTEGER,
    sku_zone        TEXT,
    session_seq     INTEGER,
    ingested_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_events_store_id ON events(store_id);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);
CREATE INDEX IF NOT EXISTS idx_events_visitor_id ON events(visitor_id);
CREATE INDEX IF NOT EXISTS idx_events_store_type ON events(store_id, event_type);
CREATE INDEX IF NOT EXISTS idx_events_store_zone ON events(store_id, zone_id);

CREATE TABLE IF NOT EXISTS pos_transactions (
    transaction_id  TEXT PRIMARY KEY,
    store_id        TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    order_date      TEXT,
    order_time      TEXT,
    basket_value    REAL NOT NULL,
    customer_name   TEXT,
    product_name    TEXT,
    brand_name      TEXT,
    category        TEXT
);

CREATE INDEX IF NOT EXISTS idx_pos_store ON pos_transactions(store_id);
CREATE INDEX IF NOT EXISTS idx_pos_timestamp ON pos_transactions(timestamp);
"""


class Database:
    """Async SQLite database manager."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        """Initialize the database connection and create tables."""
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA_SQL)
        await self._db.commit()
        logger.info("Database initialized at %s", self.db_path)

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._db

    # ------------------------------------------------------------------
    # Event Operations
    # ------------------------------------------------------------------

    async def insert_event(self, event: dict) -> bool:
        """Insert a single event. Returns True if inserted, False if duplicate."""
        try:
            await self.db.execute(
                """INSERT INTO events
                   (event_id, store_id, camera_id, visitor_id, event_type,
                    timestamp, zone_id, dwell_ms, is_staff, confidence,
                    queue_depth, sku_zone, session_seq)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event["event_id"],
                    event["store_id"],
                    event["camera_id"],
                    event["visitor_id"],
                    event["event_type"],
                    event["timestamp"],
                    event.get("zone_id"),
                    event.get("dwell_ms", 0),
                    1 if event.get("is_staff", False) else 0,
                    event["confidence"],
                    event.get("metadata", {}).get("queue_depth"),
                    event.get("metadata", {}).get("sku_zone"),
                    event.get("metadata", {}).get("session_seq"),
                ),
            )
            return True
        except aiosqlite.IntegrityError:
            return False  # Duplicate event_id

    async def insert_events_batch(
        self, events: list[dict]
    ) -> tuple[int, int, list[dict]]:
        """
        Insert a batch of events.
        Returns (accepted, duplicates, errors_list).
        """
        accepted = 0
        duplicates = 0
        errors = []
        for i, event in enumerate(events):
            try:
                inserted = await self.insert_event(event)
                if inserted:
                    accepted += 1
                else:
                    duplicates += 1
            except Exception as e:
                errors.append(
                    {"index": i, "event_id": event.get("event_id"), "error": str(e)}
                )
        await self.db.commit()
        return accepted, duplicates, errors

    async def get_events(
        self,
        store_id: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
        event_type: Optional[str] = None,
        exclude_staff: bool = True,
    ) -> list[dict]:
        """Query events with optional filters."""
        query = "SELECT * FROM events WHERE store_id = ?"
        params: list[Any] = [store_id]

        if exclude_staff:
            query += " AND is_staff = 0"
        if start:
            query += " AND timestamp >= ?"
            params.append(start)
        if end:
            query += " AND timestamp <= ?"
            params.append(end)
        if event_type:
            query += " AND event_type = ?"
            params.append(event_type)

        query += " ORDER BY timestamp ASC"
        cursor = await self.db.execute(query, params)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Metrics Queries
    # ------------------------------------------------------------------

    async def get_unique_visitors(self, store_id: str, start: str, end: str) -> int:
        """Count unique non-staff visitors in time window."""
        cursor = await self.db.execute(
            """SELECT COUNT(DISTINCT visitor_id) as cnt
               FROM events
               WHERE store_id = ? AND timestamp >= ? AND timestamp <= ?
               AND is_staff = 0 AND event_type IN ('ENTRY', 'REENTRY')""",
            (store_id, start, end),
        )
        row = await cursor.fetchone()
        return row["cnt"] if row else 0

    async def get_avg_dwell_by_zone(
        self, store_id: str, start: str, end: str
    ) -> list[dict]:
        """Get average dwell time per zone."""
        cursor = await self.db.execute(
            """SELECT zone_id, AVG(dwell_ms) as avg_dwell_ms, COUNT(*) as visit_count
               FROM events
               WHERE store_id = ? AND timestamp >= ? AND timestamp <= ?
               AND is_staff = 0 AND event_type = 'ZONE_DWELL'
               AND zone_id IS NOT NULL
               GROUP BY zone_id""",
            (store_id, start, end),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_current_queue_depth(self, store_id: str) -> int:
        """Get current billing queue depth (latest value)."""
        cursor = await self.db.execute(
            """SELECT queue_depth FROM events
               WHERE store_id = ? AND event_type = 'BILLING_QUEUE_JOIN'
               AND queue_depth IS NOT NULL
               ORDER BY timestamp DESC LIMIT 1""",
            (store_id,),
        )
        row = await cursor.fetchone()
        return row["queue_depth"] if row else 0

    async def get_abandonment_rate(self, store_id: str, start: str, end: str) -> float:
        """Calculate billing queue abandonment rate."""
        cursor = await self.db.execute(
            """SELECT
                 COUNT(CASE WHEN event_type = 'BILLING_QUEUE_JOIN' THEN 1 END) as joins,
                 COUNT(CASE WHEN event_type = 'BILLING_QUEUE_ABANDON' THEN 1 END) as abandons
               FROM events
               WHERE store_id = ? AND timestamp >= ? AND timestamp <= ?
               AND is_staff = 0""",
            (store_id, start, end),
        )
        row = await cursor.fetchone()
        joins = row["joins"] if row else 0
        abandons = row["abandons"] if row else 0
        if joins == 0:
            return 0.0
        return round(abandons / joins, 4)

    # ------------------------------------------------------------------
    # Funnel Queries
    # ------------------------------------------------------------------

    async def get_funnel_data(self, store_id: str, start: str, end: str) -> dict:
        """Get session-based funnel data."""
        # Unique visitors who entered (non-staff)
        entry_cursor = await self.db.execute(
            """SELECT COUNT(DISTINCT visitor_id) as cnt FROM events
               WHERE store_id = ? AND timestamp >= ? AND timestamp <= ?
               AND is_staff = 0 AND event_type IN ('ENTRY', 'REENTRY')""",
            (store_id, start, end),
        )
        entry_row = await entry_cursor.fetchone()
        entry_count = entry_row["cnt"] if entry_row else 0

        # Visitors who visited any zone
        zone_cursor = await self.db.execute(
            """SELECT COUNT(DISTINCT visitor_id) as cnt FROM events
               WHERE store_id = ? AND timestamp >= ? AND timestamp <= ?
               AND is_staff = 0 AND event_type IN ('ZONE_ENTER', 'ZONE_DWELL')""",
            (store_id, start, end),
        )
        zone_row = await zone_cursor.fetchone()
        zone_count = zone_row["cnt"] if zone_row else 0

        # Visitors who joined billing queue
        billing_cursor = await self.db.execute(
            """SELECT COUNT(DISTINCT visitor_id) as cnt FROM events
               WHERE store_id = ? AND timestamp >= ? AND timestamp <= ?
               AND is_staff = 0
               AND event_type IN ('BILLING_QUEUE_JOIN', 'ZONE_ENTER')
               AND zone_id = 'BILLING'""",
            (store_id, start, end),
        )
        billing_row = await billing_cursor.fetchone()
        billing_count = billing_row["cnt"] if billing_row else 0

        # Visitors who made a purchase (POS correlation)
        purchase_count = await self._get_purchase_count(store_id, start, end)

        return {
            "entry": entry_count,
            "zone_visit": zone_count,
            "billing_queue": billing_count,
            "purchase": purchase_count,
        }

    async def _get_purchase_count(self, store_id: str, start: str, end: str) -> int:
        """Count distinct visitors who were in billing zone near a POS transaction."""
        # Get POS transactions in the time window
        pos_cursor = await self.db.execute(
            """SELECT timestamp FROM pos_transactions
               WHERE store_id = ? AND timestamp >= ? AND timestamp <= ?""",
            (store_id, start, end),
        )
        pos_rows = await pos_cursor.fetchall()
        if not pos_rows:
            return 0

        # For each transaction, find visitors in billing zone within 5 min before
        converted_visitors: set[str] = set()
        for pos_row in pos_rows:
            pos_ts = pos_row["timestamp"]
            # 5 min before
            try:
                pos_dt = datetime.fromisoformat(pos_ts.replace("Z", "+00:00"))
                window_start = (
                    pos_dt.replace(tzinfo=None)
                    if pos_dt.tzinfo
                    else pos_dt
                )
                from datetime import timedelta
                window_start_str = (pos_dt - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
            except Exception:
                continue

            visitor_cursor = await self.db.execute(
                """SELECT DISTINCT visitor_id FROM events
                   WHERE store_id = ? AND is_staff = 0
                   AND zone_id = 'BILLING'
                   AND timestamp >= ? AND timestamp <= ?""",
                (store_id, window_start_str, pos_ts),
            )
            visitor_rows = await visitor_cursor.fetchall()
            for vr in visitor_rows:
                converted_visitors.add(vr["visitor_id"])

        return len(converted_visitors)

    # ------------------------------------------------------------------
    # Heatmap Queries
    # ------------------------------------------------------------------

    async def get_zone_heatmap(
        self, store_id: str, start: str, end: str
    ) -> list[dict]:
        """Get zone visit frequency and average dwell for heatmap."""
        cursor = await self.db.execute(
            """SELECT zone_id,
                      COUNT(DISTINCT visitor_id) as visit_count,
                      AVG(CASE WHEN dwell_ms > 0 THEN dwell_ms ELSE NULL END) as avg_dwell_ms
               FROM events
               WHERE store_id = ? AND timestamp >= ? AND timestamp <= ?
               AND is_staff = 0
               AND zone_id IS NOT NULL
               AND event_type IN ('ZONE_ENTER', 'ZONE_DWELL', 'ZONE_EXIT')
               GROUP BY zone_id""",
            (store_id, start, end),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Anomaly Queries
    # ------------------------------------------------------------------

    async def get_recent_queue_depths(self, store_id: str, minutes: int = 30) -> list[int]:
        """Get recent queue depth values for anomaly detection."""
        cursor = await self.db.execute(
            """SELECT queue_depth FROM events
               WHERE store_id = ? AND event_type = 'BILLING_QUEUE_JOIN'
               AND queue_depth IS NOT NULL
               ORDER BY timestamp DESC LIMIT ?""",
            (store_id, minutes),
        )
        rows = await cursor.fetchall()
        return [row["queue_depth"] for row in rows]

    async def get_zone_last_visit(self, store_id: str) -> dict[str, str]:
        """Get last visit timestamp per zone for dead zone detection."""
        cursor = await self.db.execute(
            """SELECT zone_id, MAX(timestamp) as last_visit FROM events
               WHERE store_id = ? AND is_staff = 0
               AND zone_id IS NOT NULL
               GROUP BY zone_id""",
            (store_id,),
        )
        rows = await cursor.fetchall()
        return {row["zone_id"]: row["last_visit"] for row in rows}

    # ------------------------------------------------------------------
    # Health Queries
    # ------------------------------------------------------------------

    async def get_store_health(self) -> list[dict]:
        """Get health data for all stores."""
        cursor = await self.db.execute(
            """SELECT store_id,
                      MAX(timestamp) as last_event_at,
                      COUNT(*) as event_count
               FROM events
               GROUP BY store_id"""
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def check_connection(self) -> bool:
        """Check database connectivity."""
        try:
            await self.db.execute("SELECT 1")
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # POS Operations
    # ------------------------------------------------------------------

    async def insert_pos_transaction(self, txn: dict) -> None:
        """Insert a POS transaction record."""
        try:
            await self.db.execute(
                """INSERT OR IGNORE INTO pos_transactions
                   (transaction_id, store_id, timestamp, order_date, order_time,
                    basket_value, customer_name, product_name, brand_name, category)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    txn["transaction_id"],
                    txn["store_id"],
                    txn["timestamp"],
                    txn.get("order_date"),
                    txn.get("order_time"),
                    txn["basket_value"],
                    txn.get("customer_name"),
                    txn.get("product_name"),
                    txn.get("brand_name"),
                    txn.get("category"),
                ),
            )
            await self.db.commit()
        except Exception as e:
            logger.error("Failed to insert POS transaction: %s", e)

    async def load_pos_csv(self, csv_path: str) -> int:
        """Load POS transactions from CSV file."""
        import csv

        count = 0
        try:
            with open(csv_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    order_date = row.get("order_date", "")
                    order_time = row.get("order_time", "")
                    # Build ISO timestamp
                    if order_date and order_time:
                        # Convert DD-MM-YYYY to YYYY-MM-DD
                        parts = order_date.split("-")
                        if len(parts) == 3:
                            iso_date = f"{parts[2]}-{parts[1]}-{parts[0]}"
                        else:
                            iso_date = order_date
                        timestamp = f"{iso_date}T{order_time}Z"
                    else:
                        timestamp = ""

                    # Map store_id
                    store_id = row.get("store_id", "")
                    if store_id == "ST1008":
                        store_id = "STORE_BLR_001"

                    basket_value = 0.0
                    try:
                        basket_value = float(row.get("total_amount", 0) or 0)
                    except (ValueError, TypeError):
                        pass

                    txn = {
                        "transaction_id": row.get("invoice_number", f"TXN_{count:05d}"),
                        "store_id": store_id,
                        "timestamp": timestamp,
                        "order_date": order_date,
                        "order_time": order_time,
                        "basket_value": basket_value,
                        "customer_name": row.get("customer_name", ""),
                        "product_name": row.get("product_name", ""),
                        "brand_name": row.get("brand_name", ""),
                        "category": row.get("sub_category", ""),
                    }
                    await self.insert_pos_transaction(txn)
                    count += 1
            logger.info("Loaded %d POS transactions from %s", count, csv_path)
        except Exception as e:
            logger.error("Failed to load POS CSV: %s", e)
        return count

    async def get_total_transactions(self, store_id: str, start: str, end: str) -> int:
        """Count distinct transactions in time window."""
        cursor = await self.db.execute(
            """SELECT COUNT(DISTINCT transaction_id) as cnt
               FROM pos_transactions
               WHERE store_id = ? AND timestamp >= ? AND timestamp <= ?""",
            (store_id, start, end),
        )
        row = await cursor.fetchone()
        return row["cnt"] if row else 0


# ---------------------------------------------------------------------------
# Global instance
# ---------------------------------------------------------------------------

db = Database()
