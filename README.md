# Employee Attendance & Analytics API

FastAPI and MongoDB backend implementation for employee attendance tracking, manual corrections with audit trails, and aggregation-driven analytics pipelines.

## Requirements
- Python 3.11+
- MongoDB 6.0+ (Tested on MongoDB 7.0 / 9.0)

## Quick Start

```bash
# 1. Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate    # On Windows: .\.venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Seed sample data (optional)
python sample_seed.py

# 4. Start the application
uvicorn app.main:app --port 8000
```

The service will be ready at `http://localhost:8000`. Interactive OpenAPI documentation is available at `http://localhost:8000/docs`.

## Environment Variables
- `MONGO_URI`: MongoDB connection string (default: `mongodb://localhost:27017`)
- `MONGO_DB`: Target database name (default: `attendance_db`)

All environment variables are loaded dynamically from the environment or a local `.env` file, with real system environment variables taking precedence.

## Key Design Highlights
- **Single File Architecture:** All application code, Pydantic validation models, helper functions, and endpoint handlers reside in `app/main.py`.
- **Database Indexing:** Unique and compound indexes are created idempotently on startup via FastAPI `lifespan`, eliminating collection scans (`COLLSCAN`) and ensuring race-safe operations.
- **Pure MongoDB Aggregations:** Analytics endpoints (`monthly`, `departments/summary`, `leaderboard/late`, `departments/trend`) execute purely within MongoDB aggregation pipelines using `$lookup`, `$densify`, and `$setWindowFields` rather than Python in-memory loops.
- **Race Safety:** Atomic operations (`find_one_and_update`, storage-level unique constraint collision handling) guarantee concurrency safety for punch-ins, punch-outs, and regularizations.
- **Contract Adherence:** Strict compliance with OpenAPI 3.0 schemas and business rules R1–R10 (timezones, grace periods, half-up rounding, overtime cutoffs, audit history).

## Deliverables
- `app/main.py`: Complete production implementation.
- `requirements.txt`: Project dependencies.
- `REVIEW.md`: Comprehensive review and catalog of defects found in the starter code.
- `DECISIONS.md`: Architectural explanations (indexing, race handling, ties, headcount, 100x scaling).
- `README.md`: Overview and setup instructions.
