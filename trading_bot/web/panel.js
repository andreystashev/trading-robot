function strategyLabel(kind) { return ({ma:'Пересечение средних', breakout:'Пробой диапазона', reversion:'Откат 2σ + RSI', pullback:'Откат RSI2'})[kind] || kind; }
let key = document.querySelector('meta[name=control-key]').content;
let updating = false;
let series = [];
let busy = false;
let testError = "";
let testStarting = false;
let activeJob = null;
let currentProfile = null,
    profileSignature = "",
    profileBusy = false;
const $ = id => document.getElementById(id);
const text = (id, value) => {
    if ($(id).textContent !== String(value)) $(id).textContent = value
};
const rub = v => typeof v === 'number' && Number.isFinite(v) ? v.toLocaleString('ru-RU', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
}) + ' ₽' : '—';
const date = v => v ? new Date(v).toLocaleString('ru-RU', {
    timeZone: 'Europe/Moscow'
}) + ' МСК' : '—';
const actionLabel = value => ({
    BUY: 'Покупка',
    SELL: 'Продажа',
    HOLD: 'Ожидание',
    ORDER_DIRECTION_BUY: 'Покупка',
    ORDER_DIRECTION_SELL: 'Продажа'
} [value] || value || '—');
const reasonLabel = value => ({
    'no actionable crossing': 'Нет сигнала для сделки',
    'market orders unavailable': 'Рыночные заявки недоступны'
} [value] || value || '—');
const eventLabel = kind => ({
    SIGNAL: 'Сигнал',
    PLAN: 'План',
    DECISION: 'Решение',
    ORDER_PREPARED: 'Подготовка',
    ORDER_SENT: 'Отправка',
    ORDER_STATUS: 'Исполнение',
    ERROR: 'Ошибка'
} [kind] || kind);

function journalDescription(e) {
    const p = e.payload && typeof e.payload === 'object' ? e.payload : {};
    if (e.kind === 'PLAN') return (p.mode === 'execute' ? 'Исполнение' : 'Наблюдение') + ' · ' + actionLabel(p.action) + ' · ' + p.planned_lots + ' лот · цена ' + rub(p.reference_price) + ' · сумма ' + rub(p.notional) + ' · комиссия ≈ ' + rub(p.estimated_commission) + ' · ' + reasonLabel(p.reason);
    if (e.kind === 'SIGNAL') return actionLabel(p.action) + ' · ' + reasonLabel(p.reason) + ' · цена ' + rub(p.reference_price);
    if (e.kind === 'DECISION') return p.decision || 'Подробности отсутствуют';
    if (e.kind === 'ORDER_STATUS') return actionLabel(p.side) + ' · ' + p.status + ' · исполнено ' + p.filled_lots + '/' + p.requested_lots + ' лот · комиссия ' + rub(p.commission);
    if (e.kind === 'ORDER_PREPARED') return actionLabel(p.side) + ' · ' + p.lots + ' лот · заявка сохранена перед отправкой';
    if (e.kind === 'ORDER_SENT') return 'Брокер принял запрос · ' + p.status;
    return p.error_type || '';
}

function table(id, rows) {
    const root = $(id),
        signature = JSON.stringify(rows);
    if (root.dataset.signature === signature) return;
    root.dataset.signature = signature;
    root.replaceChildren();
    if (!rows.length) {
        const td = document.createElement('td'),
            tr = document.createElement('tr');
        td.colSpan = root.closest('table').querySelectorAll('thead th').length;
        td.className = 'empty-state';
        td.textContent = id === 'journal-events' ? 'Решений пока нет. Запустите наблюдение, чтобы увидеть работу робота.' : id === 'positions' ? 'Открытых позиций нет' : id === 'paper-events' ? 'Виртуальных сделок пока нет. Запустите бумажную торговлю и дождитесь нового сигнала.' : 'Активных заявок нет';
        tr.append(td);
        root.append(tr);
        return;
    }
    for (const row of rows) {
        const tr = document.createElement('tr');
        for (const value of row) {
            const td = document.createElement('td');
            td.textContent = value;
            tr.append(td)
        }
        root.append(tr)
    }
}
let journalEvents = [],
    latestLogs = [],
    visibleLogs = [],
    logsPaused = false;

function filterJournal(events, selected) {
    return events.filter(e => selected === 'all' || (selected === 'ORDER' ? String(e.kind).startsWith('ORDER_') : e.kind === selected));
}

