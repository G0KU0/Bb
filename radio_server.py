import socket
import subprocess
import threading
import queue
import time
import sys
import os
import base64
import tempfile
from flask import (
    Flask, render_template_string, request,
    redirect, jsonify
)

# =============================================
#  BEÁLLÍTÁSOK
# =============================================
SHOUTCAST_HOST = os.environ.get(
    'SHOUTCAST_HOST', 'uk3freenew.listen2myradio.com')
SHOUTCAST_PORT = int(os.environ.get('SHOUTCAST_PORT', '31822'))
SHOUTCAST_PASSWORD = os.environ.get('SHOUTCAST_PASSWORD', '2002')
BITRATE = int(os.environ.get('BITRATE', '128'))
STATION_NAME = os.environ.get('STATION_NAME', 'Szaby Radio')
STATION_GENRE = os.environ.get('STATION_GENRE', 'Various')
STATION_URL = os.environ.get(
    'STATION_URL', 'http://szaby.radio12345.com')
WEB_PORT = int(os.environ.get('PORT', '5000'))

# Cookie fájl helye
COOKIE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    'cookies.txt'
)

app = Flask(__name__)


# =============================================
#  JS RUNTIME
# =============================================
def detect_js_runtime():
    for name, binary in [
        ('node', 'node'),
        ('deno', 'deno'),
        ('bun', 'bun'),
    ]:
        try:
            r = subprocess.run(
                [binary, '--version'],
                capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                v = r.stdout.strip()
                print(f'  ✅ JS: {name} ({v})')
                return name
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    print('  ❌ Nincs JS runtime!')
    return None


def check_cookies():
    """Cookie fájl ellenőrzése"""
    if os.path.exists(COOKIE_FILE):
        size = os.path.getsize(COOKIE_FILE)
        if size > 100:
            print(f'  ✅ Cookies: {COOKIE_FILE} ({size} bytes)')
            return True
        else:
            print(f'  ⚠️  Cookies fájl túl kicsi ({size} bytes)')
            return False
    print(f'  ⚠️  Nincs cookies.txt — YouTube blokkolhat!')
    return False


print()
print('╔═══════════════════════════════════════════╗')
print('║  🔧  Rendszer ellenőrzés                  ║')
print('╚═══════════════════════════════════════════╝')
JS_RUNTIME = detect_js_runtime()
HAS_COOKIES = check_cookies()

# FFmpeg & yt-dlp
for name, cmd in [
    ('FFmpeg', ['ffmpeg', '-version']),
    ('yt-dlp', ['yt-dlp', '--version']),
]:
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=5)
        v = r.stdout.strip().split('\n')[0][:50]
        print(f'  ✅ {name}: {v}')
    except Exception:
        print(f'  ❌ {name} hiányzik!')
print()


