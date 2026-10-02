function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function csrfToken() {
  const entry = document.cookie
    .split(';')
    .map((value) => value.trim())
    .find((value) => value.startsWith('csrftoken='));
  return entry ? decodeURIComponent(entry.slice('csrftoken='.length)) : '';
}

function statusLabel(status) {
  const labels = {
    pending: 'Очікує',
    printing: 'Друкується',
    completed: 'Завершено',
    failed: 'Помилка',
    cancelled: 'Скасовано',
    skipped: 'Пропущено'
  };
  return labels[status] || status || '—';
}

function formatMinutes(value) {
  const minutes = Number(value);
  if (!Number.isFinite(minutes)) return '—';
  if (minutes < 60) return `${Math.max(0, Math.round(minutes))} хв`;
  const hours = Math.floor(minutes / 60);
  return `${hours} год ${Math.round(minutes % 60)} хв`;
}

function renderQueueRows(items) {
  if (!items?.length) {
    return '<div class="bb-empty">Черга ще не створена.</div>';
  }
  return items.map((item) => {
    const progress = item.progress == null ? null : Math.max(0, Math.min(100, Number(item.progress)));
    const details = item.status === 'printing'
      ? `${progress == null ? '—' : `${progress.toFixed(0)}%`} · залишилось ${formatMinutes(item.remaining_time)}`
      : (item.error || '');
    return `
      <div class="bb-run">
        <div class="bb-run-main">
          <strong>Запуск ${escapeHtml(item.run_number)}</strong>
          <span>${escapeHtml(item.pieces)} шт.</span>
          <span>${escapeHtml(item.printer_name || 'Без принтера')}</span>
          <span class="bb-status bb-${escapeHtml(item.status)}">${escapeHtml(statusLabel(item.status))}</span>
        </div>
        ${progress == null ? '' : `<div class="bb-mini"><i style="width:${progress}%"></i></div>`}
        ${details ? `<div class="bb-details">${escapeHtml(details)}</div>` : ''}
      </div>`;
  }).join('');
}

function printerOptions(printers) {
  return [
    '<option value="">Без призначення — керувати в Bambuddy</option>',
    ...(printers || []).map((printer) => (
      `<option value="${escapeHtml(printer.id)}">${escapeHtml(printer.name)}${printer.model ? ` · ${escapeHtml(printer.model)}` : ''}</option>`
    ))
  ].join('');
}

function detailMessage(payload, fallback) {
  if (typeof payload?.detail === 'string') return payload.detail;
  if (Array.isArray(payload?.detail)) {
    return payload.detail.map((item) => item.msg || JSON.stringify(item)).join('; ');
  }
  return fallback;
}

