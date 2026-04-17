Param(
    [string]$Sql = "SELECT id, user_id, status, created_at FROM transactions WHERE status='PROCESSING' ORDER BY created_at DESC LIMIT 100;"
)

if (-not $env:DATABASE_URL) {
    Write-Error "DATABASE_URL not set. Export DATABASE_URL before running this script."
    exit 1
}

Write-Output "Executando consulta para transações PROCESSING..."

# Usa psql; a connection string é passada via DATABASE_URL
psql "$env:DATABASE_URL" -c $Sql

if ($LASTEXITCODE -ne 0) {
    Write-Error "psql retornou código de erro: $LASTEXITCODE"
    exit $LASTEXITCODE
}

Write-Output "Consulta finalizada. Ajuste a query passando -Sql se necessário."
