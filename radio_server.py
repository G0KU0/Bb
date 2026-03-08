import socket
import subprocess
import threading
import queue
import time
import sys
import os
import base64
from flask import (
    Flask, render_template_string, request, redirect, jsonify
)

# =============================================
#  BEÁLLÍTÁSOK — Render.com env vars
# =============================================
SHOUTCAST_HOST = os.environ.get(
    'SHOUTCAST_HOST', 'uk3freenew.listen2myradio.com'
)
SHOUTCAST_PORT = int(os.environ.get('SHOUTCAST_PORT', '31822'))
SHOUTCAST_PASSWORD = os.environ.get('SHOUTCAST_PASSWORD', '2002')
BITRATE = int(os.environ.get('BITRATE', '128'))
STATION_NAME = os.environ.get('STATION_NAME', 'Szaby Radio')
STATION_GENRE = os.environ.get('STATION_GENRE', 'Various')
STATION_URL = os.environ.get(
    'STATION_URL', 'http://szaby.radio12345.com'
)
WEB_PORT = int(os.environ.get('PORT', '5000'))

app = Flask(__name__)


# =============================================
#  JS RUNTIME DETEKCIÓ — JAVÍTOTT!
#  yt-dlp elfogadott nevek: node, deno, bun, quickjs
#  (NEM "nodejs"!)
# =============================================
def detect_js_runtime():
    """
    Megkeresi a JS runtime-ot.
    FONTOS: a yt-dlp-nek 'node'-ot kell megadni,
    NEM 'nodejs'-t!
    """
    # Próba sorrend: node, deno, bun
    checks = [
        # (yt-dlp runtime név, shell parancs)
        ('node', 'node'),
        ('deno',  'deno'),
        ('bun',   'bun'),
    ]

    for runtime_name, binary in checks:
        try:
            r = subprocess.run(
                [binary, '--version'],
                capture_output=True, text=True, timeout=5
            )
            if r.returncode == 0:
                ver = r.stdout.strip()
                print(f'  ✅ JS Runtime: {runtime_name} ({ver})')
                return runtime_name
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue

    print('  ❌ Nincs JS runtime (node/deno/bun)!')
    return None


def check_tools():
    """FFmpeg és yt-dlp ellenőrzés"""
    for name, cmd in [
        ('FFmpeg', ['ffmpeg', '-version']),
        ('yt-dlp', ['yt-dlp', '--version']),
    ]:
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True, timeout=5
            )
            if r.returncode == 0:
                ver = r.stdout.strip().split('\n')[0][:60]
                print(f'  ✅ {name}: {ver}')
            else:
                print(f'  ❌ {name} hiba!')
        except FileNotFoundError:
            print(f'  ❌ {name} nem található!')


print()
print('╔═══════════════════════════════════════════╗')
print('║  🔧  Rendszer ellenőrzés...               ║')
print('╚═══════════════════════════════════════════╝')
JS_RUNTIME = detect_js_runtime()
check_tools()
print()


def get_ytdlp_base():
    """
    Alap yt-dlp argumentumok.
    A --js-runtimes 'node'-ot kap (NEM 'nodejs'-t!)
    """
    args = ['yt-dlp']
    if JS_RUNTIME:
        # FONTOS: 'node' kell, NEM 'nodejs'!
        args.extend(['--js-runtimes', JS_RUNTIME])
    args.extend([
        '--no-playlist',
        '--no-warnings',
    ])
    return args


# =============================================
#  GLOBAL STATE
# =============================================
class RadioState:
    def __init__(self):
        self.song_queue    = queue.Queue()
        self.display_queue = []
        self.lock          = threading.Lock()
        self.current       = {
            'title': 'Nincs zene lejátszás alatt',
            'url': ''
        }
        self.streaming     = False
        self.connected     = False
        self.conn_method   = ''
        self.worker_thread = None
        self.ffmpeg_proc   = None
        self.yt_proc       = None
        self.skip_event    = threading.Event()
        self.logs          = []
        self.total_played  = 0

    def log(self, msg):
        ts = time.strftime('%H:%M:%S')
        entry = f'[{ts}] {msg}'
        self.logs.append(entry)
        if len(self.logs) > 500:
            self.logs = self.logs[-300:]
        print(entry, flush=True)

R = RadioState()


# =============================================
#  SHOUTCAST SOURCE CONNECTION
# =============================================
class ShoutcastSource:
    def __init__(self):
        self.sock  = None
        self.alive = False

    def connect(self):
        methods = [
            ('SHOUTcast v1 (port+1)',
             self._try_v1_port_plus),
            ('SHOUTcast v2 SOURCE',
             self._try_v2_source),
            ('SHOUTcast v1 (base)',
             self._try_v1_base),
            ('Icecast PUT',
             self._try_icecast_put),
        ]
        for name, method in methods:
            R.log(f'🔌 Próba: {name}...')
            try:
                if method():
                    self.alive    = True
                    R.connected   = True
                    R.conn_method = name
                    R.log(f'✅ Csatlakozva: {name}')
                    return True
                else:
                    R.log(f'   ✗ {name}')
            except Exception as e:
                R.log(f'   ✗ {name}: {e}')
            self._close()

        R.log('❌ Nem sikerült csatlakozni!')
        R.connected = False
        return False

    def _try_v1_port_plus(self):
        port = SHOUTCAST_PORT + 1
        self.sock = socket.socket(
            socket.AF_INET, socket.SOCK_STREAM
        )
        self.sock.settimeout(10)
        self.sock.connect((SHOUTCAST_HOST, port))
        self.sock.sendall(
            f'{SHOUTCAST_PASSWORD}\r\n'.encode()
        )
        time.sleep(2)
        resp = self._recv()
        R.log(f'   Válasz: {repr(resp[:80])}')
        if 'OK' in resp.upper():
            self._send_icy()
            self.sock.settimeout(None)
            return True
        return False

    def _try_v2_source(self):
        self.sock = socket.socket(
            socket.AF_INET, socket.SOCK_STREAM
        )
        self.sock.settimeout(10)
        self.sock.connect((SHOUTCAST_HOST, SHOUTCAST_PORT))
        auth = base64.b64encode(
            f'source:{SHOUTCAST_PASSWORD}'.encode()
        ).decode()
        req = (
            f'SOURCE /sid=1 ICE/1.0\r\n'
            f'Content-Type: audio/mpeg\r\n'
            f'Authorization: Basic {auth}\r\n'
            f'User-Agent: SzabyRadio/4.1\r\n'
            f'ice-name: {STATION_NAME}\r\n'
            f'ice-genre: {STATION_GENRE}\r\n'
            f'ice-bitrate: {BITRATE}\r\n'
            f'ice-public: 1\r\n\r\n'
        )
        self.sock.sendall(req.encode())
        time.sleep(2)
        resp = self._recv()
        R.log(f'   Válasz: {repr(resp[:80])}')
        if '200' in resp or 'OK' in resp.upper():
            self.sock.settimeout(None)
            return True
        return False

    def _try_v1_base(self):
        self.sock = socket.socket(
            socket.AF_INET, socket.SOCK_STREAM
        )
        self.sock.settimeout(10)
        self.sock.connect((SHOUTCAST_HOST, SHOUTCAST_PORT))
        self.sock.sendall(
            f'{SHOUTCAST_PASSWORD}\r\n'.encode()
        )
        time.sleep(2)
        resp = self._recv()
        R.log(f'   Válasz: {repr(resp[:80])}')
        if 'OK' in resp.upper():
            self._send_icy()
            self.sock.settimeout(None)
            return True
        return False

    def _try_icecast_put(self):
        self.sock = socket.socket(
            socket.AF_INET, socket.SOCK_STREAM
        )
        self.sock.settimeout(10)
        self.sock.connect((SHOUTCAST_HOST, SHOUTCAST_PORT))
        auth = base64.b64encode(
            f'source:{SHOUTCAST_PASSWORD}'.encode()
        ).decode()
        req = (
            f'PUT / HTTP/1.1\r\n'
            f'Host: {SHOUTCAST_HOST}:{SHOUTCAST_PORT}\r\n'
            f'Authorization: Basic {auth}\r\n'
            f'Content-Type: audio/mpeg\r\n'
            f'ice-name: {STATION_NAME}\r\n'
            f'ice-bitrate: {BITRATE}\r\n'
            f'Transfer-Encoding: chunked\r\n\r\n'
        )
        self.sock.sendall(req.encode())
        time.sleep(2)
        resp = self._recv()
        R.log(f'   Válasz: {repr(resp[:80])}')
        if any(x in resp for x in ['200', '100']) \
                or 'OK' in resp.upper():
            self.sock.settimeout(None)
            return True
        return False

    def _recv(self):
        try:
            return self.sock.recv(4096).decode(
                errors='ignore'
            ).strip()
        except Exception:
            return ''

    def _send_icy(self):
        h = (
            f'content-type:audio/mpeg\r\n'
            f'icy-name:{STATION_NAME}\r\n'
            f'icy-genre:{STATION_GENRE}\r\n'
            f'icy-url:{STATION_URL}\r\n'
            f'icy-pub:1\r\n'
            f'icy-br:{BITRATE}\r\n\r\n'
        )
        self.sock.sendall(h.encode())

    def send(self, data):
        if not self.alive:
            return False
        try:
            self.sock.sendall(data)
            return True
        except Exception as e:
            R.log(f'❌ Küldési hiba: {e}')
            self.alive  = False
            R.connected = False
            return False

    def update_meta(self, title):
        """Dal cím frissítése a SHOUTcast szerveren"""
        try:
            import urllib.request
            import urllib.parse
            encoded = urllib.parse.quote(title)
            url = (
                f'http://{SHOUTCAST_HOST}:{SHOUTCAST_PORT}'
                f'/admin.cgi?pass={SHOUTCAST_PASSWORD}'
                f'&mode=updinfo&song={encoded}'
            )
            req = urllib.request.Request(url, headers={
                'User-Agent': 'SzabyRadio/4.1'
            })
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass

    def disconnect(self):
        self.alive  = False
        R.connected = False
        self._close()
        R.log('🔌 Lecsatlakozva')

    def _close(self):
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None


# =============================================
#  YOUTUBE HELPERS — JAVÍTOTT!
# =============================================
def yt_get_info(url):
    """
    Cím és audio URL egyszerre lekérdezése
    (gyorsabb, mint külön-külön)
    """
    try:
        cmd = get_ytdlp_base() + [
            '-f', 'bestaudio[ext=m4a]/bestaudio/best',
            '--print', 'title',
            '--print', 'urls',
            url
        ]
        R.log(f'   yt-dlp info lekérés...')
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=45
        )

        if r.returncode == 0 and r.stdout.strip():
            lines = r.stdout.strip().split('\n')
            title = lines[0] if len(lines) > 0 else 'Ismeretlen'
            audio_url = lines[1] if len(lines) > 1 else None
            R.log(f'   ✅ Cím: {title[:60]}')
            return title, audio_url

        # Hiba részletek
        if r.stderr:
            err = r.stderr.strip()
            R.log(f'   yt-dlp stderr: {err[:200]}')

        return 'Ismeretlen cím', None

    except subprocess.TimeoutExpired:
        R.log('   yt-dlp timeout!')
        return 'Ismeretlen cím', None
    except Exception as e:
        R.log(f'   yt-dlp hiba: {e}')
        return 'Ismeretlen cím', None


def yt_get_title(url):
    """Csak a cím lekérdezése"""
    try:
        cmd = get_ytdlp_base() + ['--print', 'title', url]
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30
        )
        t = r.stdout.strip()
        return t if t else 'Ismeretlen cím'
    except Exception:
        return 'Ismeretlen cím'


def yt_download_file(url, path):
    """Fájlba letöltés — utolsó módszer"""
    try:
        cmd = get_ytdlp_base() + [
            '-f', 'bestaudio[ext=m4a]/bestaudio/best',
            '-x', '--audio-format', 'mp3',
            '--audio-quality', f'{BITRATE}k',
            '-o', path, url
        ]
        R.log('   Letöltés fájlba...')
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=180
        )
        if r.returncode == 0 and os.path.exists(path):
            sz = os.path.getsize(path) // 1024
            R.log(f'   ✅ Letöltve: {sz} KB')
            return True
        if r.stderr:
            R.log(f'   yt-dlp: {r.stderr[:200]}')
        return False
    except Exception as e:
        R.log(f'   Letöltési hiba: {e}')
        return False


# =============================================
#  STREAMING ENGINE
# =============================================
def _kill(proc):
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def play_song(sc, youtube_url):
    """3 módszert próbál sorban"""

    # Egyszerre kérjük le a címet és audio URL-t
    title, audio_url = yt_get_info(youtube_url)
    R.current = {'title': title, 'url': youtube_url}
    R.log(f'🎵 Most szól: {title}')
    sc.update_meta(title)

    # === 1. módszer: URL stream ===
    if audio_url:
        R.log('   → URL stream')
        result = _stream_cmd(sc, [
            'ffmpeg', '-hide_banner', '-loglevel', 'error',
            '-reconnect', '1',
            '-reconnect_streamed', '1',
            '-reconnect_delay_max', '5',
            '-i', audio_url,
            '-vn', '-c:a', 'libmp3lame',
            '-b:a', f'{BITRATE}k',
            '-ar', '44100', '-ac', '2',
            '-f', 'mp3', 'pipe:1'
        ], title)
        if result != 'empty':
            return result != 'disconnect'

    # === 2. módszer: Pipe ===
    R.log('   → Pipe mód')
    pipe_result = _stream_pipe(sc, youtube_url, title)
    if pipe_result != 'empty':
        return pipe_result != 'disconnect'

    # === 3. módszer: Fájl letöltés ===
    R.log('   → Fájl mód')
    import tempfile
    tmp = os.path.join(
        tempfile.gettempdir(),
        f'szaby_{int(time.time())}.mp3'
    )
    if yt_download_file(youtube_url, tmp):
        result = _stream_cmd(sc, [
            'ffmpeg', '-hide_banner', '-loglevel', 'error',
            '-re', '-i', tmp,
            '-vn', '-c:a', 'libmp3lame',
            '-b:a', f'{BITRATE}k',
            '-ar', '44100', '-ac', '2',
            '-f', 'mp3', 'pipe:1'
        ], title)
        try:
            os.remove(tmp)
        except Exception:
            pass
        return result != 'disconnect'

    R.log(f'❌ Nem sikerült: {title}')
    return True


def _stream_pipe(sc, url, title):
    try:
        yt_cmd = get_ytdlp_base() + [
            '-f', 'bestaudio/best', '-o', '-', url
        ]
        yt_proc = subprocess.Popen(
            yt_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL
        )
        R.yt_proc = yt_proc

        ff_cmd = [
            'ffmpeg', '-hide_banner', '-loglevel', 'error',
            '-i', 'pipe:0', '-vn',
            '-c:a', 'libmp3lame',
            '-b:a', f'{BITRATE}k',
            '-ar', '44100', '-ac', '2',
            '-f', 'mp3', 'pipe:1'
        ]
        ff_proc = subprocess.Popen(
            ff_cmd,
            stdin=yt_proc.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL
        )
        yt_proc.stdout.close()

        result = _do_stream(sc, ff_proc, title)
        _kill(yt_proc)
        R.yt_proc = None
        return result
    except Exception as e:
        R.log(f'   Pipe hiba: {e}')
        return 'empty'


def _stream_cmd(sc, cmd, title):
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        return _do_stream(sc, proc, title)
    except Exception as e:
        R.log(f'❌ FFmpeg hiba: {e}')
        return 'empty'


