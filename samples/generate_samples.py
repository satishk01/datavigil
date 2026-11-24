"""Regenerate every sample file used by test-steps.pdf.

    python samples/generate_samples.py

Deterministic — re-running produces byte-identical small files and a
seeded-random parts_inventory.csv (2000 rows).
"""
import csv
import json
import random
from pathlib import Path

OUT = Path(__file__).resolve().parent

# --------------------------------------------------------------------------
# 1. parts_inventory.csv — the general-purpose profiling/rules file
#    2000 rows x 9 cols; ~6% null supplier, a few negative unit_costs,
#    supplier_email ~97% valid, unit_cost<->list_price correlation ~0.99
# --------------------------------------------------------------------------
random.seed(42)
cats = ["Fasteners", "Bearings", "Seals", "Filters", "Electrical", "Hydraulic"]
suppliers = ["Acme Corp", "BoltWorks", "SealRight", "FilterPro", "VoltEdge",
             "HydroLine", "PartMax", "IndusTech"]
rows = []
for i in range(1, 2001):
    cat = random.choice(cats)
    sup = random.choice(suppliers) if random.random() > 0.06 else ""
    cost = round(random.uniform(0.5, 200), 2)
    if random.random() < 0.01:
        cost = -cost                      # ~1% negative costs -> validity finding
    price = round(cost * 1.3 + random.uniform(-2, 2), 2)
    email = (sup.lower().replace(" ", "") + "@example.com"
             if sup and random.random() < 0.97 else "")
    rows.append([f"P{i:05d}", f"{cat[:-1]} Part {i:04d}",
                 f"SKU-{cat[:3].upper()}-{i:05d}", cat, sup, email,
                 cost, price,
                 f"2024-{random.randint(1,12):02d}-{random.randint(1,28):02d}"])
with open(OUT / "parts_inventory.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["part_id", "part_name", "sku", "category", "supplier",
                "supplier_email", "unit_cost", "list_price",
                "last_restock_date"])
    w.writerows(rows)

# --------------------------------------------------------------------------
# 2. customers_src.csv + stg_customers(+_bad) — 1:1 with derived column
# --------------------------------------------------------------------------
customers = [
    (101, "Asha",  "Rao",    "asha.rao@example.com",    "Gold"),
    (102, "Biju",  "Mathew", "biju.mathew@example.com", "Silver"),
    (103, "Cara",  "Diaz",   "cara.diaz@example.com",   "Gold"),
    (104, "Dev",   "Patel",  "dev.patel@example.com",   "Bronze"),
    (105, "Elena", "Fox",    "elena.fox@example.com",   "Silver"),
    (106, "Farid", "Khan",   "farid.khan@example.com",  "Gold"),
    (107, "Gita",  "Nair",   "gita.nair@example.com",   "Bronze"),
    (108, "Hiro",  "Tanaka", "hiro.tanaka@example.com", "Silver"),
]
with open(OUT / "customers_src.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["customer_id", "first_name", "last_name", "email", "segment"])
    w.writerows(customers)


def write_stg_customers(path, bad=False):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["customer_id", "full_name", "email", "segment"])
        for cid, fn, ln, em, seg in customers:
            if bad and cid == 106:
                continue                                  # missing in target
            name = f"{fn} {ln}"
            if bad and cid == 103:
                name = "C X"                              # value mismatch
            w.writerow([cid, name, em, seg])
        if bad:
            w.writerow([999, "Ghost User", "g@example.com", "Gold"])  # orphan


write_stg_customers(OUT / "stg_customers.csv")
write_stg_customers(OUT / "stg_customers_bad.csv", bad=True)

# --------------------------------------------------------------------------
# 3. orders_src.csv + stg_orders(+_bad) — 1:1 with derived calc + aggregate
# --------------------------------------------------------------------------
orders = [  # order_id, customer_id, qty, unit_price, order_date, status
    (201, 101, 2, 10.00, "2024-06-01", "SHIPPED"),
    (202, 102, 1, 25.50, "2024-06-01", "SHIPPED"),
    (203, 103, 5, 4.75,  "2024-06-02", "PENDING"),
    (204, 101, 3, 12.00, "2024-06-03", "SHIPPED"),
    (205, 104, 2, 8.25,  "2024-06-03", "CANCELLED"),
    (206, 105, 4, 15.00, "2024-06-04", "SHIPPED"),
    (207, 106, 1, 99.99, "2024-06-05", "PENDING"),
    (208, 107, 6, 3.50,  "2024-06-05", "SHIPPED"),
    (209, 108, 2, 45.00, "2024-06-06", "SHIPPED"),
    (210, 103, 1, 12.75, "2024-06-07", "PENDING"),
    (211, 104, 3, 7.00,  "2024-06-08", "SHIPPED"),
    (212, 102, 2, 18.40, "2024-06-08", "SHIPPED"),
]
with open(OUT / "orders_src.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["order_id", "customer_id", "qty", "unit_price",
                "order_date", "status"])
    w.writerows(orders)


