# Recuperando artefatos locais

Estes arquivos (SQLite, perfil do Playwright etc.) não entram no Git porque estão listados no `.gitignore`. Para reproduzi-los em outro computador ou após um clone limpo, basta:

1. Criar/ativar um virtualenv: `python3 -m venv .venv && source .venv/bin/activate`
2. Instalar dependências e navegadores:
   ```bash
   pip install -r requirements.txt
   python3 -m playwright install chromium
   ```
3. Rodar o script de preparo `./render.deploy.sh` para:
   * criar `data/`, o banco SQLite e o diretório do Playwright no caminho configurado;
   * definir `SKYSCANNER_DB_PATH` e `SKYSCANNER_USER_DATA_DIR` localmente;
   * inicializar o banco e deixar o cron pronto.
4. Exportar variáveis locais (por exemplo via `.env`) antes de rodar o Flask/Playwright.

O script cuida de tudo que precisa e garante que os arquivos grandes nunca sejam versionados.