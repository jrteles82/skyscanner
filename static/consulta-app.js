
      function pretty(data) {
        return JSON.stringify(data, null, 2);
      }

      function badgeClass(band) {
        const map = {
          excelente: 'text-bg-success',
          bom: 'text-bg-primary',
          normal: 'text-bg-secondary',
          caro: 'text-bg-danger',
          novo: 'text-bg-info',
          sem_preco: 'text-bg-dark',
        };
        return map[band] || 'text-bg-secondary';
      }

      function safe(v, fallback = '-') {
        return v === null || v === undefined || v === '' ? fallback : v;
      }

      function formatDateTimePtBr(iso) {
        if (!iso) return '-';
        const d = new Date(iso);
        if (Number.isNaN(d.getTime())) return iso;
        return d.toLocaleString('pt-BR', {
          timeZone: 'America/Porto_Velho',
          year: 'numeric',
          month: '2-digit',
          day: '2-digit',
          hour: '2-digit',
          minute: '2-digit',
          second: '2-digit',
        });
      }

      function parsePriceToNumber(text) {
        if (!text) return null;
        const clean = String(text).replace(/\s/g, '').replace('R$', '').replace(/\./g, '').replace(',', '.');
        const n = Number(clean);
        return Number.isFinite(n) ? n : null;
      }

      function formatPrice(value) {
        if (value === null || value === undefined || value === '') return '-';
        const n = Number(value);
        if (!Number.isFinite(n)) return String(value);
        return `R$ ${n.toFixed(2).replace('.', ',')}`;
      }

      function trajetoFromDestination(destination) {
        return String(destination || '').trim().toUpperCase() === 'PVH'
          ? 'Volta → PVH'
          : 'Ida saindo de PVH';
      }

      function sortByPriceAsc(a, b) {
        const pa = a.priceNumber ?? Number.POSITIVE_INFINITY;
        const pb = b.priceNumber ?? Number.POSITIVE_INFINITY;
        return pa - pb;
      }

      function highlightBestRows(tbodyId, priceColIndex) {
        const body = document.getElementById(tbodyId);
        const rows = Array.from(body.querySelectorAll('tr'));

        let min = null;
        rows.forEach((row) => {
          row.classList.remove('table-success', 'table-warning', 'fw-semibold');
          const cell = row.children[priceColIndex];
          const value = cell ? parsePriceToNumber(cell.textContent) : null;
          if (value !== null) {
            if (min === null || value < min) min = value;
          }
        });

        if (min === null) return;

        rows.forEach((row) => {
          const cell = row.children[priceColIndex];
          const value = cell ? parsePriceToNumber(cell.textContent) : null;
          if (value !== null && value === min) {
            row.classList.add('table-success', 'fw-semibold');
          }
        });
      }

      function highlightBestReturnToPVH(tbodyId, destinationColIndex, priceColIndex) {
        const body = document.getElementById(tbodyId);
        const rows = Array.from(body.querySelectorAll('tr'));

        let minReturn = null;
        rows.forEach((row) => {
          const destCell = row.children[destinationColIndex];
          const priceCell = row.children[priceColIndex];
          const dest = (destCell?.textContent || '').trim().toUpperCase();
          const value = priceCell ? parsePriceToNumber(priceCell.textContent) : null;

          if (dest === 'PVH' && value !== null) {
            if (minReturn === null || value < minReturn) minReturn = value;
          }
        });

        if (minReturn === null) return;

        rows.forEach((row) => {
          const destCell = row.children[destinationColIndex];
          const priceCell = row.children[priceColIndex];
          const dest = (destCell?.textContent || '').trim().toUpperCase();
          const value = priceCell ? parsePriceToNumber(priceCell.textContent) : null;

          if (dest === 'PVH' && value !== null && value === minReturn) {
            row.classList.add('table-warning', 'fw-semibold');
          }
        });
      }

      function highlightBestReturnToPVHFromRoute(tbodyId, routeColIndex, priceColIndex) {
        const body = document.getElementById(tbodyId);
        const rows = Array.from(body.querySelectorAll('tr'));

        let minReturn = null;
        rows.forEach((row) => {
          const routeText = (row.children[routeColIndex]?.textContent || '').toUpperCase();
          const parts = routeText.split('→').map((x) => x.trim());
          const dest = parts.length > 1 ? parts[1] : '';
          const value = parsePriceToNumber(row.children[priceColIndex]?.textContent || '');
          if (dest === 'PVH' && value !== null) {
            if (minReturn === null || value < minReturn) minReturn = value;
          }
        });

        if (minReturn === null) return;

        rows.forEach((row) => {
          const routeText = (row.children[routeColIndex]?.textContent || '').toUpperCase();
          const parts = routeText.split('→').map((x) => x.trim());
          const dest = parts.length > 1 ? parts[1] : '';
          const value = parsePriceToNumber(row.children[priceColIndex]?.textContent || '');
          if (dest === 'PVH' && value !== null && value === minReturn) {
            row.classList.add('table-warning', 'fw-semibold');
          }
        });
      }

      function enableColumnSorting(tableId) {
        const table = document.getElementById(tableId);
        if (!table) return;
        const headers = table.querySelectorAll('thead th');
        headers.forEach((th, idx) => {
          th.style.cursor = 'pointer';
          th.title = 'Ordenar';
          th.addEventListener('click', () => {
            const tbody = table.querySelector('tbody');
            const rows = Array.from(tbody.querySelectorAll('tr')).filter((r) => r.children.length === headers.length);
            const asc = th.dataset.sortDir !== 'asc';
            headers.forEach((h) => delete h.dataset.sortDir);
            th.dataset.sortDir = asc ? 'asc' : 'desc';
            rows.sort((a, b) => {
              const avRaw = (a.children[idx]?.textContent || '').trim();
              const bvRaw = (b.children[idx]?.textContent || '').trim();
              const avNum = parsePriceToNumber(avRaw);
              const bvNum = parsePriceToNumber(bvRaw);
              let cmp = 0;
              if (avNum !== null && bvNum !== null) cmp = avNum - bvNum;
              else cmp = avRaw.localeCompare(bvRaw, 'pt-BR');
              return asc ? cmp : -cmp;
            });
            tbody.innerHTML = '';
            rows.forEach((r) => tbody.appendChild(r));
          });
        });
      }

      async function consultar() {
        const btn = document.getElementById('btn-consultar');
        const body = document.getElementById('consulta-body');

        btn.disabled = true;
        body.innerHTML = '<tr><td colspan="7" class="text-center text-muted">Consultando...</td></tr>';

        try {
          const params = new URLSearchParams({
            origin: document.getElementById('origin').value,
            destination: document.getElementById('destination').value,
            outbound_date: document.getElementById('outbound_date').value,
            inbound_date: document.getElementById('inbound_date').value,
            fonte: document.getElementById('fonte').value,
          });

          const res = await fetch(`/consulta?${params.toString()}`);
          const data = await res.json();

          if (data.error) {
            body.innerHTML = `<tr><td colspan="7" class="text-center text-danger">${data.error}</td></tr>`;
          } else {
            const rota = data.rota || {};
            const r = data.resultado || {};
            const preco = r.price_fmt || 'Sem preço';
            const dataVoo = safe(rota.inbound_date) ? `${safe(rota.outbound_date)} / ${safe(rota.inbound_date)}` : `${safe(rota.outbound_date)}`;

            const melhorCompra = (r.best_vendor && String(r.best_vendor).trim())
              ? `${safe(r.best_vendor)} (${safe(formatPrice(r.best_vendor_price), '-')})`
              : '-';

            body.innerHTML = `
              <tr>
                <td>${safe(rota.origin)} → ${safe(rota.destination)}</td>
                <td>${dataVoo}</td>
                <td>${preco}</td>
                <td>${melhorCompra}</td>
                <td>${safe(r.site)}</td>
                <td>${safe(r.final_price_source)}</td>
                <td>${formatDateTimePtBr(new Date().toISOString())}</td>
              </tr>
            `;
            highlightBestRows('consulta-body', 2);
          }

          historico();
        } catch (e) {
          body.innerHTML = '<tr><td colspan="7" class="text-center text-danger">Erro ao consultar.</td></tr>';
        } finally {
          btn.disabled = false;
        }
      }

      async function rotas() {
        const body = document.getElementById('rotas-body');
        const loading = document.getElementById('rotas-loading');
        body.innerHTML = '<tr><td colspan="5" class="text-center text-muted">Buscando rotas...</td></tr>';

        try {
          const res = await fetch('/rotas');
          const data = await res.json();

          if (!data.rotas || !data.rotas.length) {
            body.innerHTML = '<tr><td colspan="5" class="text-center text-muted">Sem rotas configuradas.</td></tr>';
            return;
          }

          body.innerHTML = '';
          for (const item of data.rotas) {
            const tipo = item.trip_type === 'roundtrip' ? 'Ida e volta' : 'Somente ida';
            const tr = document.createElement('tr');
            tr.innerHTML = `
              <td>${safe(item.origin)}</td>
              <td>${safe(item.destination)}</td>
              <td>${safe(item.outbound_date)}</td>
              <td>${safe(item.inbound_date, '-')}</td>
              <td>${tipo}</td>
            `;
            body.appendChild(tr);
            await new Promise((resolve) => setTimeout(resolve, 50));
          }
        } catch (e) {
          body.innerHTML = '<tr><td colspan="5" class="text-center text-danger">Erro ao carregar rotas.</td></tr>';
        } finally {
        }
      }

      function executarCron() {
        const btn = document.getElementById('btn-cron');
        const loading = document.getElementById('cron-loading');
        const body = document.getElementById('cron-body');

        btn.disabled = true;
        loading.style.display = 'block';
        loading.textContent = 'Iniciando busca...';
        body.innerHTML = '<tr><td colspan="7" class="text-center text-muted">Iniciando busca...</td></tr>';

        let started = false;
        const items = [];
        const es = new EventSource('/cron-stream');

        const renderCronTable = () => {
          const idas = items.filter((x) => String(x.destination).toUpperCase() !== 'PVH').sort(sortByPriceAsc);
          const voltas = items.filter((x) => String(x.destination).toUpperCase() === 'PVH').sort(sortByPriceAsc);

          const rows = [];
          if (idas.length) rows.push('<tr class="table-secondary"><td colspan="7"><strong>IDAS</strong> (menor → maior preço)</td></tr>');
          for (const item of idas) {
            const melhorCompra = (item.best_vendor && String(item.best_vendor).trim())
              ? `${safe(item.best_vendor)} (${safe(formatPrice(item.best_vendor_price), '-')})`
              : '-';
            rows.push(`
<tr>
<td>${safe(item.origin)} → ${safe(item.destination)}</td>
<td>${safe(item.inbound_date) ? `${safe(item.outbound_date)} / ${safe(item.inbound_date)}` : `${safe(item.outbound_date)}`}</td>
<td>${safe(item.price_fmt, 'Sem preço')}</td>
<td>${melhorCompra}</td>
<td>${safe(item.site)}</td>
<td>${safe(item.final_price_source)}</td>
<td>${formatDateTimePtBr(new Date().toISOString())}</td>
</tr>`);
          }

          if (voltas.length) rows.push('<tr class="table-secondary"><td colspan="7"><strong>VOLTAS PARA PVH</strong> (menor → maior preço)</td></tr>');
          for (const item of voltas) {
            const melhorCompra = (item.best_vendor && String(item.best_vendor).trim())
              ? `${safe(item.best_vendor)} (${safe(formatPrice(item.best_vendor_price), '-')})`
              : '-';
            rows.push(`
<tr>
<td>${safe(item.origin)} → ${safe(item.destination)}</td>
<td>${safe(item.inbound_date) ? `${safe(item.outbound_date)} / ${safe(item.inbound_date)}` : `${safe(item.outbound_date)}`}</td>
<td>${safe(item.price_fmt, 'Sem preço')}</td>
<td>${melhorCompra}</td>
<td>${safe(item.site)}</td>
<td>${safe(item.final_price_source)}</td>
<td>${formatDateTimePtBr(new Date().toISOString())}</td>
</tr>`);
          }

          body.innerHTML = rows.length ? rows.join('') : '<tr><td colspan="7" class="text-center text-muted">Sem resultados nesta execução.</td></tr>';
          highlightBestRows('cron-body', 2);
          highlightBestReturnToPVHFromRoute('cron-body', 0, 2);
        };

        es.onmessage = (evt) => {
          try {
            const msg = JSON.parse(evt.data);

            if (msg.type === 'start') {
              loading.textContent = `Buscando rotas... 0/${msg.total}`;
              body.innerHTML = '';
              return;
            }

            if (msg.type === 'row') {
              started = true;
              const item = msg.item || {};
              item.priceNumber = parsePriceToNumber(item.price_fmt || item.price);
              items.push(item);
              renderCronTable();
              loading.textContent = `Buscando rotas... ${msg.index}/${msg.total}`;
              return;
            }

            if (msg.type === 'done') {
              es.close();
              btn.disabled = false;
              loading.style.display = 'none';
              loading.textContent = 'Buscando rotas... isso pode levar alguns minutos.';
              if (!started) {
                body.innerHTML = '<tr><td colspan="6" class="text-center text-muted">Sem resultados nesta execução.</td></tr>';
              }
              historico();
              return;
            }

            if (msg.type === 'error') {
              es.close();
              btn.disabled = false;
              loading.style.display = 'none';
              loading.textContent = 'Buscando rotas... isso pode levar alguns minutos.';
              body.innerHTML = `<tr><td colspan="7" class="text-center text-danger">${safe(msg.message, 'Erro ao executar busca completa.')}</td></tr>`;
              return;
            }
          } catch (_e) {}
        };

        es.onerror = () => {
          es.close();
          btn.disabled = false;
          loading.style.display = 'none';
          loading.textContent = 'Buscando rotas... isso pode levar alguns minutos.';
          if (!started) {
            body.innerHTML = '<tr><td colspan="7" class="text-center text-danger">Erro ao executar busca completa.</td></tr>';
          }
        };
      }

      async function historico() {
        const loading = document.getElementById('historico-loading');
        const body = document.getElementById('historico-body');
        body.innerHTML = '<tr><td colspan="7" class="text-center text-muted">Carregando histórico...</td></tr>';

        try {
          const limit = Math.max(1, Math.min(200, Number(document.getElementById('historico-limit').value || 20)));
          const res = await fetch(`/historico?limit=${limit}`);
          const data = await res.json();

          if (!data.items || !data.items.length) {
            body.innerHTML = '<tr><td colspan="7" class="text-center text-muted">Sem dados no histórico.</td></tr>';
            return;
          }

          const mapped = data.items.map((item) => ({
            ...item,
            trajeto: trajetoFromDestination(item.destination),
            priceNumber: item.price !== null && item.price !== undefined ? Number(item.price) : null,
          }));

          const idas = mapped.filter((x) => String(x.destination).toUpperCase() !== 'PVH').sort(sortByPriceAsc);
          const voltas = mapped.filter((x) => String(x.destination).toUpperCase() === 'PVH').sort(sortByPriceAsc);

          const rows = [];
          if (idas.length) rows.push('<tr class="table-secondary"><td colspan="7"><strong>IDAS</strong> (menor → maior preço)</td></tr>');
          for (const item of idas) {
            const rota = `${safe(item.origin)} → ${safe(item.destination)}`;
            const dataVoo = item.inbound_date ? `${safe(item.outbound_date)} / ${safe(item.inbound_date)}` : safe(item.outbound_date);
            const preco = item.price !== null && item.price !== undefined ? `R$ ${Number(item.price).toFixed(2).replace('.', ',')}` : 'Sem preço';
            const melhorCompra = (item.best_vendor && String(item.best_vendor).trim())
              ? `${safe(item.best_vendor)} (${safe(formatPrice(item.best_vendor_price), '-')})`
              : '-';
            rows.push(`
<tr>
<td>${rota}</td>
<td>${dataVoo}</td>
<td>${preco}</td>
<td>${melhorCompra}</td>
<td>${safe(item.site)}</td>
<td>${safe(item.final_price_source)}</td>
<td>${formatDateTimePtBr(item.created_at)}</td>
</tr>`);
          }

          if (voltas.length) rows.push('<tr class="table-secondary"><td colspan="7"><strong>VOLTAS PARA PVH</strong> (menor → maior preço)</td></tr>');
          for (const item of voltas) {
            const rota = `${safe(item.origin)} → ${safe(item.destination)}`;
            const dataVoo = item.inbound_date ? `${safe(item.outbound_date)} / ${safe(item.inbound_date)}` : safe(item.outbound_date);
            const preco = item.price !== null && item.price !== undefined ? `R$ ${Number(item.price).toFixed(2).replace('.', ',')}` : 'Sem preço';
            const melhorCompra = (item.best_vendor && String(item.best_vendor).trim())
              ? `${safe(item.best_vendor)} (${safe(formatPrice(item.best_vendor_price), '-')})`
              : '-';
            rows.push(`
<tr>
<td>${rota}</td>
<td>${dataVoo}</td>
<td>${preco}</td>
<td>${melhorCompra}</td>
<td>${safe(item.site)}</td>
<td>${safe(item.final_price_source)}</td>
<td>${formatDateTimePtBr(item.created_at)}</td>
</tr>`);
          }

          body.innerHTML = rows.join('');
          highlightBestRows('historico-body', 2);
          highlightBestReturnToPVHFromRoute('historico-body', 0, 2);
        } catch (e) {
          body.innerHTML = '<tr><td colspan="7" class="text-center text-danger">Erro ao carregar histórico.</td></tr>';
        } finally {
        }
      }

      // carrega automático ao abrir
      enableColumnSorting('consulta-table');
      enableColumnSorting('cron-table');
      enableColumnSorting('historico-table');
      enableColumnSorting('rotas-table');
      rotas();
      historico();
    