# =============================================
#  YT-DLP ARGUMENTUMOK
# =============================================
def get_ytdlp_base():
    """
    yt-dlp alap argumentumok:
    - JS runtime
    - Cookie fájl (ha létezik)
    - Anti-bot beállítások
    """
    args = ['yt-dlp']

    # JS runtime
    if JS_RUNTIME:
        args.extend(['--js-runtimes', JS_RUNTIME])

    # Cookie fájl — EZ A KULCS a bot-védelem ellen!
    if os.path.exists(COOKIE_FILE):
        size = os.path.getsize(COOKIE_FILE)
        if size > 100:
            args.extend(['--cookies', COOKIE_FILE])

    # Anti-bot beállítások
    args.extend([
        '--no-playlist',
        '--no-warnings',
        '--extractor-args',
        'youtube:player_client=web,default',
        '--user-agent',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/131.0.0.0 Safari/537.36',
        '--referer', 'https://www.youtube.com/',
        '--sleep-interval', '1',
        '--max-sleep-interval', '3',
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
        self.has_cookies   = HAS_COOKIES
        self.auth_method   = (
            'Cookie' if HAS_COOKIES else 'Nincs'
        )

    def log(self, msg):
        ts = time.strftime('%H:%M:%S')
        entry = f'[{ts}] {msg}'
        self.logs.append(entry)
        if len(self.logs) > 500:
            self.logs = self.logs[-300:]
        print(entry, flush=True)

R = RadioState()


# =============================================
#  SHOUTCAST SOURCE
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
            except Exception as e:
                R.log(f'   ✗ {name}: {e}')
            self._close()
        R.log('❌ Csatlakozás sikertelen!')
        R.connected = False
        return False

    def _try_v1_port_plus(self):
        port = SHOUTCAST_PORT + 1
        self.sock = socket.socket(
            socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect((SHOUTCAST_HOST, port))
        self.sock.sendall(
            f'{SHOUTCAST_PASSWORD}\r\n'.encode())
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
            socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect(
            (SHOUTCAST_HOST, SHOUTCAST_PORT))
        auth = base64.b64encode(
            f'source:{SHOUTCAST_PASSWORD}'.encode()
        ).decode()
        req = (
            f'SOURCE /sid=1 ICE/1.0\r\n'
            f'Content-Type: audio/mpeg\r\n'
            f'Authorization: Basic {auth}\r\n'
            f'ice-name: {STATION_NAME}\r\n'
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
            socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect(
            (SHOUTCAST_HOST, SHOUTCAST_PORT))
        self.sock.sendall(
            f'{SHOUTCAST_PASSWORD}\r\n'.encode())
        time.sleep(2)
        resp = self._recv()
        R.log(f'   Válasz: {repr(resp[:80])}')
        if 'OK' in resp.upper():
            self._send_icy()
            self.sock.settimeout(None)
            return True
        return False

    def _recv(self):
        try:
            return self.sock.recv(4096).decode(
                errors='ignore').strip()
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
        try:
            import urllib.request
            import urllib.parse
            enc = urllib.parse.quote(title)
            url = (
                f'http://{SHOUTCAST_HOST}:'
                f'{SHOUTCAST_PORT}'
                f'/admin.cgi?pass='
                f'{SHOUTCAST_PASSWORD}'
                f'&mode=updinfo&song={enc}'
            )
            req = urllib.request.Request(url)
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
#  YOUTUBE FUNCTIONS
# =============================================
def yt_get_info(url):
    """Cím + audio URL egyszerre"""
    try:
        cmd = get_ytdlp_base() + [
            '-f', 'bestaudio[ext=m4a]/bestaudio/best',
            '--print', 'title',
            '--print', 'urls',
            url
        ]
        R.log('   yt-dlp info lekérés...')
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=60
        )

        if r.returncode == 0 and r.stdout.strip():
            lines = r.stdout.strip().split('\n')
            title = lines[0] if lines else 'Ismeretlen'
            audio = lines[1] if len(lines) > 1 else None
            R.log(f'   ✅ {title[:60]}')
            return title, audio

        # Hiba kezelés
        if r.stderr:
            err = r.stderr.strip()
            if 'Sign in' in err or 'bot' in err:
                R.log('   ❌ YouTube bot-védelem!')
                R.log('   → Cookie frissítés szükséges!')
            elif 'ERROR' in err:
                R.log(f'   ❌ {err[:200]}')
            else:
                R.log(f'   ⚠️ {err[:200]}')

        return 'Ismeretlen cím', None

    except subprocess.TimeoutExpired:
        R.log('   ⏱ Timeout!')
        return 'Ismeretlen cím', None
    except Exception as e:
        R.log(f'   ❌ Hiba: {e}')
        return 'Ismeretlen cím', None


def yt_get_title(url):
    try:
        cmd = get_ytdlp_base() + [
            '--print', 'title', url
        ]
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=30
        )
        t = r.stdout.strip()
        return t if t else 'Ismeretlen cím'
    except Exception:
        return 'Ismeretlen cím'


def yt_download_file(url, path):
    """Letöltés fájlba"""
    try:
        cmd = get_ytdlp_base() + [
            '-f', 'bestaudio[ext=m4a]/bestaudio/best',
            '-x', '--audio-format', 'mp3',
            '--audio-quality', f'{BITRATE}k',
            '-o', path, url
        ]
        R.log('   Letöltés fájlba...')
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=180
        )
        if r.returncode == 0 and os.path.exists(path):
            sz = os.path.getsize(path) // 1024
            R.log(f'   ✅ {sz} KB')
            return True
        if r.stderr:
            R.log(f'   ❌ {r.stderr[:200]}')
        return False
    except Exception as e:
        R.log(f'   ❌ {e}')
        return False


def yt_test_cookies():
    """Teszteli hogy a cookie-k működnek-e"""
    try:
        cmd = get_ytdlp_base() + [
            '--print', 'title',
            'https://www.youtube.com/watch?v=dQw4w9WgXcQ'
        ]
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=30
        )
        if r.returncode == 0 and r.stdout.strip():
            return True, r.stdout.strip()
        if r.stderr and 'bot' in r.stderr.lower():
            return False, 'Bot-védelem aktív!'
        return False, r.stderr[:200] if r.stderr else '?'
    except Exception as e:
        return False, str(e)


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
    title, audio_url = yt_get_info(youtube_url)
    R.current = {'title': title, 'url': youtube_url}
    R.log(f'🎵 {title}')
    sc.update_meta(title)

    # 1. URL stream
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

    # 2. Pipe
    R.log('   → Pipe')
    pr = _stream_pipe(sc, youtube_url, title)
    if pr != 'empty':
        return pr != 'disconnect'

    # 3. Fájl
    R.log('   → Fájl letöltés')
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
    R.log('   → Ellenőrizd a cookies.txt fájlt!')
    return True


