/* Create and Engage tabs. Plain DOM, same conventions as app.js. */
(function () {
    'use strict';

    const $ = (sel, root = document) => root.querySelector(sel);
    const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

    function el(tag, attrs = {}, children = []) {
        const node = document.createElement(tag);
        for (const [key, value] of Object.entries(attrs)) {
            if (value === null || value === undefined || value === false) continue;
            if (key === 'class') node.className = value;
            else if (key === 'text') node.textContent = value;
            else node.setAttribute(key, value === true ? '' : value);
        }
        for (const child of [].concat(children)) {
            if (child) node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
        }
        return node;
    }

    async function call(url, body, method = 'POST') {
        const options = { method, headers: {} };
        if (body instanceof FormData) options.body = body;
        else if (body !== undefined) {
            options.headers['Content-Type'] = 'application/json';
            options.body = JSON.stringify(body);
        }
        let response, data = {};
        try {
            response = await fetch(url, options);
            data = await response.json();
        } catch (e) {
            throw new Error('The server did not answer. Try again.');
        }
        if (!response.ok || data.error) throw new Error(data.error || `Request failed (${response.status}).`);
        return data;
    }

    function busy(button, on) {
        if (on) { button.dataset.was = button.disabled ? '1' : ''; button.disabled = true; }
        else button.disabled = button.dataset.was === '1';
        button.classList.toggle('is-busy', on);
    }

    function say(node, text, isError) {
        node.textContent = text;
        node.classList.toggle('error-text', !!isError);
    }

    /* ------------------------------------------------------------------ *
     * Composer and drafts
     * ------------------------------------------------------------------ */

    const composer = $('#studio-text');

    function useText(text) {
        composer.value = text;
        composer.dispatchEvent(new Event('input'));
        composer.focus();
    }

    function renderDrafts(posts) {
        const list = $('#ai-drafts');
        list.replaceChildren(...posts.map(text => {
            const area = el('textarea', { rows: '3', 'aria-label': 'Draft' });
            area.value = text;
            const count = el('span', { class: 'count', text: `${text.length}/280` });
            area.addEventListener('input', () => { count.textContent = `${area.value.length}/280`; });
            const use = el('button', { type: 'button', class: 'btn btn-primary btn-small', text: 'Use this' });
            use.addEventListener('click', () => useText(area.value.trim()));
            const drop = el('button', { type: 'button', class: 'btn btn-secondary btn-small', text: 'Discard' });
            const row = el('div', { class: 'draft' }, [area, el('div', { class: 'draft-actions' }, [count, drop, use])]);
            drop.addEventListener('click', () => row.remove());
            return row;
        }));
    }

    function initCompose() {
        const button = $('#ai-compose-btn'), status = $('#ai-compose-status');
        button.addEventListener('click', async () => {
            const brief = $('#ai-brief').value.trim();
            if (!brief) { say(status, 'Say what the posts should be about.', true); return; }
            busy(button, true);
            say(status, 'Writing drafts...');
            try {
                const data = await call('/ai/compose', { brief, count: Number($('#ai-count').value) });
                renderDrafts(data.posts);
                say(status, `${data.posts.length} draft(s). Edit them freely, then pick one.`);
            } catch (e) {
                say(status, e.message, true);
            } finally {
                busy(button, false);
            }
        });
    }

    /* ------------------------------------------------------------------ *
     * Media library
     * ------------------------------------------------------------------ */

    let media = JSON.parse($('#media-data').textContent || '[]');
    let attachedId = null;      // file attached to the composer
    let pollTimer = null;

    function attach(item) {
        attachedId = item ? item.id : null;
        $('#studio-media-id').value = attachedId || '';
        const box = $('#studio-attachment');
        box.hidden = !item;
        box.replaceChildren();
        if (item) {
            const remove = el('button', { type: 'button', class: 'btn btn-secondary btn-small', text: 'Remove' });
            remove.addEventListener('click', () => { attach(null); renderMedia(); });
            box.append(preview(item), el('span', { text: item.kind === 'video' ? 'Video attached' : 'Image attached' }), remove);
        }
    }

    function preview(item) {
        if (item.kind === 'video') return el('video', { src: item.url, muted: true, controls: true, preload: 'metadata' });
        return el('img', { src: item.url, alt: item.prompt || 'Uploaded image', loading: 'lazy' });
    }

    function renderMedia() {
        const grid = $('#media-grid');
        $('#media-empty').hidden = media.length > 0;
        grid.replaceChildren(...media.map(item => {
            const tile = el('div', { class: 'media-tile' + (item.id === attachedId ? ' selected' : '') });
            if (item.status === 'ready') {
                tile.appendChild(preview(item));
                const use = el('button', { type: 'button', class: 'btn btn-primary btn-small',
                                           text: item.id === attachedId ? 'Attached' : 'Attach' });
                use.addEventListener('click', () => { attach(item.id === attachedId ? null : item); renderMedia(); });
                tile.appendChild(el('div', { class: 'media-actions' }, [use, deleteButton(item)]));
            } else if (item.status === 'pending') {
                tile.appendChild(el('div', { class: 'media-wait' }, [
                    el('i', { class: 'fa-solid fa-spinner fa-spin', 'aria-hidden': 'true' }),
                    el('span', { text: item.kind === 'video' ? 'Making video. This can take minutes.' : 'Making image...' }),
                ]));
            } else {
                tile.appendChild(el('div', { class: 'media-wait error-text' }, [el('span', { text: item.error || 'Failed.' })]));
                tile.appendChild(el('div', { class: 'media-actions' }, [deleteButton(item)]));
            }
            if (item.prompt) tile.title = item.prompt;
            return tile;
        }));
        const waiting = media.some(item => item.status === 'pending');
        if (waiting && !pollTimer) pollTimer = setInterval(refreshMedia, 4000);
        if (!waiting && pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    }

    function deleteButton(item) {
        const button = el('button', { type: 'button', class: 'btn btn-ghost-danger btn-small', text: 'Delete' });
        button.addEventListener('click', async () => {
            if (!confirm('Delete this file? It is removed from any queued post too.')) return;
            try {
                await call(`/media/${item.id}/delete`);
                if (attachedId === item.id) attach(null);
                media = media.filter(m => m.id !== item.id);
                renderMedia();
            } catch (e) { alert(e.message); }
        });
        return button;
    }

    async function refreshMedia() {
        try {
            media = (await call('/api/media', undefined, 'GET')).media;
            renderMedia();
        } catch (e) { /* keep the current view; the next poll retries */ }
    }

    function initMedia() {
        const button = $('#media-generate-btn'), status = $('#media-status');
        button.addEventListener('click', async () => {
            const prompt = $('#media-prompt').value.trim();
            if (!prompt) { say(status, 'Describe what you want first.', true); return; }
            const choice = $('#media-provider').value;
            const aspect = $('#media-aspect').value;
            busy(button, true);
            try {
                let data;
                if (choice === 'imagineart-video') {
                    const source = media.find(m => m.id === attachedId && m.kind === 'image');
                    data = await call('/ai/video', { prompt, aspect, image_id: source ? source.id : null });
                } else {
                    data = await call('/ai/image', { prompt, aspect, provider: choice });
                }
                media.unshift(data.media);
                renderMedia();
                say(status, 'Started. It appears in the library when ready.');
            } catch (e) {
                say(status, e.message, true);
            } finally {
                busy(button, false);
            }
        });

        $('#media-file').addEventListener('change', async e => {
            const file = e.target.files[0];
            if (!file) return;
            const form = new FormData();
            form.append('file', file);
            say(status, 'Uploading...');
            try {
                const data = await call('/media/upload', form);
                media.unshift(data.media);
                attach(data.media);
                renderMedia();
                say(status, 'Uploaded and attached to the post.');
            } catch (err) {
                say(status, err.message, true);
            }
            e.target.value = '';
        });

        // Pick the first provider that has a key.
        const provider = $('#media-provider');
        const firstUsable = $$('option', provider).find(o => !o.disabled);
        if (firstUsable) provider.value = firstUsable.value;
        renderMedia();
    }

    /* ------------------------------------------------------------------ *
     * Engage
     * ------------------------------------------------------------------ */

    function intentLinks(card) {
        const id = card.dataset.id, author = card.dataset.author || 'i';
        const text = $('.engage-draft', card).value.trim();
        const reply = new URLSearchParams({ in_reply_to: id });
        if (text) reply.set('text', text);
        $('.js-reply', card).href = 'https://x.com/intent/post?' + reply;
        const quote = new URLSearchParams({ url: `https://x.com/${author}/status/${id}` });
        if (text) quote.set('text', text);
        $('.js-quote', card).href = 'https://x.com/intent/post?' + quote;
    }

    async function setStatus(card, status) {
        try {
            await call(`/engage/item/${card.dataset.id}/status`, { status, draft: $('.engage-draft', card).value });
            card.remove();
            const list = $('.engage-list');
            if (!$('.engage-item', list)) list.appendChild(el('p', { class: 'empty-message', text: 'All caught up.' }));
        } catch (e) { alert(e.message); }
    }

    function initEngage() {
        $$('.engage-item').forEach(card => {
            const draft = $('.engage-draft', card);
            intentLinks(card);
            draft.addEventListener('input', () => intentLinks(card));
            $('.js-done', card).addEventListener('click', () => setStatus(card, 'done'));
            $('.js-dismiss', card).addEventListener('click', () => setStatus(card, 'dismissed'));

            const redraft = $('.js-redraft', card);
            redraft.addEventListener('click', async () => {
                busy(redraft, true);
                try {
                    draft.value = (await call(`/engage/item/${card.dataset.id}/draft`)).draft;
                    intentLinks(card);
                } catch (e) { alert(e.message); }
                busy(redraft, false);
            });

            const send = $('.js-api-reply', card);
            if (send) send.addEventListener('click', async () => {
                const text = draft.value.trim();
                if (!text) { draft.focus(); return; }
                if (!confirm(`Send this reply to @${card.dataset.author} now?`)) return;
                busy(send, true);
                try {
                    await call(`/engage/item/${card.dataset.id}/reply`, { text });
                    card.remove();
                } catch (e) { alert(e.message); busy(send, false); }
            });
        });
    }

    function initSettings() {
        const button = $('#check-openai');
        if (!button) return;
        button.addEventListener('click', async () => {
            const status = $('#check-openai-status');
            busy(button, true);
            try {
                const models = (await call('/ai/models', undefined, 'GET')).models;
                $('#openai-models').replaceChildren(...models.map(id => el('option', { value: id })));
                say(status, `Key works. ${models.length} models available; start typing in a model field to pick one.`);
            } catch (e) {
                say(status, e.message, true);
            }
            busy(button, false);
        });
    }

    document.addEventListener('DOMContentLoaded', () => {
        initCompose();
        initMedia();
        initEngage();
        initSettings();
    });
})();
