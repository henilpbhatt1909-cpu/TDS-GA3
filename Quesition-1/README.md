# A2A Invoice Agent (GA 3 - Question 2)

An automated HTTPS Invoice Agent built with Flask and SQLite. The service processes incoming invoice packages, validates business rules against approval thresholds and PO matching, and maintains state persistence to ensure idempotency.

---

## Features
- **Idempotency & Safe Recovery:** Stores completed invoice states in SQLite. Duplicate requests return the previously calculated action without re-processing payments.
- **Business Rule Engine:** Automatically checks for PO matching and enforces maximum transaction approval limits.
- **Evidence Citation:** Provides standard human-readable evidence strings with every decision.

---

## Local Setup & Development

### 1. Prerequisites
- Python 3.10+
- Git

### 2. Virtual Environment Setup
```bash
# Navigate to the project folder
cd "GA 3/Question-1"

# Create virtual environment
python -m venv venv

# Activate virtual environment
# Windows (PowerShell):
.\venv\Scripts\Activate.ps1
# Linux / macOS:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt