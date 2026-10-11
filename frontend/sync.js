// Sync credentials remain in this page's memory until saved to the backend.
(() => {
    'use strict';
    let saving = false;
    let running = false;
    let statusLoading = false;
    const element = (id) => document.getElementById(id);

    async function request(path, options = {}) {
        const response = await fetch(`/api/sync/${path}`, { cache: 'no-store', ...options });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            if (response.status === 401) throw new Error('登录已过期，请刷新页面重新登录。');
            throw new Error(typeof data.detail === 'string' ? data.detail : '连接未完成，请稍后重试。');
        }
        return data;
    }

    function displayConfig(config) {
        element('syncRemoteUrl').value = config.remote_url || '';
        element('syncEnabled').checked = Boolean(config.enabled);
        element('syncToken').value = '';
        element('syncToken').placeholder = config.has_token ? '已保存；留空沿用原口令' : '填写服务器的连接口令';
        element('syncTokenHint').textContent = config.has_token
            ? '连接口令已保存在当前服务；留空保存会沿用。'
            : '只保存在当前服务，不写入浏览器。';
    }

    function displayStatus(status) {
        const lines = [];
        running = Boolean(status.running);
        if (running) lines.push('正在同步资料…');
        else if (status.enabled) lines.push('自动同步已开启');
        else lines.push('自动同步未开启；本机可独立使用');
        const lastSync = status.last_success || status.last_sync;
        if (lastSync) {
            const time = new Date(typeof lastSync === 'number' ? lastSync * 1000 : lastSync);
            if (!Number.isNaN(time.getTime())) lines.push(`上次完成：${time.toLocaleString('zh-CN')}`);
        }
        if (status.last_error) lines.push(`等待恢复：${status.last_error}`);
        else if (status.message) lines.push(status.message);
        element('syncStatus').textContent = lines.join('\n');
        element('syncRunBtn').disabled = running || saving;
    }

    async function refreshStatus() {
        if (statusLoading) return;
        statusLoading = true;
        try {
            displayStatus(await request('status'));
        } catch (error) {
            element('syncStatus').textContent = error.message;
        } finally {
            statusLoading = false;
        }
    }

    async function saveConfig() {
        if (saving) return;
        saving = true;
        element('syncSaveBtn').disabled = true;
        element('syncRunBtn').disabled = true;
        try {
            const payload = {
                remote_url: element('syncRemoteUrl').value.trim(),
                enabled: element('syncEnabled').checked
            };
            const token = element('syncToken').value.trim();
            if (token) payload.token = token;
            const config = await request('config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            displayConfig(config);
            element('syncStatus').textContent = '连接设置已保存。';
            await refreshStatus();
        } catch (error) {
            element('syncStatus').textContent = `保存失败：${error.message}`;
        } finally {
            saving = false;
            element('syncSaveBtn').disabled = false;
            element('syncRunBtn').disabled = running;
        }
    }

    async function syncNow() {
        if (running || saving) return;
        running = true;
        element('syncRunBtn').disabled = true;
        element('syncStatus').textContent = '正在连接并同步资料…';
        try {
            await request('run', { method: 'POST' });
            await refreshStatus();
            if (typeof loadRecentTasks === 'function') loadRecentTasks(true);
        } catch (error) {
            running = false;
            element('syncStatus').textContent = `同步未完成：${error.message}`;
        } finally {
            element('syncRunBtn').disabled = running;
        }
    }

    document.addEventListener('DOMContentLoaded', async () => {
        element('syncSaveBtn').addEventListener('click', saveConfig);
        element('syncRunBtn').addEventListener('click', syncNow);
        try {
            displayConfig(await request('config'));
            await refreshStatus();
        } catch (error) {
            element('syncStatus').textContent = error.message;
        }
        window.setInterval(refreshStatus, 10000);
    });
})();