def _stream_pipe(sc, url, title):
    try:
        yt_cmd = get_ytdlp_base() + [
            '-f', 'bestaudio/best',
            '-o', '-', url
        ]
        yt_proc = subprocess.Popen(
            yt_cmd, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL)
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
            ff_cmd, stdin=yt_proc.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL)
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
            cmd, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        return _do_stream(sc, proc, title)
    except Exception as e:
        R.log(f'❌ FFmpeg: {e}')
        return 'empty'


def _do_stream(sc, ff_proc, title):
    R.ffmpeg_proc = ff_proc
    R.skip_event.clear()

    bps = (BITRATE * 1000) / 8
    chunk = 4096
    t0 = time.time()
    sent = 0

    try:
        while R.streaming and not R.skip_event.is_set():
            data = ff_proc.stdout.read(chunk)
            if not data:
                break
            if not sc.send(data):
                _kill(ff_proc)
                R.ffmpeg_proc = None
                return 'disconnect'
            sent += len(data)
            exp = sent / bps
            ela = time.time() - t0
            if exp > ela:
                time.sleep(exp - ela)
    except Exception as e:
        R.log(f'❌ {e}')

    _kill(ff_proc)
    R.ffmpeg_proc = None

    if sent == 0:
        return 'empty'

    dur = time.time() - t0
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
    for i in range(5):
        if sc.connect():
            break
        R.log(f'🔄 Újrapróba {i+1}/5...')
        time.sleep(10)

    if not sc.alive:
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
            ok2 = False
            for i in range(3):
                if sc.connect():
                    ok2 = True
                    break
                time.sleep(5)
            if not ok2:
                break

    sc.disconnect()
    R.streaming = False
    R.connected = False
    R.current = {
        'title': 'Nincs zene lejátszás alatt',
        'url': ''
    }
    R.log('⏹ Leállt.')


# =============================================
#  WEB ROUTES
# =============================================
@app.route('/')
def index():
    msg = request.args.get('msg', '')
    mt  = request.args.get('t', 'success')
    with R.lock:
        dq = list(R.display_queue)
    return render_template_string(
        HTML_TEMPLATE,
        station=STATION_NAME,
        connected=R.connected,
        streaming=R.streaming,
        conn_method=R.conn_method,
        current=R.current,
        queue=dq,
        logs=R.logs[-60:],
        msg=msg,
        msg_type=mt,
        js_runtime=JS_RUNTIME or 'NINCS',
        total=R.total_played,
        has_cookies=R.has_cookies,
        auth_method=R.auth_method,
    )


@app.route('/add', methods=['POST'])
def add_song():
    url = request.form.get('url', '').strip()
    if not url:
        return redirect('/?msg=Üres!&t=error')
    if 'youtube.com' not in url \
            and 'youtu.be' not in url:
        return redirect('/?msg=Csak+YouTube!&t=error')

    url = url.split('&list=')[0]

    with R.lock:
        R.display_queue.append({
            'title': '⏳ Betöltés...', 'url': url
        })
    R.song_queue.put(url)
    R.log(f'➕ {url[:70]}')

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
    R.log('▶ Indítás...')
    R.worker_thread = threading.Thread(
        target=stream_worker, daemon=True)
    R.worker_thread.start()
    time.sleep(4)
    return redirect('/?msg=Elindítva!&t=success')