export function renderBuildOrderPanel(target, data) {
  if (!target) return;
  const context = data?.serverContext || data?.context || {};
  let lastState = null;
  let loading = false;
  let stopped = false;

  target.innerHTML = `
    <style>
      .bb-wrap{font-family:inherit;padding:.5rem 0}.bb-head{display:flex;justify-content:space-between;gap:1rem;align-items:center;flex-wrap:wrap}
      .bb-progress{height:12px;background:var(--mantine-color-dark-5,#343a40);border-radius:8px;overflow:hidden;margin:.8rem 0}
      .bb-progress i,.bb-mini i{display:block;height:100%;background:#00ae42;transition:width .3s}.bb-grid{display:grid;grid-template-columns:repeat(5,minmax(90px,1fr));gap:.6rem;margin:.7rem 0}
      .bb-card{border:1px solid var(--mantine-color-default-border,#dee2e6);border-radius:7px;padding:.6rem}.bb-card b{font-size:1.15rem;display:block}.bb-muted,.bb-details,.bb-empty{opacity:.72;font-size:.85rem}
      .bb-form{display:flex;gap:.6rem;align-items:end;flex-wrap:wrap;border-top:1px solid var(--mantine-color-default-border,#dee2e6);padding-top:.8rem;margin-top:.8rem}.bb-field{display:flex;flex-direction:column;gap:.25rem}.bb-field input,.bb-field select{min-height:34px;padding:.3rem .45rem;border:1px solid #868e96;border-radius:5px;background:transparent;color:inherit}.bb-field select option{color:#111}
      .bb-button{min-height:34px;padding:.35rem .8rem;border:0;border-radius:5px;background:#00ae42;color:white;font-weight:600;cursor:pointer}.bb-button:disabled{opacity:.5;cursor:not-allowed}.bb-error{color:#fa5252;margin:.5rem 0}.bb-runs{display:grid;gap:.45rem;margin-top:.8rem}.bb-run{border:1px solid var(--mantine-color-default-border,#dee2e6);border-radius:6px;padding:.5rem}.bb-run-main{display:grid;grid-template-columns:110px 70px minmax(100px,1fr) 110px;gap:.5rem;align-items:center}.bb-status{font-weight:600}.bb-printing{color:#228be6}.bb-completed{color:#00ae42}.bb-failed{color:#fa5252}.bb-mini{height:5px;background:#495057;border-radius:5px;overflow:hidden;margin-top:.4rem}
      @media(max-width:800px){.bb-grid{grid-template-columns:repeat(2,1fr)}.bb-run-main{grid-template-columns:1fr 1fr}}
    </style>
    <div class="bb-wrap"><div class="bb-muted">Завантаження стану Bambuddy…</div></div>`;

  async function load() {
    if (loading || stopped || !target.isConnected) return;
    loading = true;
    try {
      const response = await fetch(context.statusUrl, {credentials: 'same-origin'});
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(detailMessage(payload, `HTTP ${response.status}`));
      lastState = payload;
      draw();
    } catch (error) {
      target.querySelector('.bb-wrap').innerHTML = `<div class="bb-error">${escapeHtml(error.message)}</div>`;
    } finally {
      loading = false;
    }
  }

  function draw(message = '') {
    const state = lastState || {};
    const progress = state.progress || {};
    const percent = Number(progress.percent || 0);
    const build = state.build || {};
    const batch = state.batch || {};
    const configured = Boolean(state.configured);
    const canQueue = Boolean(context.canQueue) && !configured && [10, 20].includes(Number(build.status));
    const openLink = context.bambuddyUrl
      ? `<a href="${escapeHtml(context.bambuddyUrl)}" target="_blank" rel="noopener">Відкрити Bambuddy</a>`
      : '';

    target.querySelector('.bb-wrap').innerHTML = `
      <div class="bb-head">
        <div><strong>${escapeHtml(build.reference || `Build ${context.buildId}`)}</strong> · ${escapeHtml(build.part_name || '')}<div class="bb-muted">${escapeHtml(build.status_text || '')}${batch.name ? ` · Batch ${escapeHtml(batch.id)}` : ''}</div></div>
        ${openLink}
      </div>
      ${message ? `<div class="bb-error">${escapeHtml(message)}</div>` : ''}
      ${configured ? `
        <div class="bb-progress"><i style="width:${Math.max(0, Math.min(100, percent))}%"></i></div>
        <div class="bb-grid">
          <div class="bb-card"><span class="bb-muted">Прогрес</span><b>${escapeHtml(percent.toFixed(0))}%</b></div>
          <div class="bb-card"><span class="bb-muted">Готово деталей</span><b>${escapeHtml(progress.completed_pieces || 0)} / ${escapeHtml(progress.planned_pieces || 0)}</b></div>
          <div class="bb-card"><span class="bb-muted">У черзі</span><b>${escapeHtml(progress.pending_runs || 0)}</b></div>
          <div class="bb-card"><span class="bb-muted">Друкується</span><b>${escapeHtml(progress.printing_runs || 0)}</b></div>
          <div class="bb-card"><span class="bb-muted">Помилки</span><b>${escapeHtml(progress.failed_runs || 0)}</b></div>
        </div>
        <div class="bb-runs">${renderQueueRows(state.queue_items)}</div>
      ` : `
        <div class="bb-empty">${state.has_3mf ? 'Build Order готовий до передачі в чергу.' : 'У Part немає вкладення .3mf.'}</div>
        ${canQueue ? `
          <form class="bb-form" id="bb-queue-form">
            <label class="bb-field"><span>Деталей на платформі</span><input name="units" type="number" min="1" max="999" value="1" required></label>
            <label class="bb-field"><span>Номер платформи</span><input name="plate" type="number" min="0" placeholder="авто"></label>
            <label class="bb-field"><span>Принтер</span><select name="printer">${printerOptions(state.printers)}</select></label>
            <label class="bb-field"><span><input name="overproduction" type="checkbox"> дозволити перевиробництво</span></label>
            <button class="bb-button" type="submit" ${state.has_3mf ? '' : 'disabled'}>Передати в Bambuddy</button>
          </form>` : ''}
      `}`;

    const form = target.querySelector('#bb-queue-form');
    if (form) form.addEventListener('submit', submitQueue);
  }

  async function submitQueue(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector('button');
    button.disabled = true;
    const values = new FormData(form);
    const plate = values.get('plate');
    const printer = values.get('printer');
    const payload = {
      units_per_run: Number(values.get('units')),
      plate_id: plate === '' ? null : Number(plate),
      printer_id: printer === '' ? null : Number(printer),
      allow_overproduction: values.get('overproduction') === 'on'
    };
    try {
      const response = await fetch(context.queueUrl, {
        method: 'POST',
        credentials: 'same-origin',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrfToken()},
        body: JSON.stringify(payload)
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(detailMessage(result, `HTTP ${response.status}`));
      lastState = result;
      draw();
    } catch (error) {
      button.disabled = false;
      draw(error.message);
    }
  }

  load();
  const timer = window.setInterval(load, 5000);
  const observer = new MutationObserver(() => {
    if (!target.isConnected) {
      stopped = true;
      window.clearInterval(timer);
      observer.disconnect();
    }
  });
  observer.observe(document.body, {childList: true, subtree: true});
}