function filterLogs(lines, query) {
    const needle = query.trim().toLocaleLowerCase('ru-RU');
    return lines.filter(line => String(line).toLocaleLowerCase('ru-RU').includes(needle));
}

function renderJournal() {
    const rows = filterJournal(journalEvents, $('journal-filter').value);
    table('journal-events', rows.map(e => [date(e.time), eventLabel(e.kind), String(e.plan_id || '').slice(0, 8), journalDescription(e)]));
    text('journal-count', 'Показано ' + rows.length + ' из ' + journalEvents.length);
    if (!rows.length && journalEvents.length) $('journal-events').querySelector('td').textContent = 'В загруженных событиях нет записей этого типа.';
}

function renderLogs() {
    const logs = $('logs'),
        atEnd = logs.scrollHeight - logs.scrollTop - logs.clientHeight < 35;
    const rows = filterLogs(visibleLogs, $('log-search').value);
    text('logs', rows.length ? rows.join('\n') : visibleLogs.length ? 'Совпадений в загруженных строках нет.' : 'Журнал пока пуст.');
    text('log-count', 'Строк: ' + rows.length + ' / ' + visibleLogs.length + (logsPaused ? ' · обновление на паузе' : ''));
    if (atEnd) logs.scrollTop = logs.scrollHeight;
}
let chartIndex = null;
const reportDate = v => new Date(v).toLocaleString('ru-RU', {
    timeZone: 'Europe/Moscow',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
});
const compactDate = v => new Date(v).toLocaleString('ru-RU', {
    timeZone: 'Europe/Moscow',
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
});

function draw() {
    const canvas = $('chart'),
        box = canvas.getBoundingClientRect(),
        dpr = Math.min(window.devicePixelRatio || 1, 2);
    if (!box.width) return;
    canvas.width = box.width * dpr;
    canvas.height = box.height * dpr;
    const c = canvas.getContext('2d');
    c.scale(dpr, dpr);
    const w = box.width,
        h = box.height,
        p = {
            l: 10,
            r: 58,
            t: 20,
            b: 32
        };
    c.font = '11px system-ui';
    if (!series.length) {
        c.fillStyle = '#9aa8bd';
        c.fillText('Ожидаем закрытые свечи', 18, h / 2);
        text('chart-readout', 'Данные графика пока недоступны');
        return;
    }
    const values = series.flatMap(s => [s.close, s.ma10, s.ma30]).filter(Number.isFinite);
    if (!values.length) return;
    let lo = Math.min(...values),
        hi = Math.max(...values);
    const spread = Math.max(hi - lo, .1);
    lo -= spread * .1;
    hi += spread * .1;
    const x = i => p.l + (w - p.l - p.r) * i / Math.max(1, series.length - 1),
        y = v => p.t + (h - p.t - p.b) * (hi - v) / (hi - lo);
    for (let i = 0; i < 5; i++) {
        const v = lo + (hi - lo) * i / 4,
            yy = y(v);
        c.strokeStyle = '#253044';
        c.lineWidth = 1;
        c.beginPath();
        c.moveTo(p.l, yy);
        c.lineTo(w - p.r, yy);
        c.stroke();
        c.fillStyle = '#9aa8bd';
        c.fillText(v.toFixed(2), w - p.r + 8, yy + 4);
    }
    const gradient = c.createLinearGradient(0, p.t, 0, h - p.b);
    gradient.addColorStop(0, 'rgba(99,223,180,.16)');
    gradient.addColorStop(1, 'rgba(99,223,180,0)');
    c.beginPath();
    c.moveTo(x(0), h - p.b);
    series.forEach((s, i) => c.lineTo(x(i), y(s.close)));
    c.lineTo(x(series.length - 1), h - p.b);
    c.closePath();
    c.fillStyle = gradient;
    c.fill();
    for (const [field, color] of [
            ['close', '#63dfb4'],
            ['ma10', '#7da6ff'],
            ['ma30', '#f2c76c']
        ]) {
        c.strokeStyle = color;
        c.lineWidth = field === 'close' ? 2 : 1.5;
        c.beginPath();
        let started = false;
        series.forEach((s, i) => {
            if (!Number.isFinite(s[field])) {
                started = false;
                return;
            }
            if (!started) {
                c.moveTo(x(i), y(s[field]));
                started = true;
            } else c.lineTo(x(i), y(s[field]));
        });
        c.stroke();
    }
    c.fillStyle = '#9aa8bd';
    c.fillText(compactDate(series[0].time), p.l, h - 6);
    const end = compactDate(series.at(-1).time);
    c.fillText(end, Math.max(p.l, w - p.r - c.measureText(end).width), h - 6);
    const i = chartIndex === null ? series.length - 1 : Math.min(chartIndex, series.length - 1),
        bar = series[i];
    text('chart-readout', compactDate(bar.time) + ' МСК · ' + rub(bar.close) + ' · ' + $('fast-legend').textContent + ' ' + rub(bar.ma10) + ' · ' + $('slow-legend').textContent + ' ' + rub(bar.ma30));
    if (chartIndex !== null) {
        c.strokeStyle = '#9aa8bd';
        c.setLineDash([3, 4]);
        c.beginPath();
        c.moveTo(x(i), p.t);
        c.lineTo(x(i), h - p.b);
        c.stroke();
        c.setLineDash([]);
        c.beginPath();
        c.arc(x(i), y(bar.close), 4, 0, Math.PI * 2);
        c.fillStyle = '#63dfb4';
        c.fill();
    }
}
let chartSignature = '';

