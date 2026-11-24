-- ============================================================================
-- DataVigil — Aurora PostgreSQL demo schema
-- Run in psql / pgAdmin / Aurora Query Editor:
--     psql -h <cluster-endpoint> -U <user> -d <db> -f aurora_setup.sql
-- Creates schema `demo` with source + staging tables that mirror the CSV
-- sample files exactly, so Reconcile results match whether the target is a
-- file or an Aurora table. Also creates deliberately-broken *_bad tables for
-- the failing test cases, `enroll_*` junction tables for the M:N reconcile
-- case, and `products` (204 rows, planted DQ issues) for
-- profiling/rules/anomaly tests.
-- ============================================================================

BEGIN;
DROP SCHEMA IF EXISTS demo CASCADE;
CREATE SCHEMA demo;
SET search_path = demo;

-- ---------------------------------------------------------------------------
-- customers : file customers_src.csv -> demo.stg_customers  (1:1, derived name)
-- ---------------------------------------------------------------------------
CREATE TABLE src_customers (
    customer_id  INT PRIMARY KEY,
    first_name   VARCHAR(50),
    last_name    VARCHAR(50),
    email        VARCHAR(120),
    segment      VARCHAR(10)
);
INSERT INTO src_customers VALUES
 (101,'Asha','Rao','asha.rao@example.com','Gold'),
 (102,'Biju','Mathew','biju.mathew@example.com','Silver'),
 (103,'Cara','Diaz','cara.diaz@example.com','Gold'),
 (104,'Dev','Patel','dev.patel@example.com','Bronze'),
 (105,'Elena','Fox','elena.fox@example.com','Silver'),
 (106,'Farid','Khan','farid.khan@example.com','Gold'),
 (107,'Gita','Nair','gita.nair@example.com','Bronze'),
 (108,'Hiro','Tanaka','hiro.tanaka@example.com','Silver');

CREATE TABLE stg_customers (
    customer_id  INT,
    full_name    VARCHAR(101),
    email        VARCHAR(120),
    segment      VARCHAR(10)
);
INSERT INTO stg_customers
SELECT customer_id, first_name || ' ' || last_name, email, segment
FROM src_customers;

CREATE TABLE stg_customers_bad AS
    SELECT * FROM stg_customers WHERE customer_id <> 106;   -- missing in target
INSERT INTO stg_customers_bad VALUES
    (999, 'Ghost User', 'g@example.com', 'Gold');          -- orphan
UPDATE stg_customers_bad
   SET full_name = 'C X' WHERE customer_id = 103;          -- value mismatch

-- ---------------------------------------------------------------------------
-- orders : file orders_src.csv -> demo.stg_orders (1:1, derived total, sum)
-- ---------------------------------------------------------------------------
CREATE TABLE src_orders (
    order_id     INT PRIMARY KEY,
    customer_id  INT,
    qty          INT,
    unit_price   NUMERIC(10,2),
    order_date   DATE,
    status       VARCHAR(10)
);
INSERT INTO src_orders VALUES
 (201,101,2,10.00,'2024-06-01','SHIPPED'),
 (202,102,1,25.50,'2024-06-01','SHIPPED'),
 (203,103,5,4.75,'2024-06-02','PENDING'),
 (204,101,3,12.00,'2024-06-03','SHIPPED'),
 (205,104,2,8.25,'2024-06-03','CANCELLED'),
 (206,105,4,15.00,'2024-06-04','SHIPPED'),
 (207,106,1,99.99,'2024-06-05','PENDING'),
 (208,107,6,3.50,'2024-06-05','SHIPPED'),
 (209,108,2,45.00,'2024-06-06','SHIPPED'),
 (210,103,1,12.75,'2024-06-07','PENDING'),
 (211,104,3,7.00,'2024-06-08','SHIPPED'),
 (212,102,2,18.40,'2024-06-08','SHIPPED');

