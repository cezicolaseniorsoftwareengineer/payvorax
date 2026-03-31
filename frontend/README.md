Frontend scaffold (static) — deploy target: Netlify
-------------------------------------------------

Objetivo:
- Fornecer um diretório estático `frontend/public/` com os assets atuais de UI
  (copiados de `app/static/` e `app/templates/`) para deploy no Netlify.

Workflow rápido:
1. Rodar locally: python scripts/extract_frontend.py
2. Conferir conteúdo em `frontend/public/` (index.html, sw.js, static/)
3. Commitar `frontend/` e conectar Netlify ao repositório (ou usar CI para extrair antes do deploy).

Notas:
- Este scaffold NÃO converte templates Jinja2 dinâmicos em SPA. É um passo inicial para
  separar assets e permitir um deploy estático imediato no Netlify. Migração para Angular
  é a próxima etapa planejada.

Netlify:
- O arquivo `netlify.toml` está presente e publica `frontend/public/`.
- Para usar um domínio Netlify, defina `FRONTEND_URL` como variável de ambiente no Render
  (backend) apontando para `https://<seu-site>.netlify.app` para habilitar CORS.

Autor: Cezi Cola (scaffold automático)