function setSeries(data) {
    const next = (data || []).filter(b => Number.isFinite(b.close) && b.close > 0 && Number.isFinite(Date.parse(b.time)));
    const signature = JSON.stringify(next);
    if (signature !== chartSignature) {
        series = next;
        chartSignature = signature;
        draw();
    }
}

function setCommandLoading(action, mode, loading) {
    const id = action === 'stop' ? 'stop' : mode === 'paper' ? 'paper-start' : mode === 'observe' ? 'observe' : 'execute',
        button = $(id);
    button.textContent = loading ? (action === 'stop' ? 'Останавливаем…' : 'Запускаем…') : {
        'paper-start': '▶ Бумажная торговля',
        observe: '▶ Запустить наблюдение',
        execute: '▶ Исполнение в Sandbox',
        stop: '■ Остановить процесс робота'
    } [id];
    button.setAttribute('aria-busy', String(loading));
}

function renderBacktest(d) {
    const root = $('test-result'),
        bt = d.backtest?.summary,
        bp = d.backtest?.parameters;
    const state = testError || (testStarting ? 'Отправляем запрос на запуск…' : d.backtest_running ? 'Проверяем стратегию на истории. Отчёт появится после завершения.' : d.backtest_exit_code ? 'Тест завершился ошибкой. Подробности в журнале.' : bt ? (activeJob && d.backtest_id === activeJob ? 'Тест завершён' : 'Последний сохранённый результат') : 'Запустите первый тест. Он использует историю и не отправляет заявки.');
    const signature = JSON.stringify([d.backtest?.profile, d.backtest?.instrument, state, bt, bp, testStarting, d.backtest_running, d.backtest_exit_code]);
    if (root.dataset.signature !== signature) {
        root.dataset.signature = signature;
        root.replaceChildren();
        const heading = document.createElement('div');
        heading.textContent = state;
        root.append(heading);
        if (bt && !testError && !testStarting && !d.backtest_running && !d.backtest_exit_code) {
            const period = document.createElement('div');
            period.className = 'note';
            period.textContent = (d.backtest?.profile?.name || d.backtest?.instrument?.ticker || 'Сохранённый тест') + ' · ' + (bp ? reportDate(bp.start) + ' — ' + reportDate(bp.end) + ' МСК · ' + strategyLabel(bp.strategy?.kind || 'ma') : '');
            root.append(period);
            const metrics = document.createElement('div');
            metrics.className = 'result-metrics';
            for (const [label, value, color] of [
                    ['Чистый результат', rub(bt.net_pnl), bt.net_pnl < 0 ? 'negative' : 'positive'],
                    ['Комиссия', rub(bt.fees), ''],
                    ['Просадка', Number.isFinite(bt.max_drawdown_pct) ? bt.max_drawdown_pct.toFixed(3) + '%' : '—', ''],
                    ['Закрытые сделки', bt.closed_trades ?? '—', '']
                ]) {
                const block = document.createElement('div'),
                    name = document.createElement('span'),
                    number = document.createElement('strong');
                name.textContent = label;
                number.textContent = value;
                number.className = color;
                block.append(name, number);
                metrics.append(block);
            }
            root.append(metrics);
        }
    }
    const disabled = testStarting || d.backtest_running;
    for (const input of $('history').querySelectorAll('input,select')) input.disabled = disabled;
    const report = $('test-report');
    report.setAttribute('aria-disabled', String(disabled || !bt));
    report.tabIndex = disabled || !bt ? -1 : 0;
}
async function update() {
    if (updating) return;
    updating = true;
    $('refresh-status').disabled = true;
    $('refresh-status').setAttribute('aria-busy', 'true');
    try {
        const r = await timedFetch('/api/status?lines=' + $('log-lines').value);
        if (!r.ok) throw Error('HTTP ' + r.status);
        const d = await r.json();
        renderProfiles(d);
        renderPaper(d.paper);
        if (d.panel_session && !key.startsWith(d.panel_session)) {
            await refreshControl();
            text('message', 'Управление восстановлено после перезапуска панели.');
        }
        journalEvents = Array.isArray(d.journal) ? d.journal : [];
        renderJournal();
        text('journal-error', d.journal_error || '');
        const s = d.bot_state || {},
            a = d.account,
            m = d.market,
            runner = d.runner;
        $('test-start').disabled = testStarting || d.backtest_running;
        $('test-start').textContent = testStarting ? 'Отправляем…' : d.backtest_running ? 'Тест выполняется…' : 'Прогнать историю';
        $('test-start').setAttribute('aria-busy', String(testStarting || d.backtest_running));
        renderBacktest(d);
        text('connection', d.connected ? '● Связь с брокером' : '○ Нет связи с брокером');
        $('connection').className = 'pill ' + (d.connected ? 'good' : 'warn');
        text('price', rub(d.price));
        text('quote', 'Котировка: ' + date(d.quote_time) + (d.quote_age > 120 ? ' · устарела' : ''));
        text('equity', rub(a?.equity));
        text('cash', a ? 'Деньги: ' + rub(a.cash) + ' · блок: ' + rub(a.blocked) : 'Счёт появится после запуска робота');
        text('position', s.held_lots == null ? '—' : s.held_lots + ' лот');
        text('entry', s.entry_price ? 'Вход: ' + rub(s.entry_price) : 'Открытой позиции робота нет');
        text('market', m ? (m.market ? 'Рыночные доступны' : m.limit ? 'Только лимитные' : 'Торги недоступны') : '—');
        text('availability', m ? (m.market ? 'Проверки риска выполняются перед заявкой' : m.limit ? 'Рыночный выход сейчас недоступен' : 'Ожидаем доступную торговую сессию') : 'Проверяем доступность заявок');
        text('signal', 'Сигнал: ' + actionLabel(d.signal));
        text('runner', runner.running ? (runner.mode === 'paper' ? 'Бумажная торговля' : runner.mode === 'execute' ? 'Исполнение' : 'Наблюдение') : 'Остановлен');
        $('runner').className = 'pill ' + (runner.running ? 'good' : '');
        const riskState = runner.mode === 'paper' ? d.paper : s;
        text('entries', (riskState.entries ?? 0) + ' / 2');
        text('attempts', (riskState.attempts ?? 0) + ' / 4');
        text('risk-stop', d.selected_profile.params.stop_pct + '%');
        text('permission', d.execution_allowed ? 'Флаги исполнения включены. Запуск — отдельной кнопкой.' : 'Исполнение отключено настройками. Наблюдение доступно без торговых заявок.');
        $('observe').disabled = busy || runner.running;
        $('paper-start').disabled = profileBusy || busy || runner.running || !d.connected;
        for (const id of ['profile-select', 'profile-import', 'profile-create']) $(id).disabled = profileBusy || runner.running || d.backtest_running;
        $('execute').disabled = busy || runner.running || !d.execution_allowed || !d.connected;
        $('stop').disabled = busy || !runner.running;
        text('account', a ? '…' + a.id.slice(-12) : 'Счёт ещё не создан');
        table('positions', (a?.positions || []).map(p => [p.uid, p.shares, p.blocked]));
        table('orders', (a?.orders || []).map(o => [actionLabel(o.side), o.executed + '/' + o.requested, o.status.replace('EXECUTION_REPORT_STATUS_', '')]));
        setSeries(d.series);
        latestLogs = Array.isArray(d.logs) ? d.logs : [];
        if (!logsPaused) {
            visibleLogs = latestLogs;
            renderLogs();
        }
        text('updated', 'Последнее обновление: ' + date(d.updated));
        let warning = d.error || d.state_error || (s.halted ? 'Дневные входы заблокированы ограничением риска.' : '');
        if (!warning && runner.exit_code && runner.exit_code !== 130) warning = 'Процесс завершился с кодом ' + runner.exit_code + '. Причина в журнале.';
        $('alert').style.display = warning ? 'block' : 'none';
        text('alert', warning)
    } catch (e) {
        $('alert').style.display = 'block';
        text('alert', 'Нет связи с локальной панелью: ' + e.message + '. Следующая попытка выполняется автоматически.');
        text('connection', '○ Панель недоступна');
        $('connection').className = 'pill warn';
        for (const id of ['paper-start', 'observe', 'execute', 'stop', 'test-start']) $(id).disabled = true
    } finally {
        updating = false;
        $('refresh-status').disabled = false;
        $('refresh-status').setAttribute('aria-busy', 'false');
    }
}
async function timedFetch(url, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 12000);
    try {
        return await fetch(url, {
            ...options,
            signal: controller.signal
        })
    } finally {
        clearTimeout(timer)
    }
}
async function refreshControl() {
    const r = await timedFetch('/api/control-session');
    if (!r.ok) throw Error('Не удалось восстановить управление: HTTP ' + r.status);
    const d = await r.json();
    if (typeof d.key !== 'string' || !d.key) throw Error('Неверный ответ панели');
    key = d.key;
}
async function postControl(action, body) {
    await refreshControl();
    const r = await timedFetch('/api/' + action, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-Control-Key': key
        },
        body: JSON.stringify(body)
    });
    const d = await r.json();
    if (!r.ok) throw Error(d.error || 'Ошибка команды');
    return d;
}
async function sendBotCommand(action, mode) {
    if (busy) return;
    busy = true;
    setCommandLoading(action, mode, true);
    for (const id of ['paper-start', 'observe', 'execute', 'stop']) $(id).disabled = true;
    try {
        await postControl(action, {
            mode,
            profile_id: currentProfile?.id
        });
        text('message', action === 'stop' ? 'Остановка запрошена. Позиция не закрывается.' : 'Процесс запускается; состояние появится через несколько секунд.')
    } catch (e) {
        text('message', e.name === 'AbortError' ? 'Время ожидания истекло. Проверьте состояние процесса; команда автоматически не повторяется.' : e.message)
    } finally {
        busy = false;
        setCommandLoading(action, mode, false);
        await update()
    }
}
const todayMSK = new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'Europe/Moscow',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit'
}).format(new Date());
const dayMS = 86400000,
    timelineEnd = Date.parse(todayMSK + 'T00:00:00Z'),
    timelineStart = timelineEnd - 364 * dayMS;
