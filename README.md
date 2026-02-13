# PayvoraX - Fintech Platform

Complete project demonstrating **3 real-world fintech challenges** integrated, developed with professional architecture and software engineering best practices.

<img width="1779" height="1066" alt="image" src="https://github.com/user-attachments/assets/4e7fc792-2bc0-43f9-b199-b1a91e5318f7" />

<img width="1598" height="1076" alt="image" src="https://github.com/user-attachments/assets/033ba713-a2c3-4908-b0dc-bd9d3af764ce" />

<img width="1623" height="1070" alt="image" src="https://github.com/user-attachments/assets/1221b2b8-7ce4-4a97-bc42-36be789092fb" />

Fictitious Credit Cards (DEMO)

<img width="1805" height="1078" alt="image" src="https://github.com/user-attachments/assets/0199d94d-d74e-4d96-8eb0-2d9c25b514b4" />

<img width="1906" height="1064" alt="image" src="https://github.com/user-attachments/assets/21804d91-1cfe-41b9-b5a4-8d113e49ed4b" />



## Implemented Challenges

### 1. Installment Simulation Engine

- Compound interest calculation (Price Table / Amortization)
- Annualized CET (Total Effective Cost)
- Complete amortization schedule
- Rigorous input validation
- Persistence for audit trails

### 2. PIX Transaction API

- Guaranteed idempotency via `idempotency-key`
- Status control (CREATED, PROCESSING, CONFIRMED, FAILED, CANCELED)
- PIX key validation (CPF, email, phone, random key)
- Statement with aggregators
- Full traceability

### 3. Simplified Anti-Fraud Engine

- Real-time risk analysis
- Configurable rule system
- Scoring from 0-100
- Multiple rules (night time, high value, excessive attempts)
- Risk classification (LOW/MEDIUM/HIGH)

---

## Architecture

```text
Architecture: DDD (Domain-Driven Design) + Hexagonal
Layers:
  - Domain (isolated business rules)
  - Application (use cases)
  - Infrastructure (adapters: HTTP, DB)

Applied Principles:
  - Separation of Concerns
  - Dependency Injection
  - Input Validation at all boundaries
  - RBAC and Principle of Least Privilege
```

---

## Project Structure

```text
fintech-tech-challenge/
│
├── app/
│   ├── core/
│   │   ├── config.py           # Centralized configuration
│   │   ├── database.py         # SQLAlchemy connection
│   │   ├── security.py         # JWT, bcrypt, masking
│   │   └── logger.py           # Structured logging + correlation IDs
│   │
│   ├── parcelamento/           # Challenge 1
│   │   ├── models.py           # SQLAlchemy Model
│   │   ├── schemas.py          # Pydantic Validation
│   │   ├── service.py          # Business Logic
│   │   └── router.py           # FastAPI Endpoints
│   │
│   ├── pix/                    # Challenge 2
│   │   ├── models.py           # SQLAlchemy Model
│   │   ├── schemas.py          # Pydantic Validation
│   │   ├── service.py          # Business Logic
│   │   └── router.py           # FastAPI Endpoints
│   │
│   ├── antifraude/             # Challenge 3
│   │   ├── rules.py            # Rule Engine
│   │   ├── schemas.py          # Pydantic Validation
│   │   └── router.py           # FastAPI Endpoints
│   │
│   └── main.py                 # Main FastAPI App
│
├── tests/
│   ├── test_parcelamento.py   # 8 tests
│   ├── test_pix.py             # 9 tests
│   └── test_antifraude.py      # 10 tests
│
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── README.md
```

---

## How to Run

### Option 1: Docker (Recommended)

```bash
# Build and run
docker-compose up --build

# The API will be available at http://localhost:8000
```

### Option 2: Local Environment

```bash
# Create virtual environment
python -m venv venv
.\venv\Scripts\activate  # Windows
# source venv/bin/activate  # Linux/Mac

# Install dependencies
pip install -r requirements.txt

# Configure environment variables
cp .env.example .env

# Run application
uvicorn app.main:app --reload

# The API will be available at http://localhost:8000
```

---

## API Documentation

Access the interactive documentation (Swagger UI):

```text
http://localhost:8000/docs
```

Alternative documentation (ReDoc):

```text
http://localhost:8000/redoc
```

---

## Tests

```bash
# Run all tests
pytest

# With coverage
pytest --cov=app --cov-report=html

# Specific tests
pytest tests/test_parcelamento.py
pytest tests/test_pix.py
pytest tests/test_antifraude.py
```

---

## Main Endpoints

### Installments (Parcelamento)

**POST** `/parcelamento/simular`

```json
{
  "valor": 1000,
  "parcelas": 12,
  "taxa_mensal": 0.035
}
```

**Response:**

```json
{
  "parcela": 91.62,
  "total_pago": 1099.44,
  "cet_anual": 51.11,
  "tabela": [...],
  "simulacao_id": 1,
  "criado_em": "2025-11-19T10:30:00"
}
```

---

### PIX

**POST** `/pix/create`

Headers:

```text
idempotency-key: unique-key-123
```

Body:

```json
{
  "valor": 150.5,
  "chave_pix": "test@email.com",
  "tipo_chave": "EMAIL",
  "descricao": "Test payment"
}
```

**POST** `/pix/confirm`

```json
{
  "pix_id": "uuid-of-pix"
}
```

**GET** `/pix/statement?limite=50&status=CONFIRMADO`

---

### Anti-Fraud

**POST** `/antifraude/analisar`

```json
{
  "valor": 500,
  "horario": "23:30",
  "tentativas_ultimas_24h": 5
}
```

**Response:**

```json
{
  "score": 90,
  "aprovado": false,
  "motivo": "Transaction rejected - high risk detected",
  "regras_ativadas": [
    "NIGHT_TIME: Transaction performed during risk hours (22h-6h)",
    "HIGH_VALUE: Transaction value exceeds R$ 300",
    "EXCESSIVE_ATTEMPTS: More than 3 attempts in the last 24h"
  ],
  "nivel_risco": "ALTO",
  "recomendacao": "Reject and notify user"
}
```

**GET** `/antifraude/regras` - List all configured rules

---

## Security

- **Rigorous Input Validation** with Pydantic
- **Sensitive Data Masking** in logs
- **Correlation IDs** for traceability
- **Auditable Logs** for compliance
- **JWT** and **bcrypt** implemented (security module)
- **CORS** configured
- **Health checks** in Docker

---

## Observability

### Structured Logging

All logs follow a structured format:

```text
2025-11-19 10:30:15 | INFO     | fintech | corr-123-456 | Simulation calculated: value=1000
```

### Correlation IDs

All requests receive a correlation ID for end-to-end tracking:

```text
X-Correlation-ID: automatic-uuid
```

### Metrics

Each response includes processing time:

```text
X-Process-Time: 0.123
```

---

## Technical Differentiators

### Architecture Differentiators

- DDD + Hexagonal (domain isolated from frameworks)
- Clear Separation of Concerns
- Dependency Injection via FastAPI

### Code Quality

- Complete Type Hints
- Docstrings in all modules
- 27+ Unit Tests
- Input Validation at all boundaries

### Security Features

- Rigorous Input Validation
- Masked Logs
- Mandatory Audit
- Guaranteed Idempotency

### Observability Features

- Structured Logging
- Correlation IDs
- Health Checks
- Processing Time

### DevOps

- Multi-stage Docker
- Ready-to-use docker-compose
- Configured Health Checks
- Volumes for persistence

---

## Technology Stack

- **FastAPI** 0.104.1 - Modern web framework
- **SQLAlchemy** 2.0.23 - Robust ORM
- **Pydantic** 2.5.0 - Data validation
- **Python-JOSE** - JWT
- **Passlib** - Hashing bcrypt
- **Pytest** - Testing
- **Uvicorn** - ASGI server
- **SQLite** - Database (pluggable for PostgreSQL)

---

## Development Workflow

This project uses a `Makefile` to standardize development tasks.

```bash
# Install dependencies
make install

# Run local server
make run

# Run tests with coverage
make test

# Run static analysis (Linting & Type Checking)
make lint

# Auto-format code
make format
```

---

## Architecture Decision Records (ADR)

We maintain a log of significant architectural decisions in `docs/adr`.

- [ADR-001: Adoption of Hexagonal Architecture](docs/adr/001-hexagonal-architecture.md)

---

## Suggested Semantic Commit

```bash
git init
git add .
git commit -m "feat: implement complete fintech project with 3 challenges

- Installment engine with compound interest and CET
- PIX API with idempotency and status control
- Anti-fraud engine with configurable rule system
- DDD + Hexagonal Architecture
- 27+ unit tests
- Docker ready with docker-compose
- Auditable logs and correlation IDs
- Complete Swagger/ReDoc documentation"
```

---

## Interview Presentation

### Suggested Demo (10-15 minutes)

1. **Overview** (2 min)

   - Show folder structure
   - Explain DDD + Hexagonal architecture
   - Highlight separation of concerns

2. **Challenge 1 - Installments** (3 min)

   - Show calculation in `/docs`
   - Explain Price formula
   - Highlight CET and amortization schedule

3. **Challenge 2 - PIX** (3 min)

   - Demonstrate idempotency (same key = same result)
   - Show status control
   - Display statement

4. **Challenge 3 - Anti-Fraud** (3 min)

   - Test with different values
   - Show activated rules
   - Explain scoring system

5. **Technical Differentiators** (3 min)

   - Structured logs and correlation IDs
   - Unit tests
   - Docker and docker-compose
   - Security (input validation, masking)

6. **Q&A** (3 min)

---

## License

This project was developed for educational and technical demonstration purposes.

---

## Next Steps (Roadmap)

- [ ] PostgreSQL Integration
- [ ] Full JWT Authentication
- [ ] Rate Limiting
- [ ] Redis Caching
- [ ] Messaging (RabbitMQ/Kafka)
- [ ] Prometheus Metrics
- [ ] CI/CD with GitHub Actions
- [ ] Cloud Deployment (AWS/GCP/Azure)

---
Bio Code Technology inc.
Cezi Cola Senior Software Engineer
