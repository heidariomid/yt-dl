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
from socketserver import ThreadingMixIn

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_SCRIPT = os.path.join(SCRIPT_DIR, 'download.sh')
LINKS_FILE = os.path.join(SCRIPT_DIR, 'links.txt')
OUTPUT_DIR = os.path.expanduser('~/Downloads/yt-dl')
PORT = 9876


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
  * { box-sizing: border-box; margin: 0; padding: 0 }
  body {
    background: #111; color: #e0e0e0;
    font: 14px/1.6 'SF Mono', ui-monospace, monospace;
    padding: 40px 32px; max-width: 700px;
  }
  .header { display: flex; align-items: center; gap: 12px; margin-bottom: 28px }
  h1 { font-size: 16px; font-weight: 500; color: #fff; letter-spacing: .04em }
  .badge {
    font-size: 11px; background: #f2994a22; color: #f2994a;
    border: 1px solid #f2994a44; border-radius: 4px; padding: 2px 8px;
    display: none;
  }
  .badge.visible { display: inline-block }
  .form { display: flex; flex-direction: column; gap: 10px }
  textarea {
    background: #181818; border: 1px solid #2a2a2a; color: #e0e0e0;
    padding: 12px; border-radius: 6px; font: inherit; resize: vertical;
    min-height: 90px; outline: none; transition: border-color .15s;
  }
  textarea:focus { border-color: #444 }
  textarea::placeholder { color: #444 }
  .row { display: flex; gap: 8px; align-items: stretch }
  select {
    background: #181818; border: 1px solid #2a2a2a; color: #aaa;
    padding: 0 12px; border-radius: 6px; font: inherit; outline: none;
    cursor: pointer; appearance: none; min-width: 140px;
  }
  select:focus { border-color: #444 }
  button {
    flex: 1; background: #fff; color: #111; border: none;
    padding: 10px 20px; border-radius: 6px; font: inherit;
    font-weight: 600; cursor: pointer; transition: background .15s;
  }
  button:hover:not(:disabled) { background: #e0e0e0 }
  button:disabled { background: #222; color: #555; cursor: not-allowed }
  #open-btn {
    flex: none; background: transparent; color: #555; border: 1px solid #2a2a2a;
    padding: 10px 14px; font-weight: 400; display: none;
  }
  #open-btn.visible { display: block }
  #open-btn:hover { color: #aaa; border-color: #444 }
  .log {
    margin-top: 20px; background: #181818; border: 1px solid #222;
    border-radius: 6px; padding: 14px 16px; height: 340px;
    overflow-y: auto; font-size: 12px; line-height: 1.7;
    white-space: pre-wrap; word-break: break-all; display: none;
  }
  .log.visible { display: block }
  .log::-webkit-scrollbar { width: 4px }
  .log::-webkit-scrollbar-thumb { background: #333; border-radius: 2px }
  .l-log    { color: #666 }
  .l-start  { color: #7eb8f7; font-weight: 600; margin-top: 6px }
  .l-done   { color: #6fcf97; font-weight: 600 }
  .l-failed { color: #eb5757; font-weight: 600 }
  .l-info   { color: #f2994a }
  .l-proxy  { color: #bb87fc }
</style>
</head>
<body>
<div class="header">
  <h1>YT-DL</h1>
  <span class="badge" id="badge"></span>
</div>
<div class="form">
  <textarea id="urls" placeholder="Paste URLs — one per line or comma-separated"></textarea>
  <div class="row">
    <select id="quality">
      <option value="best">Best quality</option>
      <option value="1080">1080p</option>
      <option value="720">720p</option>
      <option value="480">480p</option>
      <option value="audio">Audio MP3</option>
    </select>
    <button id="dl-btn" onclick="startDownload()">Download</button>
    <button id="open-btn" onclick="openFolder()">Open folder</button>
  </div>
</div>
<div class="log" id="log"></div>

<script>
const log   = document.getElementById('log');
const btn   = document.getElementById('dl-btn');
const openB = document.getElementById('open-btn');
const badge = document.getElementById('badge');
const textarea = document.getElementById('urls');

// Load links.txt on page open
fetch('/links').then(r => r.json()).then(({ urls }) => {
  if (urls.length) {
    textarea.value = urls.join('\n');
    badge.textContent = `links.txt  ·  ${urls.length} URL${urls.length > 1 ? 's' : ''}`;
    badge.classList.add('visible');
  }
});

textarea.addEventListener('input', () => {
  badge.classList.remove('visible');
});

function appendLine(text, cls) {
  const d = document.createElement('div');
  d.className = 'l-' + cls;
  d.textContent = text;
  log.appendChild(d);
  log.scrollTop = log.scrollHeight;
}

function classifyLine(text) {
  if (/^Proxy:/i.test(text)) return 'proxy';
  if (/^(Downloading|Quality|Output):/.test(text)) return 'info';
  return 'log';
}

function startDownload() {
  const raw = textarea.value.trim();
  if (!raw) return;

  const urls = raw.split(/[\n,]+/).map(u => u.trim()).filter(Boolean);
  const quality = document.getElementById('quality').value;

  log.innerHTML = '';
  log.classList.add('visible');
  btn.disabled = true;
  openB.classList.remove('visible');
  badge.classList.remove('visible');

  fetch('/download', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ urls: urls.join('\n'), quality })
  }).then(res => {
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';

    function read() {
      reader.read().then(({ done, value }) => {
        if (done) { btn.disabled = false; return; }
        buf += dec.decode(value, { stream: true });
        const parts = buf.split('\n\n');
        buf = parts.pop();
        for (const part of parts) {
          if (!part.startsWith('data: ')) continue;
          try {
            const ev = JSON.parse(part.slice(6));
            if (ev.type === 'start') {
              appendLine(`▶ [${ev.index}/${ev.total}]  ${ev.url}`, 'start');
            } else if (ev.type === 'log') {
              if (ev.text) appendLine(ev.text, classifyLine(ev.text));
            } else if (ev.type === 'done') {
              appendLine(`✓  done`, 'done');
            } else if (ev.type === 'failed') {
              appendLine(`✗  failed`, 'failed');
            } else if (ev.type === 'all_done') {
              appendLine('─── all downloads finished ───', 'info');
              btn.disabled = false;
              openB.classList.add('visible');
              if (ev.failed_count > 0) {
                // Reload links.txt — server wrote failed URLs back
                fetch('/links').then(r => r.json()).then(({ urls }) => {
                  textarea.value = urls.join('\n');
                  badge.textContent = `${ev.failed_count} failed — retry?`;
                  badge.style.background = '#eb575722';
                  badge.style.color = '#eb5757';
                  badge.style.borderColor = '#eb575744';
                  badge.classList.add('visible');
                });
              } else {
                textarea.value = '';
              }
            }
          } catch(e) {}
        }
        read();
      });
    }
    read();
  }).catch(err => {
    appendLine('Error: ' + err.message, 'failed');
    btn.disabled = false;
  });
}

function openFolder() {
  fetch('/open-folder', { method: 'POST' });
}

textarea.addEventListener('keydown', e => {
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') startDownload();
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
        if self.path == '/open-folder':
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            subprocess.Popen(['open', OUTPUT_DIR])
            self.send_response(204)
            self.end_headers()
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

        failed_urls = []

        for i, url in enumerate(urls):
            send({'type': 'start', 'url': url, 'index': i + 1, 'total': len(urls)})
            try:
                proc = subprocess.Popen(
                    [DOWNLOAD_SCRIPT, url, quality, OUTPUT_DIR],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env={**os.environ, 'TERM': 'dumb'},
                )
                for line in proc.stdout:
                    send({'type': 'log', 'text': line.rstrip()})
                proc.wait()
                if proc.returncode == 0:
                    send({'type': 'done', 'url': url})
                else:
                    failed_urls.append(url)
                    send({'type': 'failed', 'url': url})
            except Exception as e:
                failed_urls.append(url)
                send({'type': 'failed', 'url': url, 'text': str(e)})

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