const isoDay = ms => new Date(ms).toISOString().slice(0, 10);

function syncPeriod() {
    const first = $('test-from').value,
        last = $('test-to').value;
    for (const [id, v] of [
            ['period-start', first],
            ['period-end', last]
        ]) $(id).value = Math.round((Date.parse(v + 'T00:00:00Z') - timelineStart) / dayMS);
    text('period-caption', first.split('-').reverse().join('.') + ' — ' + last.split('-').reverse().join('.') + ' · ' + (Math.round((Date.parse(last) - Date.parse(first)) / dayMS) + 1) + ' дней');
}

function choosePeriod(value) {
    if (value === 'custom') return;
    const days = Number(value);
    $('test-to').value = todayMSK;
    $('test-from').value = isoDay(timelineEnd - (days - 1) * dayMS);
    syncPeriod();
}

function datesChanged() {
    $('test-days').value = 'custom';
    if ($('test-from').value && $('test-to').value) syncPeriod();
}

function slidePeriod(which) {
    let a = Number($('period-start').value),
        b = Number($('period-end').value);
    if (a > b) {
        if (which === 'start') b = a;
        else a = b;
    }
    $('period-start').value = a;
    $('period-end').value = b;
    $('test-from').value = isoDay(timelineStart + a * dayMS);
    $('test-to').value = isoDay(timelineStart + b * dayMS);
    datesChanged();
}

