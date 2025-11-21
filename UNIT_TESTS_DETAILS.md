# Technical Detail of Unit Tests

This document presents the technical specification of the Fintech project's unit test suite, demonstrating code coverage and isolation strategies used.

## Coverage Metrics (Unit)

Unit tests focus exclusively on **Business Logic (Domain Layer)**, isolating external dependencies like database and HTTP API.

| Module | File | Coverage | Status |
| :--- | :--- | :--- | :--- |
| **Anti-Fraud** | `app/antifraude/rules.py` | **98%** | Excellent |
| **Anti-Fraud** | `app/antifraude/schemas.py` | **97%** | Excellent |
| **PIX** | `app/pix/schemas.py` | **98%** | Excellent |
| **PIX** | `app/pix/models.py` | **96%** | Excellent |
| **PIX** | `app/pix/service.py` | **80%** | Good |
| **Installment** | `app/parcelamento/schemas.py` | **96%** | Excellent |
| **Installment** | `app/parcelamento/models.py` | **94%** | Excellent |
| **Installment** | `app/parcelamento/service.py` | **79%** | Good |

> **Note:** Infrastructure files (`router.py`, `main.py`) are not targets of unit tests, but rather integration tests (not covered in this report).

---

## Test Strategy

We use **Pytest** as the runner and **Unittest.Mock** for isolation.

### 1. Database Isolation

To test services that depend on the database without connecting to a real one, we use `MagicMock`.

**Example (`tests/test_pix.py`):**

```python
# Mock database session
db_mock = MagicMock()
# Simulating no duplicate record exists
db_mock.query().filter().first.return_value = None

pix = create_pix(db_mock, data, ...)
```

### 2. Business Rule Tests (Pure)

We test mathematical and conditional logic without any external dependencies.

**Example (`tests/test_parcelamento.py`):**

```python
# Direct test of calculation function (Input -> Output)
result = calculate_installments(data)
assert result["cet_anual"] > 0
```

---

## Test Case Specification

### Module: Installment (`tests/test_parcelamento.py`)

| Test Case | Input | Expected Behavior |
| :--- | :--- | :--- |
| `test_calculo_parcelamento_basico` | R$ 1000, 12x, 3.5% | Table with 12 rows, Total > 1000, CET calculated. |
| `test_primeira_parcela_juros` | R$ 1000, 3.5% | Interest of 1st installment must be exactly R$ 35.00. |
| `test_saldo_final_zero` | Full simulation | Outstanding balance in the last row must be 0.00. |
| `test_validacao_valor_negativo` | Value: -100.0 | Must raise validation exception. |
| `test_validacao_taxa_excessiva` | Rate: 20% | Must raise exception (Rule: Cap of 15%). |

### Module: PIX (`tests/test_pix.py`)

| Test Case | Input | Expected Behavior |
| :--- | :--- | :--- |
| `test_criacao_pix_sucesso` | Valid data | PIX object created with status `CREATED`. |
| `test_idempotencia_pix` | Same `idempotency-key` | Must return the original PIX object (without creating new). |
| `test_validacao_cpf` | CPF: "123" | Must raise invalid format error. |
| `test_confirmacao_pix` | Existing ID | Status must change to `CONFIRMED`. |
| `test_confirmacao_pix_inexistente` | Non-existent ID | Must return `None` or 404 error. |

### Module: Anti-Fraud (`tests/test_antifraude.py`)

| Test Case | Input | Expected Behavior |
| :--- | :--- | :--- |
| `test_transacao_aprovada` | R$ 50, 14:30h | Score < 60, Approved = True. |
| `test_transacao_reprovada` | R$ 1500, 23:00h | Score >= 60, Approved = False, Risk HIGH. |
| `test_regra_horario_noturno` | Time: 23:30 | Rule must return `True` (Activated). |
| `test_regra_valor_alto` | Value > Limit | Rule must return `True` (Activated). |
| `test_score_acumulado` | Multiple risk factors | Score must be the sum of rule weights. |

---

## How to Run

To reproduce these tests and generate the coverage report:

```bash
# Run tests
python -m pytest -v

# Generate coverage report
python -m pytest --cov=app
```
