# Deploy no Render (plano gratuito)

Este projeto já inclui os artefatos principais para rodar no Render:

* `Procfile` — inicia o Flask via `gunicorn main:app --bind 0.0.0.0:$PORT`.
* `render.yaml` — define o serviço web gratuito, monta um disco persistente de 1 GB e cria um cron job de exemplo.
* `main.py` agora respeita `SKYSCANNER_DB_PATH`, para você apontar o SQLite direto pro volume renderizado.

## Checklist rápido

1. **Repo pronto**
   * Garanta `requirements.txt` atualizado e commitado.
   * O banco default (`flight_tracker_browser.db`) pode ficar em `skyscanner-config.json`, mas o app vai usar o caminho definido em `SKYSCANNER_DB_PATH`.

2. **Crie o serviço no Render**
   * Conecte o GitHub/GitLab e escolha a branch principal (`main`).
   * Runtime: `python` com `PYTHON_VERSION=3.12.8`, Build Command: `PLAYWRIGHT_BROWSERS_PATH=/opt/render/project/src/.playwright-browsers pip install -r requirements.txt && PLAYWRIGHT_BROWSERS_PATH=/opt/render/project/src/.playwright-browsers python3 -m playwright install chromium`, Start Command: `gunicorn main:app --bind 0.0.0.0:$PORT --timeout 180`.
   * Buckets e datas persistem em disco montado: mantenha o diretório `data/` dentro do repo (ou use `render.yaml` para montar `disk` em `/opt/render/project/src/data`).

3. **Variáveis de ambiente essenciais**
   * `PYTHON_VERSION=3.12.8`
   * `PLAYWRIGHT_BROWSERS_PATH=/opt/render/project/src/.playwright-browsers`
   * `FLASK_DEBUG=0`
   * `SKYSCANNER_DB_PATH=/opt/render/project/src/data/flight_tracker_browser.db`
   * `SKYSCANNER_USER_DATA_DIR=/opt/render/project/src/data/playwright-profile`
   * `SKYSCANNER_AUTO_SCAN=1` (ou `0` se quiser rodar só via cron externo)
   * `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID` (opcionalmente também no painel de usuários)
   * `SKYSCANNER_FULL_SCAN_EVERY_SECONDS` e `SKYSCANNER_USER_SCAN_POLL_SECONDS` podem ser ajustados conforme a frequência desejada.

4. **Crie o cron job** (Render faz isso direta ou via `render.yaml`)
   * Exemplo: `curl --fail https://<seu-app>.onrender.com/painel/cron`
   * Ajuste o agendamento (`*/30 * * * *` foi usado no arquivo) e substitua `<seu-app>` pela URL real. Você também pode expor uma rota interna (`/painel/cron`) protegida e chamar via `render.yaml` ou um job separado.

5. **Persistência de dados**
   * Render monta o disco só na pasta indicada. O `data` usado aqui garante que Playwright (perfil) e SQLite permaneçam entre deploys.
   * Caso precise de backup, copie `/opt/render/project/src/data/flight_tracker_browser.db` para fora periodicamente.

## Erro comum: `pkg_resources` no Gunicorn

Se o log mostrar algo como `ModuleNotFoundError: No module named 'pkg_resources'` com caminho em `python3.14`, normalmente o deploy subiu com um runtime novo demais para a combinação atual de dependências. Neste projeto, a correção é:

* usar `gunicorn main:app --bind 0.0.0.0:$PORT`;
* fixar `PYTHON_VERSION=3.12.8`;
* redeployar para reinstalar as dependências nesse runtime.

## Erro comum: Playwright sem Chromium

Se o log mostrar `Executable doesn't exist` para `chrome-headless-shell`, o pacote Python do Playwright foi instalado, mas os browsers não. Neste projeto, a correção é:

* definir `PLAYWRIGHT_BROWSERS_PATH=/opt/render/project/src/.playwright-browsers`;
* instalar no build com `python3 -m playwright install chromium`;
* fazer novo deploy para reconstruir a imagem do serviço.

## Erro comum: `WORKER TIMEOUT`

Se o log mostrar `WORKER TIMEOUT` em `/consulta`, o Gunicorn matou o worker antes do Playwright terminar. Neste projeto, a correção é:

* usar `gunicorn main:app --bind 0.0.0.0:$PORT --timeout 180`;
* redeployar o serviço com esse Start Command;
* considerar reduzir `settle_seconds` no futuro se quiser consultas mais curtas.

## Extras

* Se quiser acessar o painel, a URL padrão é `https://<seu-app>.onrender.com/painel`. Configure usuários/rotas lá.
* A fila interna de rota (scheduler) roda em threads, então o app deve ficar ligado o tempo todo — o plano gratuito do Render atende isso (não entra em sleep).

Se quiser, posso preparar também um script `render.deploy.sh` ou te ajudar a configurar Secrets e Branch Protection. Quer que eu faça isso agora?