async function runHistoricalTest() {
    if (testStarting || $('test-start').disabled) return;
    testError = '';
    testStarting = true;
    $('test-start').disabled = true;
    text('test-result', 'Запускаем тест…');
    try {
        const body = {
            days: 30,
            profile_id: currentProfile?.id,
            strategy_kind: currentProfile?.params.kind || 'ma',
            from_date: $('test-from').value,
            to_date: $('test-to').value,
            fast: Number($('test-fast').value),
            slow: Number($('test-slow').value),
            trend: Number($('test-trend').value),
            entry_edge_bps: Number($('test-edge').value),
            stop_pct: Number($('test-stop').value),
            fee_bps: Number($('test-fee').value),
            slippage_bps: Number($('test-slip').value)
        };
        const d = await postControl('backtest', body);
        activeJob = d.job_id;
        text('test-result', 'Тест принят, номер ' + activeJob + '. Ожидаем отчёт…');
        $('test-report').href = '/reports/latest?job=' + encodeURIComponent(activeJob)
    } catch (e) {
        testError = 'Не удалось запустить: ' + e.message + ' Если панель перезапускалась, обновите вкладку.';
        text('test-result', testError)
    } finally {
        testStarting = false;
        await update()
    }
}
choosePeriod('30');

update();
setInterval(update, 3000);
window.addEventListener('resize', draw);

