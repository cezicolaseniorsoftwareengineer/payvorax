# Test and Validation Catalog - Fintech Tech Challenge

This document catalogs all automated tests and validation scenarios implemented in the system, demonstrating coverage of functional requirements, business rules, and security.

## Coverage Summary

- **Total Automated Tests**: 24
- **Covered Areas**: Installments (Price), PIX (Idempotency), Anti-Fraud (Scoring)
- **Test Types**: Unit, Integration, Regression

---

## 1. Installment Module (Credit)

Validates mathematical accuracy and credit granting rules.

| Test | Scenario Description | Business Value |
| :--- | :--- | :--- |
| `test_calculo_parcelamento_basico` | **Basic Calculation (Price Table)** | Ensures the fundamental calculation of installments, total paid, and CET is mathematically correct. |
| `test_primeira_parcela_juros` | **Interest Accuracy** | Verifies if the interest of the first installment is calculated exactly on the initial outstanding balance. |
| `test_saldo_final_zero` | **Amortization Integrity** | Ensures that, at the end of the term, the outstanding balance is exactly zero (no residuals). |
| `test_total_pago_maior_que_valor` | **Financial Consistency** | Validates that the total amount paid is always greater than the borrowed amount (operation profit). |
| `test_validacao_valor_negativo` | **Input Protection** | Prevents simulation of negative or null values, protecting the system from invalid data. |
| `test_validacao_taxa_excessiva` | **Rate Compliance** | Blocks abusive interest rates (above 15%), ensuring regulatory compliance. |
| `test_parcelas_decrescentes` | **Balance Consistency** | Verifies if the outstanding balance decreases progressively with each paid installment. |

---

## 2. PIX Module (Payments)

Focuses on transactional security, idempotency, and key validation.

| Test | Scenario Description | Business Value |
| :--- | :--- | :--- |
| `test_criacao_pix_sucesso` | **Happy Path Creation** | Validates the successful creation of a PIX transaction with the correct initial status. |
| `test_idempotencia_pix` | **Idempotency Guarantee** | **Critical**: Ensures that duplicate requests (same `idempotency-key`) do not generate duplicate payments. |
| `test_validacao_cpf` | **CPF Validation** | Prevents the use of CPFs with invalid formats in PIX keys. |
| `test_validacao_email` | **Email Validation** | Ensures email keys follow the standard format (user@domain). |
| `test_validacao_telefone` | **Phone Validation** | Ensures phone numbers have the correct format and length. |
| `test_validacao_valor_negativo` | **Financial Security** | Blocks transfer attempts with negative values. |
| `test_confirmacao_pix` | **Confirmation Flow** | Tests the status transition from CREATED to CONFIRMED. |
| `test_confirmacao_pix_inexistente` | **Error Handling** | Validates system behavior when trying to confirm a non-existent transaction (Error 404). |

---

## 3. Anti-Fraud Module (Security)

Tests the rule engine, score calculation, and automatic decisions.

| Test | Scenario Description | Business Value |
| :--- | :--- | :--- |
| `test_transacao_aprovada_baixo_risco` | **Automatic Approval** | Validates that legitimate transactions (low risk) are approved without friction. |
| `test_transacao_reprovada_alto_risco` | **Fraud Blocking** | Ensures suspicious transactions are automatically blocked. |
| `test_regra_horario_noturno` | **Rule: Risk Hours** | Verifies detection of transactions outside business hours (22h-06h). |
| `test_regra_valor_alto` | **Rule: Average Ticket** | Tests the risk trigger for values above the configured limit (> R$ 300). |
| `test_regra_tentativas_excessivas` | **Rule: Behavior** | Identifies anomalous behavior (many attempts in a short period). |
| `test_score_acumulado` | **Scoring Engine** | Validates the correct sum of risk points from multiple simultaneous rules. |
| `test_nivel_risco_medio` | **Risk Classification** | Tests the correct categorization of transactions into MEDIUM risk. |
| `test_multiplas_regras_ativadas` | **Extreme Scenario** | Simulates a transaction that violates all rules simultaneously (Score 100). |
| `test_validacao_horario_invalido` | **Data Integrity** | Rejects invalid time formats (e.g., "25:00"). |

---

## 4. Integration Scenarios (Manual)

These scenarios are covered by the script `scripts/test_api_manual.py` and validate the end-to-end flow via HTTP.

1. **Health Check**: Verifies if the API is online and responding.
2. **Full Simulation**: Sends a JSON payload to the installment endpoint and validates the full response (CET, Table).
3. **PIX Lifecycle**:
    - Creates a payment intent.
    - Receives the transaction ID.
    - Confirms the transaction using the received ID.
4. **Real-Time Risk Analysis**:
    - Sends "safe" transaction -> Expects approval.
    - Sends "suspicious" transaction -> Expects rejection and list of reasons.

---

## Conclusion for the Recruiter

This catalog demonstrates that the system not only "works" but was built with **Quality and Reliability** as priorities.

- **Security**: Validated by input tests and fraud rules.
- **Financial Reliability**: Validated by Price calculation and Idempotency tests.
- **Maintainability**: Testable, modular, and documented code.