@app.route('/stop', methods=['POST'])
def stop_stream():
    R.streaming = False
    R.skip_event.set()
    _kill(R.ffmpeg_proc)
    _kill(R.yt_proc)
    R.log('⏹ Stop')
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
    return redirect('/?msg=Skip!&t=success')


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


@app.route('/upload_cookies', methods=['POST'])
def upload_cookies():
    """Cookie fájl feltöltése a webes felületen"""
    if 'cookiefile' not in request.files:
        return redirect(
            '/?msg=Válassz+fájlt!&t=error')

    f = request.files['cookiefile']
    if f.filename == '':
        return redirect(
            '/?msg=Válassz+fájlt!&t=error')

    content = f.read()

    if len(content) < 50:
        return redirect(
            '/?msg=Túl+kicsi+fájl!&t=error')

    # Mentés
    with open(COOKIE_FILE, 'wb') as cf:
        cf.write(content)

    R.has_cookies = True
    R.auth_method = 'Cookie (feltöltve)'
    R.log(f'🍪 Cookie feltöltve! ({len(content)} bytes)')

    # Teszt
    ok, msg = yt_test_cookies()
    if ok:
        R.log(f'✅ Cookie teszt OK: {msg}')
        return redirect(
            '/?msg=Cookie+feltöltve+és+működik!&t=success')
    else:
        R.log(f'⚠️ Cookie teszt: {msg}')
        return redirect(
            '/?msg=Cookie+feltöltve,+de+teszt+sikertelen:'
            f'+{msg[:50]}&t=error')


@app.route('/paste_cookies', methods=['POST'])
def paste_cookies():
    """Cookie tartalom beillesztése szövegként"""
    content = request.form.get('cookie_text', '').strip()
    if not content or len(content) < 50:
        return redirect(
            '/?msg=Túl+rövid+tartalom!&t=error')

    with open(COOKIE_FILE, 'w') as cf:
        cf.write(content)

    R.has_cookies = True
    R.auth_method = 'Cookie (beillesztve)'
    R.log(f'🍪 Cookie beillesztve! ({len(content)} kar)')

    ok, msg = yt_test_cookies()
    if ok:
        R.log(f'✅ Cookie teszt OK: {msg}')
        return redirect(
            '/?msg=Cookie+OK!&t=success')
    else:
        R.log(f'⚠️ Cookie teszt: {msg}')
        return redirect(
            '/?msg=Cookie+mentve,+teszt:'
            f'+{msg[:50]}&t=error')


@app.route('/test_yt', methods=['POST'])
def test_youtube():
    """YouTube hozzáférés tesztelése"""
    R.log('🔍 YouTube teszt...')
    ok, msg = yt_test_cookies()
    if ok:
        R.log(f'✅ YouTube OK: {msg}')
        return redirect(
            '/?msg=YouTube+elérhető!+({})&t=success'
            .format(msg[:40]))
    else:
        R.log(f'❌ YouTube hiba: {msg}')
        return redirect(
            '/?msg=YouTube+hiba:+{}&t=error'
            .format(msg[:50]))


