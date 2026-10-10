# Employee Attendance & Analytics API

A REST API developed using **Python, FastAPI, and MongoDB** to manage employee attendance, calculate attendance metrics, track attendance corrections, and generate analytics.

## Tech Stack

- Python
- FastAPI
- MongoDB 6.0+
- PyMongo
- Pytest

## Prerequisites

- Python
- MongoDB 6.0 or newer
- Git

## Setup Instructions

### 1. Clone the repository

```bash
git clone https://github.com/akshitkuma/software-engineer-trainee-assignment.git
cd software-engineer-trainee-assignment
```

### 2. Create a virtual environment

**Windows PowerShell:**

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

**Linux/macOS:**

```bash
python -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

Create a `.env` file in the project root:

```env
MONGO_URI="your-mongodb-connection-string"
MONGO_DB="attendance_db"
```

Replace the placeholder with your MongoDB connection string. Never commit `.env` or database credentials to GitHub.

### 5. Load sample data

Ensure MongoDB is accessible, then run:

```bash
python sample_seed.py
```

### 6. Run the API

```bash
uvicorn app.main:app --port 8000
```

For development with automatic reload:

```bash
uvicorn app.main:app --port 8000 --reload
```

The API runs at `http://localhost:8000`.

Interactive API documentation: `http://localhost:8000/docs` (if enabled).

## Running Tests

```bash
python -m pytest -v
```

## Project Structure

```text
app/
  main.py
  __init__.py
sample_data/
tests/
openapi.yaml
DATA_MODEL.md
README.md
REVIEW.md
DECISIONS.md
requirements.txt
sample_seed.py
```

## Database Requirement

MongoDB 6.0 or newer is required. The assignment evaluation environment uses MongoDB 7.0.

## Submission

This repository contains the attendance API implementation, dependencies, and the documentation required by the candidate assignment.