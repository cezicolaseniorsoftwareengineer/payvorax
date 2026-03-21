# BioCodeTechPay - Fintech Platform

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

- Real-time risk analysis integrated into PIX transaction and QR code payment flows
- Configurable rule system with 4 active rules
- Scoring from 0-100 (threshold: score >= 60 blocks the transaction)
- Rules: NightTime (+40), HighValue >R$300 (+30), ExcessiveAttempts >3 (+50), ExtremeValue >R$1000 (+60)
- Risk classification (LOW/MEDIUM/HIGH)
- HTTP 403 rejection with risk score detail

### 4. Boleto Payment Engine

- Brazilian barcode validation (mod-10 per field, mod-11 general verifier)
- Support for 47-digit typeable line, 44-digit banking barcode, 48-digit utility boleto
- Asaas BaaS integration for boleto simulation and payment
- Fallback to mock when external API is unavailable

### 5. Credit Card Management

- Virtual card creation with auto-generated numbers
- Card listing, detail view, and soft-delete
- Expiration validation with timezone-aware timestamps
- CVV masking in responses

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
biocodetechpay/
|
+-- app/
|   +-- core/
|   |   +-- config.py               # Centralized configuration (pydantic-settings)
|   |   +-- database.py             # SQLAlchemy connection + session management
|   |   +-- security.py             # JWT, bcrypt, masking, RBAC
|   |   +-- logger.py               # Structured logging + correlation IDs
|   |
|   +-- adapters/
|   |   +-- asaas_adapter.py        # Asaas BaaS integration (PIX, boleto, webhooks)
|   |   +-- gateway_factory.py      # Payment gateway factory
|   |
|   +-- auth/
|   |   +-- models.py               # User model (balance as Numeric 15,2)
|   |   +-- router.py               # Login, register, password reset
|   |
|   +-- parcelamento/               # Installment simulation engine
|   |   +-- models.py               # SQLAlchemy Model
|   |   +-- schemas.py              # Pydantic Validation
|   |   +-- service.py              # Business Logic (Price Table, CET)
|   |   +-- router.py               # FastAPI Endpoints
|   |
|   +-- pix/                        # PIX transaction engine
|   |   +-- models.py               # PixTransaction (Numeric 15,2, payload_hash)
|   |   +-- schemas.py              # Pydantic Validation
|   |   +-- service.py              # credit_pix_receipt, R$50k cap, audit log
|   |   +-- router.py               # Endpoints, antifraud, webhooks, QR code
|   |
|   +-- boleto/                     # Boleto payment engine
|   |   +-- models.py               # BoletoTransaction model
|   |   +-- service.py              # Barcode validation (mod-10/mod-11), Asaas
|   |
|   +-- cards/                      # Credit card management
|   |   +-- models.py               # CreditCard model (timezone-aware)
|   |   +-- schemas.py              # Pydantic Validation
|   |   +-- service.py              # CRUD with expiration checks
|   |
|   +-- antifraude/                 # Anti-fraud engine
|   |   +-- rules.py                # Rule Engine (4 rules, scoring 0-100)
|   |   +-- schemas.py              # Pydantic Validation
|   |   +-- router.py               # FastAPI Endpoints
|   |
|   +-- minha_conta/                # Account management
|   +-- ia/                         # AI features
|   +-- services/                   # Shared services
|   +-- templates/                  # Jinja2 templates (Tailwind + Lucide)
|   +-- static/                     # Static assets
|   +-- web_routes.py               # Public pages, rate limiter, UUID validation
|   +-- main.py                     # FastAPI app + lifespan
|
+-- tests/
|   +-- conftest.py                 # Shared fixtures (SQLite in-memory)
|   +-- test_parcelamento.py        # Installment engine tests
|   +-- test_pix.py                 # PIX core tests
|   +-- test_pix_features_v2.py     # PIX advanced features + antifraud
|   +-- test_pix_charge.py          # PIX charge creation tests
|   +-- test_pix_internal_integration.py  # Internal transfer tests
|   +-- test_qrcode_payment.py      # QR code payment flow tests
|   +-- test_pix_link.py            # Payment link tests
|   +-- test_webhook.py             # Webhook handler tests
|   +-- test_concurrency.py         # Concurrent balance operation tests
|   +-- test_antifraude.py          # Anti-fraud engine tests
|   +-- test_cards.py               # Card CRUD tests
|   +-- test_cards_management.py    # Card management tests
|   +-- test_payment_flows.py       # End-to-end payment flows
|   +-- test_internal_banking.py    # Internal banking tests
|   +-- test_asaas_config.py        # Asaas adapter configuration tests
|   +-- test_asaas_integration.py   # Asaas API integration tests
|   +-- test_verification.py        # User verification tests
|   +-- test_admin_delete_user.py   # Admin user deletion tests
|   +-- test_admin_edit_user.py     # Admin user edit tests
|   +-- test_admin_template_integrity.py  # Template integrity tests
|
+-- scripts/                        # Migration and maintenance scripts
+-- docs/adr/                       # Architecture Decision Records
+-- requirements.txt
+-- Dockerfile
+-- docker-compose.yml
+-- render.yaml                     # Render deployment config
+-- Makefile
+-- pytest.ini
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