@app.route('/health')
def health():
    return jsonify({'status': 'ok'})


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
        'has_cookies': R.has_cookies,
        'auth_method': R.auth_method,
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
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ station }}</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🎵</text></svg>">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Segoe UI',Tahoma,sans-serif;
  background:linear-gradient(135deg,#0f0c29,#302b63,#24243e);
  color:#e0e0e0;min-height:100vh}
.w{max-width:960px;margin:0 auto;padding:14px}

.hdr{text-align:center;padding:18px 0 12px;
  border-bottom:2px solid rgba(255,255,255,.08);
  margin-bottom:12px}
.hdr h1{font-size:2em;
  background:linear-gradient(45deg,#ff6b6b,#feca57,#48dbfb,#a29bfe);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;
  background-clip:text}
.hdr .s{color:#666;font-size:.8em;margin-top:3px}

.fl{padding:9px 16px;border-radius:10px;margin-bottom:10px;
  font-weight:500;font-size:.85em}
.fl.success{background:rgba(0,184,148,.15);border:1px solid #00b894;color:#55efc4}
.fl.error{background:rgba(214,48,49,.15);border:1px solid #d63031;color:#ff7675}

.chips{display:flex;gap:6px;margin-bottom:10px;flex-wrap:wrap}
.ch{background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);
  border-radius:7px;padding:4px 10px;font-size:.7em}
.ch b{margin-right:2px}
.ch.g{border-color:#00b894;color:#55efc4}
.ch.r{border-color:#d63031;color:#ff7675}
.ch.y{border-color:#fdcb6e;color:#feca57}
.ch.p{border-color:#a29bfe;color:#a29bfe}

.sb{display:flex;justify-content:space-between;align-items:center;
  background:rgba(255,255,255,.04);border-radius:12px;padding:10px 16px;
  margin-bottom:12px;border:1px solid rgba(255,255,255,.08);
  flex-wrap:wrap;gap:8px}
.si{display:flex;align-items:center;gap:7px;font-weight:500;font-size:.9em}
.dt{width:11px;height:11px;border-radius:50%}
.dt.on{background:#00ff88;box-shadow:0 0 8px #00ff88;animation:p 2s infinite}
.dt.off{background:#ff4757;box-shadow:0 0 6px #ff4757}
@keyframes p{0%,100%{opacity:1}50%{opacity:.4}}
.bg{font-size:.6em;background:rgba(108,92,231,.3);padding:2px 7px;
  border-radius:5px;color:#a29bfe}
.ct{display:flex;gap:4px;flex-wrap:wrap}

.bt{padding:8px 15px;border:none;border-radius:7px;cursor:pointer;
  font-size:10px;font-weight:700;transition:.2s;text-transform:uppercase;
  letter-spacing:.4px;color:#fff}
.bt:hover{transform:translateY(-1px);box-shadow:0 3px 10px rgba(0,0,0,.3)}
.bt.go{background:linear-gradient(135deg,#00b894,#00cec9)}
.bt.sp{background:linear-gradient(135deg,#e17055,#d63031)}
.bt.sk{background:linear-gradient(135deg,#fdcb6e,#e17055)}
.bt.cl{background:linear-gradient(135deg,#636e72,#2d3436)}
.bt.bl{background:linear-gradient(135deg,#0984e3,#74b9ff)}

.cd{background:rgba(255,255,255,.04);border-radius:12px;padding:14px 18px;
  margin-bottom:10px;border:1px solid rgba(255,255,255,.07)}
.cd h3{font-size:.7em;text-transform:uppercase;letter-spacing:1.5px;
  margin-bottom:7px;font-weight:700}
.cd h3.c{color:#48dbfb}
.cd h3.y{color:#feca57}
.cd h3.re{color:#ff6b6b}
.cd h3.or{color:#e17055}
.cd h3.gr{color:#55efc4}

.np{display:flex;align-items:center;gap:12px}
.eq{display:flex;align-items:flex-end;gap:2px;height:24px}
.eq i{width:3px;background:#48dbfb;border-radius:2px;display:block;
  animation:e .8s ease-in-out infinite alternate}
.eq i:nth-child(1){height:7px;animation-delay:0s}
.eq i:nth-child(2){height:16px;animation-delay:.15s}
.eq i:nth-child(3){height:10px;animation-delay:.3s}
.eq i:nth-child(4){height:20px;animation-delay:.1s}
.eq i:nth-child(5){height:6px;animation-delay:.25s}
@keyframes e{to{height:3px}}
#now{font-size:1.15em;font-weight:600;color:#fff;word-break:break-word}

.ir{display:flex;gap:7px}
.ir input{flex:1;padding:10px 12px;border-radius:9px;
  border:1px solid rgba(255,255,255,.15);background:rgba(255,255,255,.06);
  color:#fff;font-size:13px;outline:none}
.ir input:focus{border-color:#a29bfe}
.ir input::placeholder{color:#555}
.ba{background:linear-gradient(135deg,#6c5ce7,#a29bfe);color:#fff;
  padding:10px 18px;border:none;border-radius:9px;cursor:pointer;
  font-size:13px;font-weight:700;white-space:nowrap}

/* Cookie section */
.cookie-box{background:rgba(253,203,110,.06);border:1px solid rgba(253,203,110,.2);
  border-radius:12px;padding:14px 18px;margin-bottom:10px}
.cookie-box h3{color:#feca57;font-size:.7em;text-transform:uppercase;
  letter-spacing:1.5px;margin-bottom:8px}
.cookie-box p{font-size:.78em;color:#999;margin-bottom:8px;line-height:1.5}
.cookie-box .row{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px;
  align-items:center}
.cookie-box input[type=file]{font-size:.8em;color:#aaa}
.cookie-box textarea{width:100%;height:80px;background:rgba(0,0,0,.3);
  border:1px solid rgba(255,255,255,.1);border-radius:8px;color:#ccc;
  font-family:monospace;font-size:.75em;padding:8px;resize:vertical;
  outline:none}
.cookie-box textarea:focus{border-color:#feca57}
.cookie-box .btn-sm{padding:6px 14px;border:none;border-radius:6px;
  cursor:pointer;font-size:.75em;font-weight:600;color:#fff;
  text-transform:uppercase}
.btn-cookie{background:linear-gradient(135deg,#fdcb6e,#e17055)}
.btn-test{background:linear-gradient(135deg,#0984e3,#74b9ff)}

.qi{display:flex;align-items:center;padding:7px 10px;
  background:rgba(255,255,255,.025);border-radius:7px;margin-bottom:4px;
  border-left:3px solid #6c5ce7}
.qi .n{color:#a29bfe;font-weight:700;margin-right:8px;min-width:18px;
  text-align:right;font-size:.8em}
.qi .t{flex:1;font-size:.83em;word-break:break-word}
.qe{color:#555;text-align:center;padding:12px;font-style:italic;font-size:.85em}

.lb{background:rgba(0,0,0,.3);border-radius:10px;padding:12px;
  margin-bottom:10px;border:1px solid rgba(255,255,255,.04)}
.lb h3{color:#55efc4;font-size:.7em;text-transform:uppercase;
  letter-spacing:1.5px;margin-bottom:5px}
#logs{max-height:220px;overflow-y:auto;font-family:'Courier New',monospace;
  font-size:.7em;color:#888;line-height:1.5}
#logs::-webkit-scrollbar{width:4px}
#logs::-webkit-scrollbar-thumb{background:#444;border-radius:3px}

.lk{text-align:center;padding:12px;background:rgba(255,255,255,.025);
  border-radius:10px;border:1px solid rgba(255,255,255,.06)}
.lk h3{color:#48dbfb;font-size:.7em;text-transform:uppercase;
  letter-spacing:1.5px;margin-bottom:5px}
.lk a{color:#feca57;text-decoration:none;margin:0 6px;font-weight:500;
  font-size:.82em}
.ft{text-align:center;padding:8px;color:#444;font-size:.65em}

.tabs{display:flex;gap:4px;margin-bottom:8px}
.tab{padding:5px 12px;border-radius:6px;cursor:pointer;font-size:.75em;
  font-weight:600;background:rgba(255,255,255,.05);color:#888;
  border:1px solid transparent;transition:.2s}
.tab.active{background:rgba(253,203,110,.1);color:#feca57;
  border-color:rgba(253,203,110,.3)}
.tab-content{display:none}
.tab-content.active{display:block}
</style>
</head>
<body>
<div class="w">
  <div class="hdr">
    <h1>🎵 {{ station }}</h1>
    <div class="s">DJ Control Panel v5.0</div>
  </div>

  {% if msg %}
  <div class="fl {{ msg_type }}">{{ msg }}</div>
  {% endif %}

  <div class="chips">
    <div class="ch {{ 'g' if js_runtime != 'NINCS' else 'r' }}">
      <b>JS:</b> {{ js_runtime }}</div>
    <div class="ch {{ 'g' if connected else 'r' }}">
      <b>SHOUTcast:</b>
      {{ 'Online' if connected else 'Offline' }}</div>
    <div class="ch {{ 'g' if has_cookies else 'r' }}">
      <b>🍪:</b> {{ auth_method }}</div>
    <div class="ch y">
      <b>Dalok:</b>
      <span id="tot">{{ total }}</span></div>
  </div>

  <div class="sb">
    <div class="si">
      <div class="dt {{ 'on' if connected else 'off' }}"
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
        <button class="bt sk">⏭</button></form>
      {% endif %}
      <form action="/clear" method="post" style="display:inline">
        <button class="bt cl">🗑</button></form>
    </div>
  </div>

  <div class="cd">
    <h3 class="c">🎧 Most Szól</h3>
    <div class="np">
      {% if connected %}
      <div class="eq"><i></i><i></i><i></i><i></i><i></i></div>
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

  <!-- COOKIE KEZELÉS -->
  <div class="cookie-box">
    <h3>🍪 YouTube Hitelesítés
      {% if has_cookies %}
        <span style="color:#55efc4">✅ Aktív</span>
      {% else %}
        <span style="color:#ff7675">❌ Szükséges!</span>
      {% endif %}
    </h3>
    <p>
      A YouTube bot-védelme miatt cookie szükséges.
      <b>Lépések:</b><br>
      1. Telepítsd a
      <a href="https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc"
         target="_blank" style="color:#74b9ff">
        "Get cookies.txt LOCALLY"</a> Chrome bővítményt<br>
      2. Jelentkezz be YouTube-ra<br>
      3. A youtube.com oldalon kattints a bővítményre →
         "Export" → Másold ki<br>
      4. Illeszd be ide alább VAGY töltsd fel fájlként
    </p>

    <div class="tabs">
      <div class="tab active" onclick="showTab(0)">
        📋 Beillesztés</div>
      <div class="tab" onclick="showTab(1)">
        📁 Fájl feltöltés</div>
    </div>

    <!-- Beillesztés -->
    <div class="tab-content active" id="tab0">
      <form action="/paste_cookies" method="post">
        <textarea name="cookie_text"
          placeholder="# Netscape HTTP Cookie File
# ide illeszd be a cookies.txt tartalmát...
.youtube.com	TRUE	/	TRUE	..."></textarea>
        <div class="row" style="margin-top:6px">
          <button type="submit"
                  class="btn-sm btn-cookie">
            🍪 Cookie mentés</button>
        </div>
      </form>
    </div>

    <!-- Fájl feltöltés -->
    <div class="tab-content" id="tab1">
      <form action="/upload_cookies" method="post"
            enctype="multipart/form-data">
        <div class="row">
          <input type="file" name="cookiefile"
                 accept=".txt">
          <button type="submit"
                  class="btn-sm btn-cookie">
            📁 Feltöltés</button>
        </div>
      </form>
    </div>

    <!-- Teszt -->
    <div class="row" style="margin-top:6px">
      <form action="/test_yt" method="post"
            style="display:inline">
        <button class="btn-sm btn-test">
          🔍 YouTube Teszt</button>
      </form>
    </div>
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
    <a href="http://szaby.radio12345.com"
       target="_blank">szaby.radio12345.com</a>
    <a href="http://szaby.radiostream321.com"
       target="_blank">szaby.radiostream321.com</a>
    <a href="http://szaby.radiostream123.com"
       target="_blank">szaby.radiostream123.com</a>
  </div>

  <div class="ft">☁️ Render.com</div>
</div>

<script>
function x(s){var d=document.createElement('div');
  d.textContent=s;return d.innerHTML}

function showTab(n){
  document.querySelectorAll('.tab').forEach(
    (t,i)=>t.classList.toggle('active',i===n));
  document.querySelectorAll('.tab-content').forEach(
    (t,i)=>t.classList.toggle('active',i===n));
}

setInterval(()=>{
  fetch('/api/status').then(r=>r.json()).then(d=>{
    document.getElementById('dot').className=
      'dt '+(d.connected?'on':'off');
    document.getElementById('st').textContent=
      d.connected?'STREAMING':'OFFLINE';
    document.getElementById('now').textContent=
      d.current.title;
    document.getElementById('qc').textContent=
      d.queue_count;
    document.getElementById('tot').textContent=
      d.total;
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


# =============================================
#  MAIN
# =============================================
if __name__ == '__main__':
    ck = '✅ Van' if HAS_COOKIES else '❌ NINCS'
    js = JS_RUNTIME or 'NINCS'
    print(f"""
╔═══════════════════════════════════════════════╗
║  🎵  {STATION_NAME} v5.0                      ║
║  Port: {WEB_PORT}                                   ║
║  JS: {js:<10} | Cookie: {ck:<20}║
║  SHOUTcast: {SHOUTCAST_HOST}:{SHOUTCAST_PORT}     ║
╚═══════════════════════════════════════════════╝
    """)
    if not HAS_COOKIES:
        print('⚠️  FIGYELEM: Nincs cookies.txt!')
        print('   A YouTube blokkolni fogja a kéréseket!')
        print('   Nyisd meg a web panelt és töltsd fel')
        print('   a cookie-kat!')
        print()

    app.run(
        host='0.0.0.0',
        port=WEB_PORT,
        debug=False,
        threaded=True
    )
