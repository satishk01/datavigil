       01  CUSTOMER-RECORD.
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
