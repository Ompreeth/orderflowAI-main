"""
One-off seed script: populates the (currently empty) `inventory` table
with a starter catalog of manufacturing parts, so the app has data to
work with (dashboard, orders, scans, reports, etc.).

Safe to re-run: it skips any part_name that already exists.
"""
from datetime import datetime

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from database import get_db, init_db

ITEMS = [
    # part_name,          material,        unit,   quantity, reorder_at, rfid_tag,     barcode
    ("Steel Rods",         "Steel",         "pcs",  500,  50,  "RFID-1001", "BC-100001"),
    ("Aluminum Sheets",    "Aluminum",      "pcs",  300,  40,  "RFID-1002", "BC-100002"),
    ("Copper Wire Spool",  "Copper",        "spool", 150, 20,  "RFID-1003", "BC-100003"),
    ("Ball Bearings",      "Chrome Steel",  "pcs", 1200, 100, "RFID-1004", "BC-100004"),
    ("Rubber Gaskets",     "Rubber",        "pcs",  800,  75,  "RFID-1005", "BC-100005"),
    ("Hex Bolts M8",       "Stainless Steel","pcs", 2500, 200, "RFID-1006", "BC-100006"),
    ("Hex Nuts M8",        "Stainless Steel","pcs", 2500, 200, "RFID-1007", "BC-100007"),
    ("Plastic Casings",    "ABS Plastic",   "pcs",  400,  50,  "RFID-1008", "BC-100008"),
    ("Circuit Boards",     "Fiberglass/Cu", "pcs",  250,  30,  "RFID-1009", "BC-100009"),
    ("Motor Housings",     "Cast Iron",     "pcs",  180,  25,  "RFID-1010", "BC-100010"),
    ("Hydraulic Hoses",    "Reinforced Rubber", "pcs", 220, 30, "RFID-1011", "BC-100011"),
    ("Welding Rods",       "Steel Alloy",   "kg",   600,  80,  "RFID-1012", "BC-100012"),
    ("Paint Cans",         "Epoxy Coating", "cans", 120,  20,  "RFID-1013", "BC-100013"),
    ("Packaging Boxes",    "Corrugated Cardboard", "pcs", 3000, 300, "RFID-1014", "BC-100014"),
    ("Conveyor Belts",     "Polyester/Rubber", "pcs", 40,  10,  "RFID-1015", "BC-100015"),
]


def main():
    init_db()
    conn = get_db()
    existing = {r["part_name"] for r in conn.execute("SELECT part_name FROM inventory")}

    inserted = 0
    for part_name, material, unit, quantity, reorder_at, rfid_tag, barcode in ITEMS:
        if part_name in existing:
            print(f"skip (already exists): {part_name}")
            continue
        conn.execute(
            """
            INSERT INTO inventory
                (part_name, material, unit, quantity, reorder_at, rfid_tag, barcode, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (part_name, material, unit, quantity, reorder_at, rfid_tag, barcode,
             datetime.now().isoformat()),
        )
        inserted += 1
        print(f"added: {part_name}")

    conn.commit()
    total = conn.execute("SELECT COUNT(*) c FROM inventory").fetchone()["c"]
    conn.close()
    print(f"\nInserted {inserted} new item(s). Inventory now has {total} item(s) total.")


if __name__ == "__main__":
    main()