**211 tests passing, 0 failures** across 20 test files.

```bash
# Run all tests
pytest

# With coverage
pytest --cov=app --cov-report=html

# Specific module tests
pytest tests/test_parcelamento.py      # Installment engine
pytest tests/test_pix.py               # PIX core
pytest tests/test_webhook.py           # Webhook handlers
pytest tests/test_concurrency.py       # Concurrent operations
pytest tests/test_antifraude.py        # Anti-fraud engine
pytest tests/test_qrcode_payment.py    # QR code flows
pytest tests/test_pix_link.py          # Payment links
pytest tests/test_cards.py             # Card management
```

### Test Coverage by Domain

| Domain           | Tests | Coverage                                                         |
| ---------------- | ----: | ---------------------------------------------------------------- |
| PIX transactions |   60+ | Idempotency, status control, antifraud, webhooks, QR code, links |
| Installments     |     8 | Compound interest, CET, amortization, validation                 |
| Anti-fraud       |   10+ | All 4 rules, scoring, thresholds, risk classification            |
| Cards            |   15+ | CRUD, expiration, masking, virtual cards                         |
| Webhooks         |     9 | Signature validation, idempotency, status mapping, refunds       |
| Payment links    |     5 | Valid rendering, expired, paid, invalid UUID, not found          |
| Concurrency      |     3 | Double debit protection, convergence, unique IDs                 |
| Admin            |   10+ | User CRUD, template integrity, access control                    |
| Integration      |   20+ | Asaas adapter, internal banking, payment flows                   |

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

### Authentication and Authorization

- **JWT** with httpOnly cookie transport, `exp` + `aud` verification, `alg=none` rejection
- **bcrypt** password hashing via passlib
- **RBAC** with `get_current_user` dependency injection
- **CORS** configured with explicit origin allowlist

### Input Validation and Sanitization

- **Pydantic v2** schemas at all API boundaries
- **UUID format validation** on public endpoints (regex check before database query)
- **Barcode validation** with mod-10/mod-11 check digits for Brazilian boletos
- **SQL injection prevention** via SQLAlchemy ORM (no raw SQL)

### Financial Security Controls

- **Timing-safe webhook signature** comparison via `hmac.compare_digest()`
- **Server-side transaction values** -- credit operations use database value, never client-supplied
- **Credit limit cap** at R$50,000 per transaction
- **Withdrawal validation** -- rejects negative/zero values, enforces R$50,000 max, validates operation type
- **Decimal precision** -- all monetary columns use `Numeric(15, 2)`, no floating-point arithmetic
- **Anti-fraud scoring** integrated into transaction and QR code payment flows
- **Idempotency** via `idempotency_key` unique constraint + `payload_hash` deduplication

