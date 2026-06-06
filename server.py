#!/usr/bin/env python3
"""
YT-DL Web UI
Run: python3 server.py
Open: http://localhost:9876
"""

import http.server
import json
import os
import subprocess
import threading
import time
from socketserver import ThreadingMixIn

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_SCRIPT = os.path.join(SCRIPT_DIR, 'download.sh')
LINKS_FILE = os.path.join(SCRIPT_DIR, 'links.txt')
OUTPUT_DIR = os.path.expanduser('~/Downloads/yt-dl')
PORT = 9876

_active_proc: subprocess.Popen | None = None
_cancel_flag = threading.Event()
_proc_lock = threading.Lock()


def read_links_file():
    """Return non-empty lines from links.txt, or empty list."""
    try:
        with open(LINKS_FILE, 'r') as f:
            return [l.strip() for l in f if l.strip()]
    except FileNotFoundError:
        return []



HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>YT-DL</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0 }

  :root {
    --bg:         #0a0a0a;
    --bg-card:    #161616;
    --bg-input:   #1a1a1a;
    --bg-tabs:    #0f0f0f;
    --bg-tab-act: #252525;
    --border:     #2a2a2a;
    --border-focus: #444;
    --text:       #d0d0d0;
    --text-strong:#f0f0f0;
    --text-head:  #fff;
    --text-dim:   #666;
    --text-muted: #333;
    --placeholder:#3a3a3a;
    --shadow:     0 24px 64px #00000080;
    --btn-dis-bg: #1e1e1e;
    --btn-dis-fg: #444;
    --btn-pri-bg: #f2994a;
    --btn-pri-fg: #1a1206;
    --btn-pri-hov:#f4a862;
    --log-thumb:  #2e2e2e;
    --log-dim:    #555;
  }
  .light {
    --bg:         #f5f5f5;
    --bg-card:    #fff;
    --bg-input:   #fafafa;
    --bg-tabs:    #efefef;
    --bg-tab-act: #fff;
    --border:     #e0e0e0;
    --border-focus:#bbb;
    --text:       #444;
    --text-strong:#111;
    --text-head:  #0a0a0a;
    --text-dim:   #aaa;
    --text-muted: #ccc;
    --placeholder:#ccc;
    --shadow:     0 24px 64px #00000018;
    --btn-dis-bg: #e8e8e8;
    --btn-dis-fg: #aaa;
    --btn-pri-bg: var(--text-head);
    --btn-pri-fg: var(--bg);
    --btn-pri-hov:var(--text-head);
    --log-thumb:  #ddd;
    --log-dim:    #bbb;
  }

  body {
    background: var(--bg);
    color: var(--text);
    font: 13px/1.6 -apple-system, 'Inter', 'SF Pro Display', system-ui, sans-serif;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 40px 20px;
    transition: background .2s, color .2s;
  }

  /* ── Theme toggle ── */
  .theme-toggle {
    position: fixed; top: 18px; right: 18px;
    background: var(--bg-card); border: 1px solid var(--border);
    color: var(--text-dim); width: 36px; height: 36px;
    border-radius: 10px; cursor: pointer; font-size: 16px;
    display: flex; align-items: center; justify-content: center;
    transition: all .15s; z-index: 10;
  }
  .theme-toggle:hover { color: var(--text-strong); border-color: var(--border-focus) }

  /* ── Logo / hero ── */
  .hero {
    text-align: center;
    margin-bottom: 40px;
    user-select: none;
  }
  .logo {
    width: 52px; height: 52px;
    background: var(--text-head);
    border-radius: 14px;
    display: inline-flex; align-items: center; justify-content: center;
    margin-bottom: 16px;
    box-shadow: 0 0 0 1px #ffffff10, 0 8px 32px #00000060;
  }
  .logo svg { width: 26px; height: 26px }
  .hero h1 {
    font-size: 22px; font-weight: 600; color: var(--text-head);
    letter-spacing: -.02em; margin-bottom: 6px;
  }
  .hero p { font-size: 13px; color: var(--text-dim); letter-spacing: .01em }

  /* ── Card ── */
  .card {
    width: 100%; max-width: 560px;
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 24px;
    box-shadow: 0 0 0 1px #ffffff06, var(--shadow);
    transition: background .2s, border-color .2s;
  }

  /* ── Tabs ── */
  .tabs {
    display: flex; gap: 4px;
    background: var(--bg-tabs);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 4px;
    margin-bottom: 20px;
    transition: background .2s;
  }
  .tab {
    flex: 1; background: none; border: none;
    color: var(--text-dim); padding: 7px 12px;
    border-radius: 7px; font: inherit; font-size: 12px; font-weight: 500;
    cursor: pointer; transition: all .15s; letter-spacing: .01em;
  }
  .tab.active { background: var(--bg-tab-act); color: var(--text-strong); box-shadow: 0 1px 4px #00000018 }
  .tab:hover:not(.active) { color: var(--text) }

  /* ── Panel ── */
  .panel { display: none }
  .panel.active { display: flex; flex-direction: column; gap: 10px }

  /* ── Inputs ── */
  textarea, input[type=text], select {
    width: 100%;
    background: var(--bg-input);
    border: 1px solid var(--border);
    color: var(--text-strong);
    border-radius: 10px;
    font: inherit; font-size: 13px;
    outline: none;
    transition: border-color .15s, background .2s;
  }
  textarea:focus, input[type=text]:focus, select:focus { border-color: var(--border-focus) }
  textarea::placeholder, input[type=text]::placeholder { color: var(--placeholder) }

  textarea {
    padding: 12px 14px; resize: vertical; min-height: 96px;
    line-height: 1.6;
  }
  input[type=text] { padding: 10px 14px }
  select {
    padding: 10px 14px; cursor: pointer; appearance: none;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%23888' d='M6 8L1 3h10z'/%3E%3C/svg%3E");
    background-repeat: no-repeat;
    background-position: right 12px center;
  }

  /* ── Badge ── */
  .badge {
    display: none; align-self: flex-start;
    font-size: 11px; font-weight: 500;
    background: #f2994a18; color: #f2994a;
    border: 1px solid #f2994a30; border-radius: 6px;
    padding: 3px 10px; letter-spacing: .02em;
  }
  .badge.visible { display: inline-block }

  /* ── Button row ── */
  .actions { display: flex; gap: 8px; align-items: stretch }

  .btn-primary {
    flex: 1; background: var(--btn-pri-bg); color: var(--btn-pri-fg); border: none;
    padding: 10px 20px; border-radius: 10px; font: inherit;
    font-size: 13px; font-weight: 600; cursor: pointer;
    transition: background .15s, opacity .15s, transform .1s;
    letter-spacing: .01em;
  }
  .btn-primary:hover:not(:disabled) { background: var(--btn-pri-hov); opacity: .88 }
  .btn-primary:active:not(:disabled) { transform: scale(.98) }
  .btn-primary:disabled { background: var(--btn-dis-bg); color: var(--btn-dis-fg); cursor: not-allowed }

  .btn-ghost {
    flex: none; background: transparent; border: 1px solid var(--border);
    color: var(--text-dim); padding: 10px 14px; border-radius: 10px;
    font: inherit; font-size: 13px; cursor: pointer;
    transition: all .15s; display: none;
  }
  .btn-ghost.visible, .btn-ghost.show { display: block }
  .btn-ghost:hover { color: var(--text-strong); border-color: var(--border-focus) }

  .btn-danger {
    flex: none; background: transparent; border: 1px solid #eb575730;
    color: #eb5757; padding: 10px 14px; border-radius: 10px;
    font: inherit; font-size: 13px; cursor: pointer;
    transition: all .15s; display: none;
  }
  .btn-danger.visible, .btn-danger.show { display: block }
  .btn-danger:hover:not(:disabled) { background: #eb575712; border-color: #eb5757 }
  .btn-danger:disabled { opacity: .4; cursor: not-allowed }

  /* ── Log ── */
  .log {
    margin-top: 16px; width: 100%; max-width: 560px;
    background: var(--bg-input);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 14px 16px;
    height: 280px; overflow-y: auto;
    font: 11.5px/1.8 'SF Mono', ui-monospace, monospace;
    white-space: pre-wrap; word-break: break-all;
    display: none;
    transition: background .2s, border-color .2s;
  }
  .log.visible { display: block }
  .log::-webkit-scrollbar { width: 3px }
  .log::-webkit-scrollbar-thumb { background: var(--log-thumb); border-radius: 2px }

  .l-log    { color: var(--log-dim) }
  .l-start  { color: #7eb8f7; font-weight: 600; margin-top: 4px }
  .l-done   { color: #6fcf97; font-weight: 600 }
  .l-failed { color: #eb5757; font-weight: 600 }
  .l-info   { color: #f2994a }
  .l-proxy  { color: #bb87fc }

  /* ── Footer ── */
  .footer {
    margin-top: 28px; font-size: 11px; color: var(--text-muted);
    letter-spacing: .03em;
  }
</style>
</head>
<body>

<button class="theme-toggle" id="theme-toggle" onclick="toggleTheme()" title="Toggle theme">🌙</button>

<div class="hero">
  <div class="logo">
    <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 14.5v-9l6 4.5-6 4.5z" fill="var(--bg)"/>
    </svg>
  </div>
  <h1>YT-DL</h1>
  <p>Download videos, audio, and files</p>
</div>

<div class="card">
  <div class="tabs">
    <button class="tab active" onclick="switchTab('yt')">Video / Audio</button>
    <button class="tab" onclick="switchTab('curl')">Direct Download</button>
  </div>

  <div class="panel active" id="panel-yt">
    <textarea id="urls" placeholder="Paste URLs — one per line or comma-separated"></textarea>
    <span class="badge" id="badge"></span>
    <select id="quality">
      <option value="best">Best quality</option>
      <option value="1080">1080p</option>
      <option value="720">720p</option>
      <option value="480">480p</option>
      <option value="audio">Audio only (MP3)</option>
    </select>
    <div class="actions">
      <button class="btn-primary" id="dl-btn" onclick="startDownload()">Download</button>
      <button class="btn-danger" id="cancel-btn" onclick="cancelDownload()">Cancel</button>
      <button class="btn-ghost" id="open-btn" onclick="openFolder()">Open folder</button>
    </div>
  </div>

  <div class="panel" id="panel-curl">
    <input type="text" id="curl-url" placeholder="https://…" />
    <input type="text" id="curl-filename" placeholder="Filename (leave blank to auto-detect)" />
    <div class="actions">
      <button class="btn-primary" id="curl-dl-btn" onclick="startCurlDownload()">Download</button>
      <button class="btn-danger" id="curl-cancel-btn" onclick="cancelDownload()">Cancel</button>
      <button class="btn-ghost" id="curl-open-btn" onclick="openFolder()">Open folder</button>
    </div>
  </div>
</div>

<div class="log" id="log"></div>

<div class="footer">~/Downloads/yt-dl</div>

<script>
// Theme
const toggleBtn = document.getElementById('theme-toggle');
function applyTheme(light) {
  document.body.classList.toggle('light', light);
  toggleBtn.textContent = light ? '🌙' : '☀️';
}
function toggleTheme() {
  const light = !document.body.classList.contains('light');
  localStorage.setItem('theme', light ? 'light' : 'dark');
  applyTheme(light);
}
applyTheme(localStorage.getItem('theme') === 'light');

const log      = document.getElementById('log');
const btn      = document.getElementById('dl-btn');
const cancelB  = document.getElementById('cancel-btn');
const openB    = document.getElementById('open-btn');
const badge    = document.getElementById('badge');
const textarea = document.getElementById('urls');

fetch('/links').then(r => r.json()).then(({ urls }) => {
  if (urls.length) {
    textarea.value = urls.join('\n');
    badge.textContent = `links.txt · ${urls.length} URL${urls.length > 1 ? 's' : ''}`;
    badge.classList.add('visible');
  }
});

textarea.addEventListener('input', () => badge.classList.remove('visible'));

function appendLine(text, cls) {
  const d = document.createElement('div');
  d.className = 'l-' + cls;
  d.textContent = text;
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  log.appendChild(d);
  if (atBottom) log.scrollTop = log.scrollHeight;
}

function classifyLine(text) {
  if (/^Proxy:/i.test(text)) return 'proxy';
  if (/^(Downloading|Quality|Output):/.test(text)) return 'info';
  return 'log';
}

function switchTab(name) {
  document.querySelectorAll('.tab').forEach((t, i) => t.classList.toggle('active', ['yt','curl'][i] === name));
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.getElementById('panel-' + name).classList.add('active');
}

function streamSSE(endpoint, body, handlers) {
  fetch(endpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  }).then(res => {
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    function read() {
      reader.read().then(({ done, value }) => {
        if (done) { handlers.done?.(); return; }
        buf += dec.decode(value, { stream: true });
        const parts = buf.split('\n\n');
        buf = parts.pop();
        for (const part of parts) {
          if (!part.startsWith('data: ')) continue;
          try { handlers.event?.(JSON.parse(part.slice(6))); } catch(e) {}
        }
        read();
      });
    }
    read();
  }).catch(err => {
    appendLine('Error: ' + err.message, 'failed');
    handlers.done?.();
  });
}

function startDownload() {
  const raw = textarea.value.trim();
  if (!raw) return;
  const urls = raw.split(/[\n,]+/).map(u => u.trim()).filter(Boolean);
  const quality = document.getElementById('quality').value;

  log.innerHTML = '';
  log.classList.add('visible');
  btn.disabled = true;
  cancelB.classList.add('visible');
  cancelB.disabled = false;
  openB.classList.remove('visible');
  badge.classList.remove('visible');

  streamSSE('/download', { urls: urls.join('\n'), quality }, {
    event(ev) {
      if (ev.type === 'start') {
        appendLine(`▶  [${ev.index}/${ev.total}]  ${ev.url}`, 'start');
      } else if (ev.type === 'log') {
        if (ev.text) appendLine(ev.text, classifyLine(ev.text));
      } else if (ev.type === 'done') {
        appendLine('✓  done', 'done');
      } else if (ev.type === 'failed') {
        appendLine('✗  failed', 'failed');
      } else if (ev.type === 'cancelled') {
        appendLine('⊘  cancelled', 'failed');
        btn.disabled = false; cancelB.classList.remove('visible');
      } else if (ev.type === 'all_done') {
        appendLine('─── finished ───', 'info');
        btn.disabled = false; cancelB.classList.remove('visible');
        openB.classList.add('visible');
        if (ev.failed_count > 0) {
          fetch('/links').then(r => r.json()).then(({ urls }) => {
            textarea.value = urls.join('\n');
            badge.textContent = `${ev.failed_count} failed — retry?`;
            badge.style.cssText = 'background:#eb575718;color:#eb5757;border-color:#eb575730';
            badge.classList.add('visible');
          });
        } else {
          textarea.value = '';
        }
      }
    },
    done() { btn.disabled = false; cancelB.classList.remove('visible'); }
  });
}

function startCurlDownload() {
  const url = document.getElementById('curl-url').value.trim();
  if (!url) return;
  const filename = document.getElementById('curl-filename').value.trim();

  log.innerHTML = '';
  log.classList.add('visible');
  document.getElementById('curl-dl-btn').disabled = true;
  document.getElementById('curl-cancel-btn').classList.add('show');
  document.getElementById('curl-open-btn').classList.remove('show');

  streamSSE('/curl-download', { url, filename }, {
    event(ev) {
      if (ev.type === 'start') {
        appendLine(`▶  ${ev.url}`, 'start');
      } else if (ev.type === 'log') {
        if (ev.text) appendLine(ev.text, 'log');
      } else if (ev.type === 'done') {
        appendLine('✓  done', 'done');
        document.getElementById('curl-dl-btn').disabled = false;
        document.getElementById('curl-cancel-btn').classList.remove('show');
        document.getElementById('curl-open-btn').classList.add('show');
      } else if (ev.type === 'failed') {
        appendLine('✗  failed', 'failed');
        document.getElementById('curl-dl-btn').disabled = false;
        document.getElementById('curl-cancel-btn').classList.remove('show');
      } else if (ev.type === 'cancelled') {
        appendLine('⊘  cancelled', 'failed');
        document.getElementById('curl-dl-btn').disabled = false;
        document.getElementById('curl-cancel-btn').classList.remove('show');
      }
    },
    done() {
      document.getElementById('curl-dl-btn').disabled = false;
      document.getElementById('curl-cancel-btn').classList.remove('show');
    }
  });
}

function cancelDownload() {
  document.getElementById('cancel-btn').disabled = true;
  document.getElementById('curl-cancel-btn').disabled = true;
  fetch('/cancel', { method: 'POST' });
}

function openFolder() {
  fetch('/open-folder', { method: 'POST' });
}

textarea.addEventListener('keydown', e => {
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') startDownload();
});
document.getElementById('curl-url').addEventListener('keydown', e => {
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') startCurlDownload();
});
</script>
</body>
</html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == '/':
            body = HTML.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == '/links':
            body = json.dumps({'urls': read_links_file()}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        global _active_proc, _cancel_flag
        if self.path == '/open-folder':
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            subprocess.Popen(['open', OUTPUT_DIR])
            self.send_response(204)
            self.end_headers()
            return

        if self.path == '/cancel':
            _cancel_flag.set()
            with _proc_lock:
                if _active_proc and _active_proc.poll() is None:
                    _active_proc.terminate()
            self.send_response(204)
            self.end_headers()
            return

        if self.path == '/curl-download':
            length = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(length))
            url = data.get('url', '').strip()
            filename = data.get('filename', '').strip()

            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('X-Accel-Buffering', 'no')
            self.end_headers()

            def send(msg):
                try:
                    self.wfile.write(f'data: {json.dumps(msg)}\n\n'.encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

            os.makedirs(OUTPUT_DIR, exist_ok=True)
            _cancel_flag.clear()
            send({'type': 'start', 'url': url})

            if not filename:
                # derive from URL path, fallback to 'download'
                filename = url.rstrip('/').split('/')[-1].split('?')[0] or 'download'

            out_path = os.path.join(OUTPUT_DIR, filename)
            STALL_SECONDS = 60
            MAX_RETRIES = 20

            def do_curl():
                if os.path.exists(out_path):
                    os.remove(out_path)
                return subprocess.Popen(
                    ['curl', '-L', '-s', '-o', out_path, url],
                    env={**os.environ, 'TERM': 'dumb'}
                )

            try:
                for attempt in range(1, MAX_RETRIES + 1):
                    proc = do_curl()
                    with _proc_lock:
                        _active_proc = proc

                    start_time = time.time()
                    last_change_time = start_time
                    last_size = -1
                    stalled = False

                    while proc.poll() is None:
                        if _cancel_flag.is_set():
                            proc.terminate()
                            break
                        time.sleep(1)
                        try:
                            size = os.path.getsize(out_path)
                        except FileNotFoundError:
                            size = 0
                        if size != last_size:
                            last_size = size
                            last_change_time = time.time()
                        elif time.time() - last_change_time > STALL_SECONDS:
                            proc.terminate()
                            stalled = True
                            break
                        elapsed = max(time.time() - start_time, 0.001)
                        speed = size / elapsed
                        send({'type': 'log', 'text': f'{size / 1_048_576:.2f} MB   avg {speed / 1_048_576:.2f} MB/s'})

                    proc.wait()
                    with _proc_lock:
                        _active_proc = None

                    if _cancel_flag.is_set():
                        send({'type': 'cancelled'})
                        break

                    if stalled:
                        send({'type': 'log', 'text': f'Stalled — retrying ({attempt}/{MAX_RETRIES})...'})
                        continue

                    if proc.returncode == 0:
                        try:
                            final = os.path.getsize(out_path)
                            send({'type': 'log', 'text': f'Total: {final / 1_048_576:.2f} MB'})
                        except FileNotFoundError:
                            pass
                        send({'type': 'done'})
                        break

                    # non-zero exit — if partial file exists try to resume, else retry fresh
                    send({'type': 'log', 'text': f'Error (attempt {attempt}/{MAX_RETRIES}), retrying...'})

                else:
                    send({'type': 'failed'})

            except Exception as e:
                send({'type': 'failed', 'text': str(e)})
            return

        if self.path != '/download':
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get('Content-Length', 0))
        data = json.loads(self.rfile.read(length))
        urls = [u.strip() for u in data.get('urls', '').split('\n') if u.strip()]
        quality = data.get('quality', 'best')

        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()

        def send(msg):
            try:
                self.wfile.write(f'data: {json.dumps(msg)}\n\n'.encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        _cancel_flag.clear()

        failed_urls = []
        cancelled = False

        for i, url in enumerate(urls):
            if _cancel_flag.is_set():
                cancelled = True
                break
            send({'type': 'start', 'url': url, 'index': i + 1, 'total': len(urls)})
            try:
                proc = subprocess.Popen(
                    [DOWNLOAD_SCRIPT, url, quality, OUTPUT_DIR],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env={**os.environ, 'TERM': 'dumb'},
                )
                with _proc_lock:
                    _active_proc = proc
                for line in proc.stdout:
                    if _cancel_flag.is_set():
                        proc.terminate()
                        break
                    send({'type': 'log', 'text': line.rstrip()})
                proc.wait()
                with _proc_lock:
                    _active_proc = None
                if _cancel_flag.is_set():
                    cancelled = True
                    break
                if proc.returncode == 0:
                    send({'type': 'done', 'url': url})
                else:
                    failed_urls.append(url)
                    send({'type': 'failed', 'url': url})
            except Exception as e:
                failed_urls.append(url)
                send({'type': 'failed', 'url': url, 'text': str(e)})

        if cancelled:
            send({'type': 'cancelled'})
            return

        # Write back only failed URLs; clear file if all succeeded
        with open(LINKS_FILE, 'w') as f:
            if failed_urls:
                f.write('\n'.join(failed_urls) + '\n')

        send({'type': 'all_done', 'failed_count': len(failed_urls)})


class ThreadedHTTPServer(ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


if __name__ == '__main__':
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print(f'  YT-DL  →  http://localhost:{PORT}')
    server = ThreadedHTTPServer(('127.0.0.1', PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopped.')
