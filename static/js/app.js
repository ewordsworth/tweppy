/* Tweppy dashboard behaviour. No framework: plain DOM, progressive enhancement. */
(function () {
    'use strict';

    const PAGE = JSON.parse(document.getElementById('page-data').textContent || '{}');
    const TWEET_MAX = PAGE.tweetMax || 280;
    const COST_POST = PAGE.costPerPost;
    const COST_LINK = PAGE.costPerLinkPost;
    const URL_RE = /https?:\/\/\S+|\bwww\.\S+/gi;

    const $ = (sel, root = document) => root.querySelector(sel);
    const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
    const pad = n => String(n).padStart(2, '0');

    function el(tag, attrs = {}, children = []) {
        const node = document.createElement(tag);
        for (const [key, value] of Object.entries(attrs)) {
            if (value === null || value === undefined || value === false) continue;
            if (key === 'class') node.className = value;
            else if (key === 'text') node.textContent = value;
            else node.setAttribute(key, value === true ? '' : value);
        }
        for (const child of [].concat(children)) {
            if (child === null || child === undefined) continue;
            node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
        }
        return node;
    }

    function money(n) { return '$' + Number(n).toFixed(n < 0.1 ? 3 : 2); }

    /* ------------------------------------------------------------------ *
     * Dates
     * ------------------------------------------------------------------ */

    function localInputValue(date) {
        return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
    }

    function dayKey(date) {
        return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
    }

    function friendlyDate(date, timeOnly) {
        const time = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        const today = new Date();
        const tomorrow = new Date(); tomorrow.setDate(today.getDate() + 1);
        const yesterday = new Date(); yesterday.setDate(today.getDate() - 1);
        const key = dayKey(date);
        if (key === dayKey(today)) return timeOnly ? time : `Today ${time}`;
        if (key === dayKey(tomorrow)) return `Tomorrow ${time}`;
        if (key === dayKey(yesterday)) return `Yesterday ${time}`;
        const sameYear = date.getFullYear() === today.getFullYear();
        return date.toLocaleDateString([], { month: 'short', day: 'numeric', year: sameYear ? undefined : 'numeric' }) + ' ' + time;
    }

    function renderTimestamps(root = document) {
        $$('.ts[data-iso]', root).forEach(node => {
            const date = new Date(node.getAttribute('data-iso'));
            if (isNaN(date)) return;
            node.textContent = friendlyDate(date, node.getAttribute('data-format') === 'time');
            node.title = date.toLocaleString();
        });
    }

    function humanizeSeconds(sec) {
        if (sec < 60) return `${Math.max(0, Math.round(sec))}s`;
        const min = Math.round(sec / 60);
        if (min < 60) return `${min}m`;
        const h = Math.floor(min / 60), m = min % 60;
        if (h < 24) return m ? `${h}h ${m}m` : `${h}h`;
        const d = Math.floor(h / 24), rh = h % 24;
        return rh ? `${d}d ${rh}h` : `${d}d`;
    }

    function tickCountdowns() {
        $$('.countdown[data-until]').forEach(node => {
            const until = new Date(node.getAttribute('data-until'));
            const sec = (until - new Date()) / 1000;
            node.textContent = sec > 0 ? `(in ${humanizeSeconds(sec)})` : '(due now)';
        });
    }

    /* ------------------------------------------------------------------ *
     * Tweet length and cost hints
     * X counts most characters as 1, CJK and emoji as 2, and every link as 23.
     * ------------------------------------------------------------------ */

    function xLength(text) {
        const withLinks = text.replace(URL_RE, 'x'.repeat(23));
        let count = 0;
        for (const ch of withLinks) {
            const c = ch.codePointAt(0);
            const light = c <= 4351 || (c >= 8192 && c <= 8205) || (c >= 8208 && c <= 8223) || (c >= 8242 && c <= 8247);
            count += light ? 1 : 2;
        }
        return count;
    }

    function hasLink(text) { URL_RE.lastIndex = 0; const found = URL_RE.test(text); URL_RE.lastIndex = 0; return found; }

    function parseBrackets(text) {
        const out = [];
        const re = /\[([^\]]+)\]/g;
        let m;
        while ((m = re.exec(text)) !== null) {
            const t = m[1].trim();
            if (t) out.push(t);
        }
        return out;
    }

    function badges(text) {
        const len = xLength(text);
        const list = [el('span', { class: 'count' + (len > TWEET_MAX ? ' over' : ''), text: `${len}/${TWEET_MAX}` })];
        if (len > TWEET_MAX) list.push(el('span', { class: 'badge badge-error', text: 'Too long' }));
        if (hasLink(text)) list.push(el('span', { class: 'badge badge-warn', text: `Has link · about ${money(COST_LINK)}` }));
        return list;
    }

    function updatePreview(textarea) {
        const target = document.getElementById(textarea.getAttribute('data-preview'));
        if (!target) return;
        target.replaceChildren();
        const value = textarea.value;
        if (!value.trim()) return;

        if (textarea.hasAttribute('data-single')) {
            target.appendChild(el('div', { class: 'preview-row' }, badges(value.trim())));
            return;
        }

        const tweets = parseBrackets(value);
        const leftover = value.replace(/\[[^\]]*\]/g, '').trim();
        const linkCount = tweets.filter(hasLink).length;
        const cost = (tweets.length - linkCount) * COST_POST + linkCount * COST_LINK;
        const summary = tweets.length
            ? `${tweets.length} tweet${tweets.length === 1 ? '' : 's'} found · about ${money(cost)} to post`
            : 'No tweets found yet. Wrap each one in [square brackets].';
        target.appendChild(el('div', { class: 'preview-summary', text: summary }));
        if (leftover) target.appendChild(el('div', { class: 'preview-row' }, [el('span', { class: 'badge badge-warn', text: 'Text outside [brackets] will be ignored' })]));
        tweets.forEach((t, i) => {
            const snippet = t.length > 60 ? t.slice(0, 60) + '…' : t;
            target.appendChild(el('div', { class: 'preview-row' }, [el('span', { class: 'preview-index', text: `#${i + 1}` }), el('span', { class: 'preview-text', text: snippet })].concat(badges(t))));
        });
    }

    function initPreviews() {
        $$('textarea[data-preview]').forEach(area => {
            area.addEventListener('input', () => updatePreview(area));
            updatePreview(area);
        });
        $$('.js-char-count').forEach(span => {
            const p = span.parentElement.querySelector('p');
            if (!p) return;
            span.replaceChildren(...badges(p.textContent.trim()));
        });
    }

    /* ------------------------------------------------------------------ *
     * Tabs
     * ------------------------------------------------------------------ */

    const chartsRendered = new Set();

    function setActiveTab(tabId) {
        const target = document.getElementById(tabId);
        if (!target) tabId = 'dashboard-content';
        $$('.nav-link').forEach(link => link.classList.toggle('active', link.getAttribute('data-tab') === tabId));
        $$('.tab-content').forEach(content => content.classList.toggle('active', content.id === tabId));
        localStorage.setItem('activeMainTab', tabId);
        const link = $(`.nav-link[data-tab="${tabId}"]`);
        if (link && link.hash && location.hash !== link.hash) history.replaceState(null, '', link.hash);
        if (tabId === 'analytics-content') renderCharts();
    }

    function initTabs() {
        $$('.nav-link').forEach(link => link.addEventListener('click', e => {
            e.preventDefault();
            setActiveTab(link.getAttribute('data-tab'));
        }));
        document.addEventListener('click', e => {
            const go = e.target.closest('[data-goto-tab]');
            if (!go) return;
            e.preventDefault();
            setActiveTab(go.getAttribute('data-goto-tab'));
        });

        const fromHash = location.hash && $(`.nav-link[href="${location.hash}"]`);
        setActiveTab(fromHash ? fromHash.getAttribute('data-tab') : (localStorage.getItem('activeMainTab') || 'dashboard-content'));

        $$('.tabbed-card').forEach(card => {
            const links = $$('.card-tab-link', card);
            links.forEach(link => link.addEventListener('click', e => {
                e.preventDefault();
                links.forEach(l => l.classList.toggle('active', l === link));
                $$('.card-tab-content', card).forEach(c => c.classList.toggle('active', c.id === link.getAttribute('data-tab')));
            }));
        });
    }

    /* ------------------------------------------------------------------ *
     * Alerts, modals, menus
     * ------------------------------------------------------------------ */

    function dismiss(alert) {
        alert.style.transition = 'opacity 0.4s ease';
        alert.style.opacity = '0';
        setTimeout(() => alert.remove(), 400);
    }

    function initAlerts() {
        $$('.alert').forEach(alert => {
            // Errors stay until dismissed; everything else fades after 6 seconds.
            if (!alert.classList.contains('alert-error')) setTimeout(() => dismiss(alert), 6000);
            const close = $('.close-alert', alert);
            if (close) close.addEventListener('click', () => dismiss(alert));
        });
    }

    function openModal(id) {
        const modal = document.getElementById(id);
        if (!modal) return;
        modal.classList.add('open');
        const first = $('textarea, input:not([type=hidden]), select', modal);
        if (first) setTimeout(() => first.focus(), 30);
    }

    function closeModals() { $$('.modal.open').forEach(m => m.classList.remove('open')); }

    function initModals() {
        document.addEventListener('click', e => {
            if (e.target.closest('.js-close-modal') || e.target.classList.contains('modal')) closeModals();
        });
        document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModals(); });

        // Edit a queued tweet (profile or community): the button carries the form action.
        document.addEventListener('click', e => {
            const btn = e.target.closest('.js-edit-tweet');
            if (!btn) return;
            $('#edit-form').action = btn.getAttribute('data-action');
            const area = $('#edit-tweet-text');
            area.value = btn.getAttribute('data-text');
            updatePreview(area);
            openModal('edit-modal');
        });

        // Edit a scheduled tweet (dashboard list and calendar day list).
        document.addEventListener('click', e => {
            const btn = e.target.closest('.js-edit-scheduled');
            if (!btn) return;
            const form = $('#edit-scheduled-form');
            form.action = `/scheduled/update/${btn.getAttribute('data-sid')}`;
            const area = $('#edit-scheduled-text');
            area.value = btn.getAttribute('data-text');
            updatePreview(area);
            $('#edit-scheduled-datetime').value = localInputValue(new Date(btn.getAttribute('data-iso')));
            openModal('edit-scheduled-modal');
        });

        $$('.js-open-schedule').forEach(btn => btn.addEventListener('click', () => openScheduleModal()));
    }

    function openScheduleModal(day) {
        let when = new Date(Date.now() + 10 * 60000);
        if (day) {
            const preset = new Date(day.getFullYear(), day.getMonth(), day.getDate(), 9, 0);
            if (preset > when) when = preset;
        }
        const input = $('#schedule-datetime');
        input.value = localInputValue(when);
        input.min = localInputValue(new Date());
        openModal('schedule-modal');
    }

    function initMenus() {
        document.addEventListener('click', e => {
            const fab = e.target.closest('.fab-main');
            $$('.fab-container.active').forEach(c => { if (!fab || c !== fab.parentElement) c.classList.remove('active'); });
            if (fab) {
                e.stopPropagation();
                fab.parentElement.classList.toggle('active');
            }
        });
    }

    /* ------------------------------------------------------------------ *
     * Forms: local time -> UTC
     * A datetime-local value is parsed by the browser as local time, so
     * toISOString() alone gives the correct UTC instant.
     * ------------------------------------------------------------------ */

    function initForms() {
        $$('.js-utc-form').forEach(form => form.addEventListener('submit', () => {
            $$('input[data-utc-target]', form).forEach(input => {
                const hidden = document.getElementById(input.getAttribute('data-utc-target'));
                if (hidden && input.value) hidden.value = new Date(input.value).toISOString();
            });
        }));
        $$('input[data-default-offset]').forEach(input => {
            if (!input.value) input.value = localInputValue(new Date(Date.now() + Number(input.getAttribute('data-default-offset')) * 60000));
            input.min = localInputValue(new Date());
        });

        const tzButton = $('#use-browser-tz');
        const tzSelect = $('#set-timezone');
        if (tzButton && tzSelect) {
            const browserTz = Intl.DateTimeFormat().resolvedOptions().timeZone;
            tzButton.addEventListener('click', () => {
                if ($$('option', tzSelect).some(o => o.value === browserTz)) tzSelect.value = browserTz;
            });
            if (browserTz && browserTz !== PAGE.settingsTimezone) {
                const hint = $('#tz-hint');
                if (hint) hint.textContent = `Your browser is in ${browserTz}, but Tweppy is set to ${PAGE.settingsTimezone}. Quiet hours and Analytics days use the setting.`;
            }
        }
    }

    /* ------------------------------------------------------------------ *
     * Charts: single-series column charts in SVG.
     * Bars cap at 24px with a 4px rounded top and a square baseline;
     * every bar has a full-height hover/focus target and a tooltip.
     * ------------------------------------------------------------------ */

    const SVG_NS = 'http://www.w3.org/2000/svg';

    function svg(tag, attrs = {}) {
        const node = document.createElementNS(SVG_NS, tag);
        for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
        return node;
    }

    function niceStep(max, ticks) {
        const raw = Math.max(1, max / ticks);
        const pow = Math.pow(10, Math.floor(Math.log10(raw)));
        for (const m of [1, 2, 5, 10]) if (m * pow >= raw) return Math.max(1, m * pow);
        return 10 * pow;
    }

    function columnPath(x, y, w, h, r) {
        r = Math.min(r, w / 2, h);
        return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
    }

    function renderColumnChart(container, data, labelEvery) {
        const width = container.clientWidth;
        if (!width) return false;
        container.replaceChildren();
        const height = 220;
        const m = { top: 20, right: 8, bottom: 26, left: 34 };
        const plotW = width - m.left - m.right;
        const plotH = height - m.top - m.bottom;
        const maxValue = Math.max(0, ...data.map(d => d.value));
        const step = niceStep(maxValue || 1, 4);
        const top = Math.max(step, Math.ceil((maxValue || 1) / step) * step);
        const band = plotW / data.length;
        const barW = Math.min(24, Math.max(2, band - 2));
        const yOf = v => m.top + plotH - (v / top) * plotH;

        const root = svg('svg', { width, height, viewBox: `0 0 ${width} ${height}`, role: 'img' });
        root.setAttribute('aria-label', container.getAttribute('aria-label') || 'Chart');

        for (let v = 0; v <= top; v += step) {
            const y = yOf(v);
            root.appendChild(svg('line', { x1: m.left, x2: width - m.right, y1: y, y2: y, class: v === 0 ? 'chart-baseline' : 'chart-grid' }));
            const label = svg('text', { x: m.left - 8, y: y + 4, 'text-anchor': 'end', class: 'chart-axis' });
            label.textContent = v.toLocaleString();
            root.appendChild(label);
        }

        const tip = el('div', { class: 'chart-tooltip', role: 'status' });
        const tipValue = el('strong');
        const tipLabel = el('span');
        tip.append(tipValue, tipLabel);

        let maxIndex = -1;
        data.forEach((d, i) => { if (d.value > 0 && (maxIndex < 0 || d.value > data[maxIndex].value)) maxIndex = i; });

        data.forEach((d, i) => {
            const cx = m.left + i * band + band / 2;
            const group = svg('g', { class: 'chart-bar', tabindex: '0' });
            group.setAttribute('aria-label', `${d.label}: ${d.value} post${d.value === 1 ? '' : 's'}`);
            group.appendChild(svg('rect', { x: m.left + i * band, y: m.top, width: band, height: plotH, class: 'chart-hit' }));
            if (d.value > 0) {
                const y = yOf(d.value);
                group.appendChild(svg('path', { d: columnPath(cx - barW / 2, y, barW, m.top + plotH - y, 4), class: 'chart-col' }));
            }
            const show = () => {
                tipValue.textContent = `${d.value} post${d.value === 1 ? '' : 's'}`;
                tipLabel.textContent = d.label;
                tip.style.left = `${Math.min(Math.max(cx, 60), width - 60)}px`;
                tip.style.top = `${Math.max(0, yOf(d.value) - 8)}px`;
                tip.classList.add('visible');
                group.classList.add('hover');
            };
            const hide = () => { tip.classList.remove('visible'); group.classList.remove('hover'); };
            group.addEventListener('pointerenter', show);
            group.addEventListener('pointerleave', hide);
            group.addEventListener('focus', show);
            group.addEventListener('blur', hide);
            root.appendChild(group);

            if (i % labelEvery === 0 || i === data.length - 1) {
                const label = svg('text', { x: cx, y: height - 8, 'text-anchor': 'middle', class: 'chart-axis' });
                label.textContent = d.short;
                root.appendChild(label);
            }
            if (i === maxIndex) {
                const label = svg('text', { x: cx, y: yOf(d.value) - 6, 'text-anchor': 'middle', class: 'chart-value' });
                label.textContent = d.value;
                root.appendChild(label);
            }
        });

        container.appendChild(root);
        container.appendChild(tip);
        if (maxValue === 0) container.appendChild(el('p', { class: 'chart-empty', text: 'No posts in this period yet.' }));
        return true;
    }

    function renderCharts() {
        $$('.chart[data-series]').forEach(container => {
            const data = PAGE[container.getAttribute('data-series')] || [];
            if (renderColumnChart(container, data, Number(container.getAttribute('data-label-every')) || 1)) {
                chartsRendered.add(container.id);
            }
        });
    }

    let resizeTimer;
    window.addEventListener('resize', () => {
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(() => { if ($('#analytics-content.active')) renderCharts(); }, 150);
    });

    /* ------------------------------------------------------------------ *
     * Scheduler calendar
     * ------------------------------------------------------------------ */

    const calendar = {
        items: (PAGE.calendar || []).map(item => Object.assign({}, item, { date: new Date(item.at) })),
        view: new Date(new Date().getFullYear(), new Date().getMonth(), 1),
        selected: new Date(),
    };

    function itemsOn(key) {
        return calendar.items.filter(item => dayKey(item.date) === key).sort((a, b) => a.date - b.date);
    }

    function renderCalendar() {
        const grid = $('#calendar');
        if (!grid) return;
        grid.replaceChildren();
        const view = calendar.view;
        $('#cal-title span').textContent = view.toLocaleDateString([], { month: 'long', year: 'numeric' });

        // Weeks start on Monday.
        const start = new Date(view);
        start.setDate(1 - ((view.getDay() + 6) % 7));
        const weekdayBase = new Date(2024, 0, 1); // a Monday
        for (let i = 0; i < 7; i++) {
            const d = new Date(weekdayBase); d.setDate(1 + i);
            grid.appendChild(el('div', { class: 'cal-weekday', text: d.toLocaleDateString([], { weekday: 'short' }) }));
        }

        const todayKey = dayKey(new Date());
        const selectedKey = dayKey(calendar.selected);
        for (let i = 0; i < 42; i++) {
            const day = new Date(start); day.setDate(start.getDate() + i);
            const key = dayKey(day);
            const items = itemsOn(key);
            const upcoming = items.filter(it => !it.posted).length;
            const posted = items.length - upcoming;
            const classes = ['cal-day'];
            if (day.getMonth() !== view.getMonth()) classes.push('other-month');
            if (key === todayKey) classes.push('today');
            if (key === selectedKey) classes.push('selected');
            const label = day.toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric' }) +
                (upcoming ? `, ${upcoming} upcoming` : '') + (posted ? `, ${posted} posted` : '');
            const chips = [];
            if (upcoming) chips.push(el('span', { class: 'cal-chip upcoming' }, [el('span', { class: 'cal-dot upcoming' }), String(upcoming)]));
            if (posted) chips.push(el('span', { class: 'cal-chip posted' }, [el('span', { class: 'cal-dot posted' }), String(posted)]));
            const cell = el('button', { type: 'button', class: classes.join(' '), 'aria-label': label, 'aria-pressed': key === selectedKey ? 'true' : 'false' },
                [el('span', { class: 'cal-num', text: String(day.getDate()) }), el('span', { class: 'cal-chips' }, chips)]);
            cell.addEventListener('click', () => {
                calendar.selected = day;
                if (day.getMonth() !== view.getMonth()) calendar.view = new Date(day.getFullYear(), day.getMonth(), 1);
                renderCalendar();
            });
            grid.appendChild(cell);
        }
        renderDayList();
    }

    function renderDayList() {
        const list = $('#day-list');
        if (!list) return;
        const day = calendar.selected;
        $('#day-title span').textContent = day.toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric' });
        list.replaceChildren();
        const items = itemsOn(dayKey(day));
        if (!items.length) {
            list.appendChild(el('p', { class: 'empty-message', text: 'Nothing scheduled on this day.' }));
            return;
        }
        items.forEach(item => {
            const time = item.date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
            const meta = el('div', { class: 'history-meta' }, [
                el('span', { class: 'timestamp', text: time }),
                el('span', { class: 'type-badge ' + (item.posted ? 'type-posted' : 'type-upcoming'), text: item.posted ? 'posted' : 'upcoming' }),
            ]);
            const body = el('div', { class: 'tweet-body' }, [meta, el('p', { text: item.text })]);
            const row = el('div', { class: 'tweet-item' }, [body]);
            if (!item.posted) {
                const edit = el('button', { type: 'button', class: 'btn btn-secondary btn-small btn-icon js-edit-scheduled', title: 'Edit', 'data-sid': item.id, 'data-text': item.text, 'data-iso': item.at },
                    [el('i', { class: 'fa-solid fa-pencil' })]);
                const del = el('form', { action: `/scheduled/delete/${item.id}`, method: 'POST' },
                    [el('button', { type: 'submit', class: 'btn btn-danger btn-small btn-icon', title: 'Cancel' }, [el('i', { class: 'fa-solid fa-trash' })])]);
                del.addEventListener('submit', e => { if (!confirm('Cancel this scheduled tweet?')) e.preventDefault(); });
                row.appendChild(el('div', { class: 'row-actions' }, [edit, del]));
            }
            list.appendChild(row);
        });
    }

    function initCalendar() {
        if (!$('#calendar')) return;
        $('#cal-prev').addEventListener('click', () => { calendar.view = new Date(calendar.view.getFullYear(), calendar.view.getMonth() - 1, 1); renderCalendar(); });
        $('#cal-next').addEventListener('click', () => { calendar.view = new Date(calendar.view.getFullYear(), calendar.view.getMonth() + 1, 1); renderCalendar(); });
        $('#cal-today').addEventListener('click', () => {
            const now = new Date();
            calendar.view = new Date(now.getFullYear(), now.getMonth(), 1);
            calendar.selected = now;
            renderCalendar();
        });
        $('#day-schedule-btn').addEventListener('click', () => openScheduleModal(calendar.selected));
        renderCalendar();
    }

    /* ------------------------------------------------------------------ */

    document.addEventListener('DOMContentLoaded', () => {
        initTabs();
        initAlerts();
        initModals();
        initMenus();
        initForms();
        initPreviews();
        initCalendar();
        renderTimestamps();
        tickCountdowns();
        setInterval(tickCountdowns, 1000);
    });
})();