### Infrastructure Security

- **Rate limiting** -- 30 requests per 60 seconds per IP (sliding window)
- **Cache-Control: no-store** on pages displaying financial data
- **Sandbox/production guard** -- API key prefix detection prevents cross-environment misconfiguration
- **Sensitive data masking** in structured logs (CPF, email, account numbers)
- **Correlation IDs** for end-to-end request tracing
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

### Architecture

- DDD + Hexagonal (domain isolated from frameworks)
- Clear Separation of Concerns with ports and adapters
- Dependency Injection via FastAPI
- Centralized credit operation (`credit_pix_receipt`) with audit trail
- Gateway factory pattern for payment provider abstraction

### Code Quality

- Complete Type Hints with Pydantic v2 models
- 211 automated tests across 20 test files (0 failures)
- Concurrency tests validating balance integrity under parallel access
- Contract tests for webhook handlers (signature, idempotency, status mapping)
- Input Validation at all boundaries

### Financial Integrity

- `Numeric(15, 2)` on all monetary database columns (no floating-point)
- Server-side value enforcement (never trust client-supplied amounts)
- R$50,000 credit cap per transaction
- Antifraud scoring integrated pre-transaction
- Idempotency via unique key + payload hash deduplication
- Timezone-aware timestamps (`datetime.now(timezone.utc)`)

### Security Hardening

- Timing-safe signature comparison (`hmac.compare_digest`)
- IP-based rate limiting (30 req/60s sliding window)
- UUID validation before database queries
- Sandbox/production environment guard
- Withdrawal type and value validation
- Cache-Control headers on sensitive pages

### Observability

- Structured Logging with JSON format
- Correlation IDs on all requests
- Health Checks (Docker + application)
- Processing time headers (`X-Process-Time`)
- PostgreSQL trigram index recommendations for LIKE queries

### DevOps

- Multi-stage Docker build
- Ready-to-use docker-compose
- Render deployment configuration (`render.yaml`)
- Makefile for standardized development tasks
- Migration scripts in `scripts/` directory

---

## Technology Stack

- **Python 3.13** - Runtime
- **FastAPI** - Modern async web framework
- **SQLAlchemy 2.0** - ORM with `Mapped` type annotations and `Numeric(15, 2)` precision
- **Pydantic v2** - Data validation and settings management
- **PostgreSQL** (Neon) - Production database
- **SQLite** - Test database (in-memory + file-based for concurrency)
- **Jinja2** + **Tailwind CSS** + **Lucide Icons** - Server-side rendered UI
- **Python-JOSE** - JWT token handling
- **Passlib** + **argon2/bcrypt** - Password hashing
- **HTTPX** - Async HTTP client for external API calls
- **Pytest** - Testing framework (211 tests)
- **Uvicorn** - ASGI server
- **Docker** + **docker-compose** - Containerization
- **Asaas BaaS** - Payment gateway (PIX, boleto, webhooks)

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

## License

This project was developed for educational and technical demonstration purposes.

---

## Completed Milestones

- [x] PostgreSQL Integration (Neon)
- [x] JWT Authentication with httpOnly cookies
- [x] Rate Limiting (IP-based sliding window)
- [x] Anti-fraud engine integrated into transaction flows
- [x] Decimal precision on all financial columns
- [x] Webhook signature validation (timing-safe)
- [x] Boleto barcode validation (mod-10/mod-11)
- [x] Asaas BaaS integration (PIX + boleto)
- [x] Cloud Deployment (Render)
- [x] 211 automated tests

## Roadmap

- [ ] Redis caching for rate limiter and session store
- [ ] Event-driven architecture (Kafka/RabbitMQ)
- [ ] Prometheus + Grafana observability stack
- [ ] CI/CD with GitHub Actions
- [ ] Alembic database migrations
- [ ] OpenTelemetry distributed tracing
- [ ] Contract testing with Pact

---

BioCodeTechPay inc.
Cezi Cola Senior Software Engineer