def _do_stream(sc, ff_proc, title):
    R.ffmpeg_proc = ff_proc
    R.skip_event.clear()

    bps        = (BITRATE * 1000) / 8
    chunk      = 4096
    t_start    = time.time()
    total_sent = 0

    try:
        while R.streaming and not R.skip_event.is_set():
            data = ff_proc.stdout.read(chunk)
            if not data:
                break

            if not sc.send(data):
                _kill(ff_proc)
                R.ffmpeg_proc = None
                return 'disconnect'

            total_sent += len(data)
            expected = total_sent / bps
            elapsed  = time.time() - t_start
            if expected > elapsed:
                time.sleep(expected - elapsed)

    except Exception as e:
        R.log(f'❌ Stream hiba: {e}')

    _kill(ff_proc)
    R.ffmpeg_proc = None

    if total_sent == 0:
        return 'empty'

    dur = time.time() - t_start
    m, s = int(dur // 60), int(dur % 60)

    if R.skip_event.is_set():
        R.log(f'⏭ Kihagyva: {title}')
        return 'skip'

    R.total_played += 1
    R.log(f'✅ Kész: {title} ({m}:{s:02d})')
    return 'ok'


# =============================================
#  STREAM WORKER
# =============================================
def stream_worker():
    sc = ShoutcastSource()

    for attempt in range(5):
        if sc.connect():
            break
        R.log(
            f'🔄 Újrapróba {attempt+1}/5 (10mp)...'
        )
        time.sleep(10)

    if not sc.alive:
        R.log('❌ Nem sikerült csatlakozni!')
        R.streaming = False
        return

    R.log('🎧 Várakozás zenékre...')

    while R.streaming:
        try:
            url = R.song_queue.get(timeout=5)
        except queue.Empty:
            continue

        with R.lock:
            if R.display_queue:
                R.display_queue.pop(0)

        ok = play_song(sc, url)
        try:
            R.song_queue.task_done()
        except ValueError:
            pass

        if not ok and R.streaming:
            R.log('🔄 Újracsatlakozás...')
            sc.disconnect()
            time.sleep(5)
            reconnected = False
            for i in range(3):
                if sc.connect():
                    reconnected = True
                    break
                time.sleep(5)
            if not reconnected:
                break

    sc.disconnect()
    R.streaming = False
    R.connected = False
    R.current = {
        'title': 'Nincs zene lejátszás alatt',
        'url': ''
    }
    R.log('⏹ Stream leállt.')


# =============================================
#  WEB ROUTES
# =============================================
@app.route('/')
def index():
    msg      = request.args.get('msg', '')
    msg_type = request.args.get('t', 'success')
    with R.lock:
        dq = list(R.display_queue)
    return render_template_string(
        HTML_TEMPLATE,
        station     = STATION_NAME,
        connected   = R.connected,
        streaming   = R.streaming,
        conn_method = R.conn_method,
        current     = R.current,
        queue       = dq,
        logs        = R.logs[-60:],
        msg         = msg,
        msg_type    = msg_type,
        js_runtime  = JS_RUNTIME or 'NINCS ❌',
        total       = R.total_played
    )


@app.route('/add', methods=['POST'])
def add_song():
    url = request.form.get('url', '').strip()
    if not url:
        return redirect('/?msg=Üres+link!&t=error')
    if 'youtube.com' not in url and 'youtu.be' not in url:
        return redirect('/?msg=Csak+YouTube!&t=error')

    url = url.split('&list=')[0]

    with R.lock:
        R.display_queue.append({
            'title': '⏳ Betöltés...', 'url': url
        })
    R.song_queue.put(url)
    R.log(f'➕ Hozzáadva: {url[:70]}')

    def fetch():
        t = yt_get_title(url)
        with R.lock:
            for item in R.display_queue:
                if item['url'] == url \
                        and '⏳' in item['title']:
                    item['title'] = t
                    break
    threading.Thread(target=fetch, daemon=True).start()

    return redirect('/?msg=Hozzáadva!&t=success')


@app.route('/start', methods=['POST'])
def start_stream():
    if R.streaming:
        return redirect('/?msg=Már+fut!&t=error')
    R.streaming = True
    R.log('▶ Stream indítása...')
    R.worker_thread = threading.Thread(
        target=stream_worker, daemon=True
    )
    R.worker_thread.start()
    time.sleep(4)
    return redirect('/?msg=Elindítva!&t=success')


@app.route('/stop', methods=['POST'])
def stop_stream():
    R.streaming = False
    R.skip_event.set()
    _kill(R.ffmpeg_proc)
    _kill(R.yt_proc)
    R.log('⏹ Leállítás...')
    return redirect('/?msg=Leállítva!&t=success')


@app.route('/skip', methods=['POST'])
def skip_song():
    if not R.streaming:
        return redirect('/?msg=Nincs+stream!&t=error')
    R.skip_event.set()
    if R.ffmpeg_proc:
        try:
            R.ffmpeg_proc.terminate()
        except Exception:
            pass
    return redirect('/?msg=Kihagyva!&t=success')


@app.route('/clear', methods=['POST'])
def clear_queue():
    with R.lock:
        R.display_queue.clear()
        while not R.song_queue.empty():
            try:
                R.song_queue.get_nowait()
            except Exception:
                break
    R.log('🗑 Törölve')
    return redirect('/?msg=Törölve!&t=success')


@app.route('/health')
def health():
    return jsonify({
        'status': 'ok',
        'streaming': R.streaming,
        'connected': R.connected,
        'js_runtime': JS_RUNTIME
    })


@app.route('/api/status')
def api_status():
    with R.lock:
        dq = list(R.display_queue)
    return jsonify({
        'connected':   R.connected,
        'streaming':   R.streaming,
        'conn_method': R.conn_method,
        'current':     R.current,
        'queue':       dq,
        'queue_count': len(dq),
        'total':       R.total_played,
        'logs':        R.logs[-60:]
    })


# =============================================
#  HTML TEMPLATE
# =============================================
HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="hu">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ station }}</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🎵</text></svg>">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Segoe UI',Tahoma,sans-serif;
  background:linear-gradient(135deg,#0f0c29,#302b63,#24243e);
  color:#e0e0e0;min-height:100vh}
.w{max-width:960px;margin:0 auto;padding:16px}

.hdr{text-align:center;padding:20px 0 14px;
  border-bottom:2px solid rgba(255,255,255,.08);
  margin-bottom:14px}
.hdr h1{font-size:2.1em;
  background:linear-gradient(45deg,#ff6b6b,#feca57,#48dbfb,#a29bfe);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;
  background-clip:text}
.hdr .s{color:#666;font-size:.82em;margin-top:3px}

.fl{padding:10px 18px;border-radius:10px;
  margin-bottom:12px;font-weight:500;font-size:.88em}
.fl.success{background:rgba(0,184,148,.15);
  border:1px solid #00b894;color:#55efc4}
.fl.error{background:rgba(214,48,49,.15);
  border:1px solid #d63031;color:#ff7675}

.chips{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap}
.ch{background:rgba(255,255,255,.05);
  border:1px solid rgba(255,255,255,.1);
  border-radius:8px;padding:4px 11px;font-size:.72em}
.ch b{margin-right:3px}
.ch.g{border-color:#00b894;color:#55efc4}
.ch.r{border-color:#d63031;color:#ff7675}
.ch.y{border-color:#fdcb6e;color:#feca57}

.sb{display:flex;justify-content:space-between;
  align-items:center;background:rgba(255,255,255,.04);
  border-radius:14px;padding:12px 18px;margin-bottom:14px;
  border:1px solid rgba(255,255,255,.08);
  flex-wrap:wrap;gap:8px}
.si{display:flex;align-items:center;gap:8px;font-weight:500}
.d{width:12px;height:12px;border-radius:50%}
.d.on{background:#00ff88;box-shadow:0 0 10px #00ff88;
  animation:p 2s infinite}
.d.off{background:#ff4757;box-shadow:0 0 6px #ff4757}
@keyframes p{0%,100%{opacity:1}50%{opacity:.4}}
.bg{font-size:.63em;background:rgba(108,92,231,.3);
  padding:2px 8px;border-radius:5px;color:#a29bfe}
.ct{display:flex;gap:5px;flex-wrap:wrap}

.bt{padding:8px 16px;border:none;border-radius:8px;
  cursor:pointer;font-size:11px;font-weight:700;
  transition:.2s;text-transform:uppercase;
  letter-spacing:.5px;color:#fff}
.bt:hover{transform:translateY(-1px);
  box-shadow:0 4px 12px rgba(0,0,0,.3)}
.bt.go{background:linear-gradient(135deg,#00b894,#00cec9)}
.bt.sp{background:linear-gradient(135deg,#e17055,#d63031)}
.bt.sk{background:linear-gradient(135deg,#fdcb6e,#e17055)}
.bt.cl{background:linear-gradient(135deg,#636e72,#2d3436)}

.cd{background:rgba(255,255,255,.04);border-radius:14px;
  padding:16px 20px;margin-bottom:12px;
  border:1px solid rgba(255,255,255,.07)}
.cd h3{font-size:.73em;text-transform:uppercase;
  letter-spacing:2px;margin-bottom:8px;font-weight:700}
.cd h3.c{color:#48dbfb}
.cd h3.y{color:#feca57}
.cd h3.re{color:#ff6b6b}

.np{display:flex;align-items:center;gap:12px}
.eq{display:flex;align-items:flex-end;gap:3px;height:26px}
.eq i{width:4px;background:#48dbfb;border-radius:2px;
  display:block;animation:e .8s ease-in-out infinite alternate}
.eq i:nth-child(1){height:8px;animation-delay:0s}
.eq i:nth-child(2){height:18px;animation-delay:.15s}
.eq i:nth-child(3){height:12px;animation-delay:.3s}
.eq i:nth-child(4){height:22px;animation-delay:.1s}
.eq i:nth-child(5){height:7px;animation-delay:.25s}
@keyframes e{to{height:3px}}
#now{font-size:1.2em;font-weight:600;color:#fff;
  word-break:break-word}

.ir{display:flex;gap:8px}
.ir input{flex:1;padding:10px 14px;border-radius:10px;
  border:1px solid rgba(255,255,255,.15);
  background:rgba(255,255,255,.06);color:#fff;
  font-size:13px;outline:none}
.ir input:focus{border-color:#a29bfe}
.ir input::placeholder{color:#555}
.ba{background:linear-gradient(135deg,#6c5ce7,#a29bfe);
  color:#fff;padding:10px 20px;border:none;border-radius:10px;
  cursor:pointer;font-size:13px;font-weight:700;
  white-space:nowrap}

.qi{display:flex;align-items:center;padding:7px 11px;
  background:rgba(255,255,255,.025);border-radius:7px;
  margin-bottom:4px;border-left:3px solid #6c5ce7}
.qi .n{color:#a29bfe;font-weight:700;margin-right:10px;
  min-width:20px;text-align:right;font-size:.82em}
.qi .t{flex:1;font-size:.85em;word-break:break-word}
.qe{color:#555;text-align:center;padding:14px;
  font-style:italic;font-size:.88em}

.lb{background:rgba(0,0,0,.3);border-radius:12px;
  padding:14px;margin-bottom:12px;
  border:1px solid rgba(255,255,255,.04)}
.lb h3{color:#55efc4;font-size:.73em;text-transform:uppercase;
  letter-spacing:2px;margin-bottom:6px}
#logs{max-height:240px;overflow-y:auto;
  font-family:'Courier New',monospace;
  font-size:.73em;color:#888;line-height:1.55}
#logs::-webkit-scrollbar{width:4px}
#logs::-webkit-scrollbar-thumb{background:#444;border-radius:3px}

.lk{text-align:center;padding:12px;
  background:rgba(255,255,255,.025);border-radius:12px;
  border:1px solid rgba(255,255,255,.06)}
.lk h3{color:#48dbfb;font-size:.73em;text-transform:uppercase;
  letter-spacing:2px;margin-bottom:6px}
.lk a{color:#feca57;text-decoration:none;margin:0 8px;
  font-weight:500;font-size:.85em}
.ft{text-align:center;padding:10px;color:#444;font-size:.68em}
</style>
</head>
<body>
<div class="w">
  <div class="hdr">
    <h1>🎵 {{ station }}</h1>
    <div class="s">DJ Control Panel v4.1 — Cloud</div>
  </div>

  {% if msg %}
  <div class="fl {{ msg_type }}">{{ msg }}</div>
  {% endif %}

  <div class="chips">
    <div class="ch {{ 'g' if 'NINCS' not in js_runtime else 'r' }}">
      <b>JS:</b> {{ js_runtime }}</div>
    <div class="ch {{ 'g' if connected else 'r' }}">
      <b>Szerver:</b>
      {{ 'Online' if connected else 'Offline' }}</div>
    <div class="ch y">
      <b>Lejátszva:</b>
      <span id="tot">{{ total }}</span> dal</div>
  </div>

  <div class="sb">
    <div class="si">
      <div class="d {{ 'on' if connected else 'off' }}"
           id="dot"></div>
      <span id="st">
        {{ 'STREAMING' if connected else 'OFFLINE' }}
      </span>
      {% if conn_method and connected %}
      <span class="bg">{{ conn_method }}</span>
      {% endif %}
    </div>
    <div class="ct">
      {% if not streaming %}
      <form action="/start" method="post" style="display:inline">
        <button class="bt go">▶ Indítás</button></form>
      {% else %}
      <form action="/stop" method="post" style="display:inline">
        <button class="bt sp">■ Stop</button></form>
      <form action="/skip" method="post" style="display:inline">
        <button class="bt sk">⏭ Skip</button></form>
      {% endif %}
      <form action="/clear" method="post" style="display:inline">
        <button class="bt cl">🗑</button></form>
    </div>
  </div>

  <div class="cd">
    <h3 class="c">🎧 Most Szól</h3>
    <div class="np">
      {% if connected %}
      <div class="eq">
        <i></i><i></i><i></i><i></i><i></i>
      </div>
      {% endif %}
      <div id="now">{{ current.title }}</div>
    </div>
  </div>

  <div class="cd">
    <h3 class="y">➕ YouTube Link</h3>
    <form action="/add" method="post">
      <div class="ir">
        <input type="text" name="url"
          placeholder="https://www.youtube.com/watch?v=..."
          required autocomplete="off">
        <button type="submit" class="ba">+ Add</button>
      </div>
    </form>
  </div>

  <div class="cd">
    <h3 class="re">📋 Várólista
      (<span id="qc">{{ queue|length }}</span>)</h3>
    <div id="ql">
      {% if queue %}
        {% for i in queue %}
        <div class="qi">
          <span class="n">{{ loop.index }}.</span>
          <span class="t">{{ i.title }}</span>
        </div>
        {% endfor %}
      {% else %}
        <div class="qe">Üres</div>
      {% endif %}
    </div>
  </div>

  <div class="lb">
    <h3>📊 Napló</h3>
    <div id="logs">
      {% for l in logs %}<div>{{ l }}</div>{% endfor %}
    </div>
  </div>

  <div class="lk">
    <h3>🔗 Hallgatás</h3>
    <a href="http://szaby.radio12345.com" target="_blank">
      szaby.radio12345.com</a>
    <a href="http://szaby.radiostream321.com" target="_blank">
      szaby.radiostream321.com</a>
    <a href="http://szaby.radiostream123.com" target="_blank">
      szaby.radiostream123.com</a>
  </div>

  <div class="ft">☁️ Render.com Cloud</div>
</div>

<script>
function x(s){var d=document.createElement('div');
  d.textContent=s;return d.innerHTML}
setInterval(()=>{
  fetch('/api/status').then(r=>r.json()).then(d=>{
    document.getElementById('dot').className=
      'd '+(d.connected?'on':'off');
    document.getElementById('st').textContent=
      d.connected?'STREAMING':'OFFLINE';
    document.getElementById('now').textContent=
      d.current.title;
    document.getElementById('qc').textContent=
      d.queue_count;
    document.getElementById('tot').textContent=d.total;
    var h='';
    if(!d.queue.length) h='<div class="qe">Üres</div>';
    else d.queue.forEach((q,i)=>{
      h+='<div class="qi"><span class="n">'+(i+1)+
        '.</span><span class="t">'+x(q.title)+
        '</span></div>';});
    document.getElementById('ql').innerHTML=h;
    var lh='';
    d.logs.forEach(l=>{lh+='<div>'+x(l)+'</div>';});
    var el=document.getElementById('logs');
    el.innerHTML=lh;el.scrollTop=el.scrollHeight;
  }).catch(()=>{});
},3000);
(()=>{var el=document.getElementById('logs');
  if(el)el.scrollTop=el.scrollHeight})();
</script>
</body>
</html>
"""


if __name__ == '__main__':
    print(f"""
╔═══════════════════════════════════════════════╗
║  🎵  {STATION_NAME} v4.1                      ║
║  Port: {WEB_PORT}  |  JS: {(JS_RUNTIME or 'NINCS'):<22}║
║  SHOUTcast: {SHOUTCAST_HOST}:{SHOUTCAST_PORT}     ║
╚═══════════════════════════════════════════════╝
    """)
    app.run(
        host='0.0.0.0',
        port=WEB_PORT,
        debug=False,
        threaded=True
    )