CREATE TABLE stg_orders (
    order_id     INT,
    customer_id  INT,
    order_total  NUMERIC(12,2),
    order_date   DATE,
    status       VARCHAR(10)
);
INSERT INTO stg_orders
SELECT order_id, customer_id, qty * unit_price, order_date, status
FROM src_orders;

CREATE TABLE stg_orders_bad AS
    SELECT * FROM stg_orders WHERE order_id <> 205;        -- missing in target
INSERT INTO stg_orders_bad VALUES
    (999, 101, 5.00, '2024-06-09', 'SHIPPED');             -- orphan
UPDATE stg_orders_bad
   SET order_total = 9.99 WHERE order_id = 207;            -- wrong total

-- ---------------------------------------------------------------------------
-- order_lines : demo.src_orders -> demo.order_lines  (1:N fanout, 2 per order)
-- ---------------------------------------------------------------------------
CREATE TABLE order_lines (
    order_id     INT,
    line_no      INT,
    sku          VARCHAR(20),
    line_amount  NUMERIC(12,2)
);
INSERT INTO order_lines (order_id, line_no, sku, line_amount)
SELECT order_id, g.line_no,
       ('SKU-' || (ARRAY['FAS','BEA','SEA','FIL'])[1 + ((order_id + g.line_no) % 4)]
        || '-000' || (1 + (order_id % 4))),
       ROUND(qty * unit_price / 2, 2)
FROM src_orders, generate_series(1, 2) AS g(line_no);

-- ---------------------------------------------------------------------------
-- transactions : demo.txn -> demo.daily_summary  (N:1 aggregate)
-- ---------------------------------------------------------------------------
CREATE TABLE txn (
    txn_id    VARCHAR(8) PRIMARY KEY,
    txn_date  DATE,
    amount    NUMERIC(12,2)
);
INSERT INTO txn VALUES
 ('T001','2024-07-01',100.00),('T002','2024-07-01',250.50),
 ('T003','2024-07-01',75.25),('T004','2024-07-01',60.00),
 ('T005','2024-07-01',40.00),('T006','2024-07-02',300.00),
 ('T007','2024-07-02',120.00),('T008','2024-07-02',80.75),
 ('T009','2024-07-02',55.50),('T010','2024-07-02',210.00),
 ('T011','2024-07-03',90.00),('T012','2024-07-03',140.25),
 ('T013','2024-07-03',33.00),('T014','2024-07-03',275.00),
 ('T015','2024-07-03',18.50),('T016','2024-07-04',500.00),
 ('T017','2024-07-04',220.00),('T018','2024-07-04',175.00),
 ('T019','2024-07-04',95.00),('T020','2024-07-04',310.00);

CREATE TABLE daily_summary (
    txn_date      DATE,
    txn_count     INT,
    total_amount  NUMERIC(14,2)
);
INSERT INTO daily_summary
SELECT txn_date, COUNT(*), SUM(amount) FROM txn GROUP BY txn_date;

CREATE TABLE daily_summary_bad AS SELECT * FROM daily_summary;
UPDATE daily_summary_bad
   SET total_amount = total_amount + 100
 WHERE txn_date = '2024-07-03';                           -- wrong aggregate

-- ---------------------------------------------------------------------------
-- enrollments : demo.enroll_src -> demo.enroll_tgt  (M:N junction, composite
-- key student_id+course_id — both columns repeat on both sides)
-- ---------------------------------------------------------------------------
CREATE TABLE enroll_src (
    student_id  VARCHAR(8),
    course_id   VARCHAR(8)
);
INSERT INTO enroll_src VALUES
 ('S1','M101'),('S1','M102'),('S2','M101'),('S3','M103'),('S3','M101');

CREATE TABLE enroll_tgt AS SELECT * FROM enroll_src;

CREATE TABLE enroll_tgt_bad AS
    SELECT * FROM enroll_src WHERE NOT (student_id='S3' AND course_id='M103');
INSERT INTO enroll_tgt_bad VALUES ('S9','M999');           -- orphan