def write_stg_orders(path, bad=False):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["order_id", "customer_id", "order_total",
                    "order_date", "status"])
        for oid, cid, qty, price, dt, st in orders:
            if bad and oid == 205:
                continue                                  # missing in target
            total = round(qty * price, 2)
            if bad and oid == 207:
                total = 9.99                              # wrong total
            w.writerow([oid, cid, total, dt, st])
        if bad:
            w.writerow([999, 101, 5.00, "2024-06-09", "SHIPPED"])  # orphan


write_stg_orders(OUT / "stg_orders.csv")
write_stg_orders(OUT / "stg_orders_bad.csv", bad=True)

# --------------------------------------------------------------------------
# 4. order_lines.csv — 1:N fanout from orders_src (2 lines per order)
# --------------------------------------------------------------------------
skus = ["SKU-FAS-0001", "SKU-BEA-0002", "SKU-SEA-0003", "SKU-FIL-0004"]
with open(OUT / "order_lines.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["order_id", "line_no", "sku", "line_amount"])
    for oid, _, qty, price, *_ in orders:
        half = round(qty * price / 2, 2)
        w.writerow([oid, 1, skus[oid % 4], half])
        w.writerow([oid, 2, skus[(oid + 1) % 4], half])

# --------------------------------------------------------------------------
# 5. txn.csv + daily_summary(+_bad) — N:1 aggregation check
# --------------------------------------------------------------------------
txns = [(f"T{i:03d}", d, a) for i, (d, a) in enumerate([
    ("2024-07-01", 100.0), ("2024-07-01", 250.5), ("2024-07-01", 75.25),
    ("2024-07-01", 60.0),  ("2024-07-01", 40.0),
    ("2024-07-02", 300.0), ("2024-07-02", 120.0), ("2024-07-02", 80.75),
    ("2024-07-02", 55.5),  ("2024-07-02", 210.0),
    ("2024-07-03", 90.0),  ("2024-07-03", 140.25),("2024-07-03", 33.0),
    ("2024-07-03", 275.0), ("2024-07-03", 18.5),
    ("2024-07-04", 500.0), ("2024-07-04", 220.0), ("2024-07-04", 175.0),
    ("2024-07-04", 95.0),  ("2024-07-04", 310.0)], start=1)]
with open(OUT / "txn.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["txn_id", "txn_date", "amount"])
    w.writerows(txns)


def write_summary(path, bad=False):
    from collections import defaultdict
    agg = defaultdict(lambda: [0, 0.0])
    for _, d, a in txns:
        agg[d][0] += 1
        agg[d][1] += a
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["txn_date", "txn_count", "total_amount"])
        for d in sorted(agg):
            cnt, tot = agg[d]
            if bad and d == "2024-07-03":
                tot = round(tot + 100.0, 2)               # wrong aggregate
            w.writerow([d, cnt, round(tot, 2)])


write_summary(OUT / "daily_summary.csv")
write_summary(OUT / "daily_summary_bad.csv", bad=True)

# --------------------------------------------------------------------------
# 6. Format variants
# --------------------------------------------------------------------------
with open(OUT / "parts_pipe.txt", "w", newline="") as f:
    w = csv.writer(f, delimiter="|")
    w.writerow(["part_id", "part_name", "unit_cost"])
    w.writerows([[r[0], r[1], r[6]] for r in rows[:30]])

with open(OUT / "parts_semicolon.csv", "w", newline="") as f:
    w = csv.writer(f, delimiter=";")
    w.writerow(["part_id", "unit_cost"])
    for r in rows[:30]:                                   # decimal commas
        w.writerow([r[0], str(r[6]).replace(".", ",")])

with open(OUT / "customers.ndjson", "w") as f:
    for cid, fn, ln, em, seg in customers:
        f.write(json.dumps({"customer_id": cid, "first_name": fn,
                            "last_name": ln, "email": em,
                            "segment": seg}) + "\n")

# fixed-width customers: id(4) first(12) last(12) segment(8)
with open(OUT / "customers_fwf.fwf", "w") as f:
    for cid, fn, ln, em, seg in customers:
        f.write(f"{cid:<4}{fn:<12}{ln:<12}{seg:<8}\n")

# --------------------------------------------------------------------------
# 7. IMS/COBOL copybook + EBCDIC data file
# --------------------------------------------------------------------------
(OUT / "custcopy.cpy").write_text("""       01  CUSTOMER-RECORD.
           05 CUST-ID          PIC X(10).
           05 CUST-NAME        PIC X(20).
           05 CUST-BALANCE     PIC S9(7)V99 COMP-3.
           05 CUST-STATUS      PIC X(1).
           05 FILLER           PIC X(4).
           05 ORDER-COUNT      PIC 9(4) COMP.
           05 TAGS OCCURS 2 TIMES.
              10 TAG-CD        PIC X(3).
           05 CUST-TYPE        PIC X(1).
           05 CUST-TYPE-N      REDEFINES CUST-TYPE PIC 9(1).
           88 ACTIVE-CUST      VALUE 'A'.
""")


def _packed(digits, neg=False):
    nib = list(digits) + ["D" if neg else "C"]
    if len(nib) % 2:
        nib = ["0"] + nib
    out = bytearray()
    for i in range(0, len(nib), 2):
        out.append(int(nib[i], 16) << 4 | int(nib[i + 1], 16))
    return bytes(out)


def _cp037(s):
    return s.encode("cp037")


cb_rows = [
    ("CUST000001", "ASHA RAO",    "001234567", False, "A", 42, "VIP", "NEW", "R"),
    ("CUST000002", "BIJU MATHEW", "000999950", True,  "I", 7,  "PRM", "OLD", "W"),
    ("CUST000003", "CARA DIAZ",   "000050000", False, "A", 15, "NEW", "VIP", "R"),
]
data = bytearray()
for cid, name, bal, neg, st, cnt, t1, t2, ty in cb_rows:
    data += _cp037(cid).ljust(10, _cp037(" "))
    data += _cp037(name).ljust(20, _cp037(" "))
    data += _packed(bal, neg)
    data += _cp037(st)
    data += _cp037("    ")
    data += cnt.to_bytes(2, "big")
    data += _cp037(t1) + _cp037(t2)
    data += _cp037(ty)
(OUT / "custcopy.dat").write_bytes(bytes(data))

# --------------------------------------------------------------------------
# 8. Schema-gate demo files
# --------------------------------------------------------------------------
# v2: customer_id becomes a string id AND 'segment' is dropped -> breaking
with open(OUT / "customers_v2_breaking.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["customer_id", "first_name", "last_name", "email"])
    for cid, fn, ln, em, _ in customers:
        w.writerow([f"CUST-{cid:03d}", fn, ln, em])

# v3: adds nullable loyalty_tier -> compatible
with open(OUT / "customers_v3_compatible.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["customer_id", "first_name", "last_name", "email",
                "segment", "loyalty_tier"])
    for cid, fn, ln, em, seg in customers:
        w.writerow([cid, fn, ln, em, seg, f"T{cid % 3 + 1}"])

# --------------------------------------------------------------------------
# 9. M:N (many-to-many) junction demo — student enrollments
#    both key columns repeat on both sides; composite key (student, course)
# --------------------------------------------------------------------------
_enroll = [("S1", "M101"), ("S1", "M102"), ("S2", "M101"),
           ("S3", "M103"), ("S3", "M101")]
with open(OUT / "enroll_src.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["student_id", "course_id"])
    w.writerows(_enroll)
with open(OUT / "enroll_tgt.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["student_id", "course_id"])
    w.writerows(_enroll)
# bad target: (S3,M103) dropped -> missing_in_target; (S9,M999) -> orphan
with open(OUT / "enroll_tgt_bad.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["student_id", "course_id"])
    w.writerows([("S1", "M101"), ("S1", "M102"), ("S2", "M101"),
                 ("S9", "M999"), ("S3", "M101")])

print("samples written to", OUT)