$('chart').addEventListener('pointermove', event => {
    if (!series.length) return;
    const box = event.currentTarget.getBoundingClientRect();
    chartIndex = Math.max(0, Math.min(series.length - 1, Math.round((event.clientX - box.left - 10) / (box.width - 68) * (series.length - 1))));
    draw();
});
$('chart').addEventListener('pointerleave', () => {
    chartIndex = null;
    draw();
});
$('chart').addEventListener('keydown', event => {
    if (!series.length || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    chartIndex = event.key === 'Home' ? 0 : event.key === 'End' ? series.length - 1 : Math.max(0, Math.min(series.length - 1, (chartIndex ?? series.length - 1) + (event.key === 'ArrowLeft' ? -1 : 1)));
    draw();
});
for (const [id, action, mode] of [
        ['paper-start', 'start', 'paper'],
        ['observe', 'start', 'observe'],
        ['execute', 'start', 'execute'],
        ['stop', 'stop', undefined]
    ]) $(id).addEventListener('click', () => sendBotCommand(action, mode));
const navLinks = [...document.querySelectorAll('.sidebar nav a')];

function updateNavigation() {
    const current = ['activity', 'history'].find(id => $(id).getBoundingClientRect().top <= Math.min(160, window.innerHeight / 3)) || 'overview';
    for (const link of navLinks) {
        const active = link.hash === '#' + current;
        link.classList.toggle('active', active);
        if (active) link.setAttribute('aria-current', 'location');
        else link.removeAttribute('aria-current');
    }
}
$('overview').addEventListener('scroll', updateNavigation, {
    passive: true
});
window.addEventListener('scroll', updateNavigation, {
    passive: true
});
window.addEventListener('load', updateNavigation);
window.addEventListener('hashchange', updateNavigation);
updateNavigation();
if (typeof ResizeObserver !== 'undefined') new ResizeObserver(draw).observe($('chart'));

$('journal-filter').addEventListener('change', renderJournal);
$('log-search').addEventListener('input', renderLogs);
$('refresh-status').addEventListener('click', update);
$('log-pause').addEventListener('click', () => {
    logsPaused = !logsPaused;
    $('log-pause').setAttribute('aria-pressed', String(logsPaused));
    $('log-pause').textContent = logsPaused ? '▶ Возобновить журнал' : 'Ⅱ Пауза обновления журнала';
    if (!logsPaused) visibleLogs = latestLogs;
    renderLogs();
});

function renderProfiles(d) {
    const profile = d.selected_profile;
    if (!profile) return;
    const signature = JSON.stringify(d.profiles);
    if (signature !== profileSignature) {
        profileSignature = signature;
        $('profile-select').replaceChildren(...d.profiles.map(p => {
            const o = document.createElement('option');
            o.value = p.id;
            o.textContent = p.name;
            return o;
        }));
    }
    $('profile-select').value = profile.id;
    if (currentProfile?.id !== profile.id) {
        currentProfile = profile;
        for (const [id, key] of [
                ['test-fast', 'fast'],
                ['test-slow', 'slow'],
                ['test-trend', 'trend'],
                ['test-edge', 'entry_edge_bps'],
                ['test-stop', 'stop_pct']
            ]) $(id).value = profile.params[key];
        $('test-fee').value = profile.fee_bps;
        $('test-slip').value = profile.slippage_bps;
        text('profile-subtitle', profile.name + ' · ' + profile.ticker + ' · 15 минут');
        text('sidebar-instrument', profile.ticker + ' · 15 минут');
        text('price-label', 'ПОСЛЕДНЯЯ ЦЕНА · ' + profile.ticker);
        text('fast-legend', (profile.params.kind === 'breakout' ? 'Нижняя граница ' : 'Средняя ') + profile.params.fast);
        text('slow-legend', (profile.params.kind === 'breakout' ? 'Верхняя граница ' : 'Средняя ') + profile.params.slow);
        const description = strategyLabel(profile.params.kind) + ' · окна ' + profile.params.fast + '/' + profile.params.slow + ' · фильтр тренда ' + profile.params.trend + ' · стоп ' + profile.params.stop_pct + '% · комиссия ' + profile.fee_bps + ' bps · проскальзывание ' + profile.slippage_bps + ' bps';
        text('profile-description', description);
        text('strategy-description', profile.name + ' · ' + description);
        chartSignature = '';
        chartIndex = null;
        $('test-report').href = '/reports/latest';
        activeJob = null;
        testError = '';
    }
}

function renderPaper(p) {
    if (!p) return;
    text('paper-state', p.running ? 'Работает' : 'Остановлен');
    $('paper-state').className = 'pill ' + (p.running ? 'good' : '');
    text('paper-equity', rub(p.equity));
    text('paper-pnl', rub(p.net_pnl));
    $('paper-pnl').className = p.net_pnl < 0 ? 'negative' : p.net_pnl > 0 ? 'positive' : '';
    text('paper-fees', rub(p.fees));
    text('paper-dd', p.drawdown_pct.toFixed(3) + '%');
    text('paper-detail', 'Капитал: ' + rub(p.initial) + ' · деньги: ' + rub(p.cash) + ' · позиция: ' + p.lots + ' лот · закрытый результат: ' + rub(p.realized) + ' · открытый результат: ' + rub(p.unrealized));
    text('paper-decision', p.decision);
    table('paper-events', [...p.events].reverse().map(e => [date(e.time), actionLabel(e.action), rub(e.price), e.shares, rub(e.fee)]));
}
async function selectProfile(id) {
    if (profileBusy) return;
    profileBusy = true;
    try {
        await postControl('profile/select', {
            id
        });
        text('profile-message', 'Профиль выбран. Ожидаем данные инструмента; сделки автоматически не запускаются.');
    } catch (e) {
        text('profile-message', e.message);
    } finally {
        profileBusy = false;
        await update();
    }
}
async function addProfile(data) {
    if (profileBusy) return;
    profileBusy = true;
    try {
        const r = await postControl('profile/import', data);
        await postControl('profile/select', {
            id: r.profile.id
        });
        text('profile-message', 'Профиль сохранён и выбран. Дождитесь котировок.');
    } catch (e) {
        text('profile-message', e.message);
    } finally {
        profileBusy = false;
        await update();
    }
}
$('profile-select').addEventListener('change', event => selectProfile(event.target.value));
$('profile-import').addEventListener('click', () => $('profile-file').click());
$('profile-file').addEventListener('change', async event => {
    const file = event.target.files[0];
    if (!file) return;
    try {
        if (file.size > 20000) throw Error('Файл профиля должен быть меньше 20 КБ.');
        await addProfile(JSON.parse(await file.text()));
    } catch (e) {
        text('profile-message', 'Не удалось загрузить профиль: ' + e.message);
    } finally {
        event.target.value = '';
    }
});
$('profile-create').addEventListener('click', () => {
    const kind = $('profile-algorithm').value,
        filtered = kind === 'filtered',
        breakout = kind === 'breakout',
        reversion = kind === 'reversion', pullback = kind === 'pullback';
    addProfile({
        name: $('profile-name').value,
        ticker: $('profile-ticker').value,
        params: {
            kind: breakout ? 'breakout' : reversion ? 'reversion' : pullback ? 'pullback' : 'ma',
            fast: pullback ? 2 : reversion ? 5 : breakout ? 30 : filtered ? 20 : 10,
            slow: pullback ? 5 : reversion ? 40 : breakout ? 120 : filtered ? 60 : 30,
            trend: reversion || pullback ? 200 : filtered || breakout ? 120 : 0,
            entry_edge_bps: reversion || pullback ? 30 : 0,
            stop_pct: reversion || pullback ? 2 : 1
        }
    });
});
