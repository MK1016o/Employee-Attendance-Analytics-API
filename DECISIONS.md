# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.**
Unique indexes on `employees(emp_code)` and `attendance_logs(emp_code, date)` guarantee identity uniqueness and atomic race-prevention. We added compound indexes on `employees(department, joined_on)` and `(joined_on)` for headcount filters, `attendance_logs(date, emp_code)` for sorted listing, `(emp_code, punch_in)` for shift matching, and `(date, late_minutes)` for leaderboard scans. We rejected a massive covering index with derived fields because index update overhead on frequent punches outweighs read gains.

2. **Punch-in race.**
Both concurrent requests determine the attendance date and execute `insert_one` without check-then-insert. MongoDB's storage engine enforces the unique index on `(emp_code, date)`: one insert commits returning 201 Created; the other raises `DuplicateKeyError`, which our exception handler translates to 409 Conflict.

3. **Ties.**
We compute standard competition rank (1, 2, 2, 4) using `$setWindowFields` with `$rank`, then match `rank <= limit`. Tied employees share the same rank and both qualify (exceeding `limit`), ordered deterministically by `emp_code` ascending.

4. **Headcount.**
The aggregation starts on `employees` (filtering `joined_on <= end_of_month`) and performs a left-outer `$lookup` to `attendance_logs`. Zero-log employees produce empty arrays, contributing 1 to headcount via `$sum: 1` and 0 to attendance metrics.

5. **One thing you would change** if this had to serve 100x the data.
For 100x data, I would pre-aggregate daily department metrics via scheduled `$merge` pipelines instead of computing on the fly, and shard `attendance_logs` by `(emp_code, date)`.
