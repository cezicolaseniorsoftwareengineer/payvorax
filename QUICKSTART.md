# PayvoraX - Quickstart Guide

## Start the Server

### Option 1: Direct Command

```bash
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### Option 2: Python Script

```bash
python start.py
```

## Access Documentation

- **Swagger UI (Interactive)**: <http://localhost:8000/docs>
- **ReDoc (Documentation)**: <http://localhost:8000/redoc>
- **Health Check**: <http://localhost:8000/health>

## Test Endpoints

### Via Test Script

```bash
pip install requests
python test_api.py
```

### Via Swagger UI

1. Open <http://localhost:8000/docs>
2. Click on the desired endpoint
3. Click "Try it out"
4. Fill in the data
5. Execute

## Request Examples

### 1. Installments (Parcelamento)

```bash
curl -X POST "http://localhost:8000/parcelamento/simular" \
  -H "Content-Type: application/json" \
  -d '{
    "valor": 1000,
    "parcelas": 12,
    "taxa_mensal": 0.035
  }'
```

### 2. PIX

```bash
curl -X POST "http://localhost:8000/pix/create" \
  -H "Content-Type: application/json" \
  -H "idempotency-key: unique-key-123" \
  -d '{
    "valor": 150.50,
    "chave_pix": "test@email.com",
    "tipo_chave": "EMAIL",
    "descricao": "Test payment"
  }'
```

### 3. Anti-Fraud

```bash
curl -X POST "http://localhost:8000/antifraude/analisar" \
  -H "Content-Type: application/json" \
  -d '{
    "valor": 500,
    "horario": "23:30",
    "tentativas_ultimas_24h": 5
  }'
```

## Docker

```bash
# Build
docker-compose up --build

# Run
docker-compose up

# Stop
docker-compose down
```

## Unit Tests

```bash
pytest

# With coverage
pytest --cov=app --cov-report=html
```

## Project Structure

```text
app/
├── core/           # Configuration, DB, security, logs
├── parcelamento/   # Challenge 1
├── pix/            # Challenge 2
├── antifraude/     # Challenge 3
└── main.py         # FastAPI app
```

## Next Steps

1. Start server
2. Access <http://localhost:8000/docs>
3. Test endpoints interactively
4. Review structured logs in terminal
5. Run tests: `pytest`

---

### Ready to present at Fintech Mais Todos
