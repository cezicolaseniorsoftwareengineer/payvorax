Param()

if (-not $env:DATABASE_URL) {
    Write-Error "DATABASE_URL not set. Export DATABASE_URL before running this script."
    exit 1
}

$timestamp = Get-Date -Format yyyyMMddHHmmss
$filename = "backup_$timestamp.dump"

Write-Output "Fazendo backup do banco para: $filename"

# pg_dump aceita connection string como primeiro argumento
pg_dump --format=custom --file $filename $env:DATABASE_URL

if ($LASTEXITCODE -ne 0) {
    Write-Error "pg_dump retornou código de erro: $LASTEXITCODE"
    exit $LASTEXITCODE
}

Write-Output "Backup concluído: $filename"
