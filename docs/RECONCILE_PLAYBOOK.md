## Playbook de Reconciliação — BioCodeTechPay

Objetivo: fornecer passos seguros e reprodutíveis para reconciliação de transações PROCESSING com a Asaas.

Pré-requisitos:
- Ter acesso ao servidor onde o banco está (credenciais) ou ao painel Asaas.
- Variáveis de ambiente configuradas localmente: DATABASE_URL, ASAAS_API_KEY, ASAAS_WEBHOOK_TOKEN, ASAAS_TOTP_SECRET (opcional).
- Ferramentas: `pg_dump`, `psql`, `python` com virtualenv do projeto.

1) Criar backup do banco (obrigatório antes de aplicar mudanças)

Windows (PowerShell) — exemplo:

```powershell
# Exemplo: certifique-se de ajustar a variável DATABASE_URL no seu ambiente
pg_dump --format=custom --file "backup_$(Get-Date -Format yyyyMMddHHmmss).dump" $env:DATABASE_URL
```

2) Dry-run (listar ações sem aplicar)

```powershell
python scripts/reconcile_processing.py
```

3) Revisar o resultado do dry-run. Checar especialmente:
- Transações com status CONFIRMED -> serão debitadas.
- Transações com status FAILED/NOT_FOUND -> serão revertidas/estornadas.
- Erros de API (404/401) devem ser investigados antes de aplicar.

4) Quando estiver seguro, aplicar reconciliação:

```powershell
python scripts/reconcile_processing.py --apply
```

5) Pós-aplicação — validação básica

- Verificar saldos dos usuários afetados (lista retornada pelo script).
- Buscar entradas no ledger para os `transaction_id` reconciliados.
- Conferir logs do servidor para erros/stacktraces.

6) Reenvio de webhooks no painel Asaas

- Se o PSP suportar reenvio manual, reenvie callbacks para os `transfer_id` ou `payment_id` afetados.
- Se não, contate suporte Asaas com os correlation IDs e timestamps.

7) Mitigações e follow-ups

- Configurar `ASAAS_WEBHOOK_TOKEN` e `ASAAS_API_KEY` em produção.
- Configurar `ASAAS_TOTP_SECRET` ou `ASAAS_OPERATION_KEY` para autorizar transfers automaticamente.
- Implementar monitoramento: alertas quando existirem ledger entries PENDING > X minutos.
- Automatizar reconciliação periódica (ex.: Task Scheduler / cron com janela segura).

Observações de segurança e conformidade:
- Não exponha chaves em logs. Use placeholders quando registrar.
- Faça rollback do backup se detectar inconsistências críticas.

Contato de emergência: mantenha contato com a equipe financeira/ops antes de aplicar mudanças que alterem saldos.