-- ---------------------------------------------------------------------------
-- products : profiling / rules / anomaly demo — 200 base + 4 dup = 204 rows
--   - ~6% NULL supplier   - 4 duplicate rows   - 6 negative unit_cost
--   - 3 list_price < unit_cost (business-rule violation)
--   - 5 extreme price outliers (isolation forest / IQR should flag)
-- ---------------------------------------------------------------------------
CREATE TABLE products (
    product_id        INT,
    product_name      VARCHAR(60),
    sku               VARCHAR(20),
    category          VARCHAR(20),
    supplier          VARCHAR(40),
    supplier_email    VARCHAR(80),
    unit_cost         NUMERIC(10,2),
    list_price        NUMERIC(10,2),
    stock_qty         INT,
    last_restock_date DATE
);
INSERT INTO products
SELECT i,
       initcap(cat) || ' Item ' || i,
       'SKU-' || upper(substr(cat,1,3)) || '-' || lpad(i::text,5,'0'),
       cat,
       CASE WHEN i % 16 = 0 THEN NULL
            ELSE (ARRAY['Acme Corp','BoltWorks','SealRight','FilterPro',
                        'VoltEdge','HydroLine'])[1 + (i % 6)] END,
       CASE WHEN i % 12 = 0 THEN NULL
            ELSE 'orders@' ||
                 lower((ARRAY['acme','boltworks','sealright','filterpro',
                              'voltedge','hydroline'])[1 + (i % 6)]) ||
                 '.com' END,
       ROUND((50 + (i * 7919 % 1500)) / 10.0, 2),
       ROUND((50 + (i * 7919 % 1500)) / 10.0 * 1.35, 2),
       (i * 37) % 400,
       DATE '2024-01-01' + (i % 365)
FROM generate_series(1,200) i,
     LATERAL (SELECT (ARRAY['Fasteners','Bearings','Seals','Filters',
                            'Electrical','Hydraulic'])[1 + (i % 6)] AS cat) c;

UPDATE products SET unit_cost = -unit_cost WHERE product_id IN (5,17,23,58,71,140);
UPDATE products SET list_price = unit_cost * 0.5
  WHERE product_id IN (12, 90, 155);                     -- price below cost
UPDATE products SET list_price = list_price * 12
  WHERE product_id IN (33, 77, 121, 166, 189);           -- outliers
INSERT INTO products SELECT * FROM products WHERE product_id IN (3, 44, 88, 150);

COMMIT;

-- ---------------------------------------------------------------------------
-- Verification — expect these counts after the run:
-- ---------------------------------------------------------------------------
SELECT 'src_customers'    tbl, COUNT(*) FROM src_customers      -- 8
UNION ALL SELECT 'stg_customers',    COUNT(*) FROM stg_customers     -- 8
UNION ALL SELECT 'stg_customers_bad',COUNT(*) FROM stg_customers_bad -- 8
UNION ALL SELECT 'src_orders',       COUNT(*) FROM src_orders        -- 12
UNION ALL SELECT 'stg_orders',       COUNT(*) FROM stg_orders        -- 12
UNION ALL SELECT 'stg_orders_bad',   COUNT(*) FROM stg_orders_bad    -- 12
UNION ALL SELECT 'order_lines',      COUNT(*) FROM order_lines       -- 24
UNION ALL SELECT 'txn',              COUNT(*) FROM txn               -- 20
UNION ALL SELECT 'daily_summary',    COUNT(*) FROM daily_summary     -- 4
UNION ALL SELECT 'daily_summary_bad',COUNT(*) FROM daily_summary_bad -- 4
UNION ALL SELECT 'enroll_src',       COUNT(*) FROM enroll_src        -- 5
UNION ALL SELECT 'enroll_tgt',       COUNT(*) FROM enroll_tgt        -- 5
UNION ALL SELECT 'enroll_tgt_bad',   COUNT(*) FROM enroll_tgt_bad    -- 5
UNION ALL SELECT 'products',         COUNT(*) FROM products          -- 204
ORDER BY tbl;
