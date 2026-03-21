# VooBot Monitor / Skyscanner Tracker

Monitor de passagens com Flask + Playwright + SQLite, feito para rodar localmente e subir no Render (ou outro provedor) com cron automático.

## 1. Pré-requisitos locais

1. Crie um virtualenv e ative:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```
2. Instale dependências:
   ```bash
   pip install -r requirements.txt
   python3 -m playwright install chromium
   ```
3. Crie um `.env` (não comite!) com as variáveis críticas:
   ```env
   TELEGRAM_BOT_TOKEN=8651349481:AAHRdUKl7Dx-GJ76Yy_kQiJ4jA6TCaQ8r4g
   TELEGRAM_CHAT_ID=1748352987
   FLASK_DEBUG=1
   SKYSCANNER_DB_PATH=flight_tracker_browser.db
   SKYSCANNER_USER_DATA_DIR=.playwright-profile
   SKYSCANNER_AUTO_SCAN=0
   ```
4. Inicialize o banco e banco cron:
   ```bash
   python3 - <<'PY'
   from skyscanner import Database
   from pathlib import Path
   from os import getenv

   db_path = getenv("SKYSCANNER_DB_PATH", "flight_tracker_browser.db")
   Database(db_path)
   print(f"Banco pronto: {db_path}")
   PY
   ```
5. Inicie o Flask para testes:
   ```bash
   flask --app main run
   ```
   Ajuste `FLASK_DEBUG=0` e exporte `SKYSCANNER_USER_DATA_DIR`/`SKYSCANNER_DB_PATH` antes de subir para o Render ou outro host.

## 2. Deploy no Render (guia completo)

O repositório já traz `render.deploy.sh`, `render.yaml` e `Procfile`. O fluxo básico é:

1. Rodar `./render.deploy.sh` localmente ou no build hook para garantir:
   * diretórios `data/`, perfil do Playwright e `SKYSCANNER_DB_PATH` criados;
   * dependências `pip install -r requirements.txt` + `python3 -m playwright install chromium` instaladas;
   * banco SQLite inicializado e pronto (reexecutar o script sempre que recriar o data volume).
2. Fazer deploy no Render com `render.yaml` (serviço `web` + disco persistente + cron) e `Procfile` com `gunicorn main:app --bind 0.0.0.0:$PORT`.
3. Definir variáveis de ambiente no painel do Render (ou via `render.yaml`):
   ```yaml
   FLASK_DEBUG: "0"
   SKYSCANNER_DB_PATH: /opt/render/project/src/data/flight_tracker_browser.db
   SKYSCANNER_USER_DATA_DIR: /opt/render/project/src/data/playwright-profile
   SKYSCANNER_AUTO_SCAN: "1"
   TELEGRAM_BOT_TOKEN: ...
   TELEGRAM_CHAT_ID: ...
   ```
4. Ajustar o cron `render.yaml` (ou criar job separado) para disparar `/painel/cron` e manter os dados frescos.

## 3. Recomendações adicionais

* Nunca comite o `.env`, o banco ou o perfil do Playwright (já estão no `.gitignore`).
* Para recriar o banco agora que ele foi removido, apenas rode `./render.deploy.sh` ou o bloco de `sqlite3` acima — ele cria a estrutura automaticamente.
* Caso precise de um deploy diferente (Fly.io, Railway), garanta que `SKYSCANNER_DB_PATH` e `SKYSCANNER_USER_DATA_DIR` apontem para um volume persistente disponível naquele ambiente.
* O `render.deploy.sh` serve tanto pra preparar o Render quanto pra gerar o banco e cache local durante testes. Basta rodá-lo sempre que trocar de máquina ou limpar o diretório `data/`.

Quer que eu gere também um `.env.example` com essas variáveis para facilitar o onboarding?