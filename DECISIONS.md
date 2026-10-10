# Design Decisions

### 1. Indexes
I created a unique index on `employees.emp_code` to prevent duplicate employee codes and a unique compound index on `(emp_code, date)` to guarantee one attendance record per employee per day. Other indexes support attendance date sorting, late-minute filtering, and department/joining-date queries. I considered a standalone status index but did not add it because I wanted to avoid extra write and storage costs without a demonstrated query benefit.

### 2. Punch-in race
If two identical punch-in requests arrive simultaneously, both may pass the initial employee lookup. The unique attendance index allows only one insert for the same employee and date. The other insert raises `DuplicateKeyError`, which the API catches and converts into HTTP 409 Conflict.

### 3. Ties
The leaderboard uses competition ranking based on total late minutes. It filters by `rank <= limit`, so employees tied at the cutoff are all returned. Employee code provides deterministic ordering without breaking equal ranks.

### 4. Headcount
The department summary starts from employees who joined on or before the month's end, then looks up their attendance records. Using `preserveNullAndEmptyArrays` retains employees with no matching logs, allowing them to count toward headcount.

### 5. Scaling
For 100 times more data, I would benchmark the actual aggregation pipelines and execution plans, then tune indexes based on measured query performance and workload.

