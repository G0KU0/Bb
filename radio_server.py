import socket
import subprocess
import threading
import queue
import time
import sys
import base64
from flask import (
    Flask, render_template_string, request, redirect, jsonify
)

# =============================================
#  BEÁLLÍTÁSOK / SETTINGS
# =============================================
SHOUTCAST_HOST     = 'uk3freenew.listen2myradio.com'
SHOUTCAST_PORT     = 31822
SHOUTCAST_PASSWORD = '2002'
BITRATE            = 128
STATION_NAME       = 'Szaby Radio'
STATION_GENRE      = 'Various'
STATION_URL        = 'http://szaby.radio12345.com'
WEB_PORT           = 5000
# =============================================

app = Flask(__name__)


# =============================================
#  GLOBAL STATE
# =============================================
class RadioState:
    def __init__(self):
        self.song_queue    = queue.Queue()
        self.display_queue = []
        self.lock          = threading.Lock()
        self.current       = {'title': 'Nincs zene lejátszás alatt', 'url': ''}
        self.streaming     = False
        self.connected     = False
        self.conn_method   = ''
        self.worker_thread = None
        self.ffmpeg_proc   = None
        self.yt_proc       = None
        self.skip_event    = threading.Event()
        self.logs          = []

    def log(self, msg):
        ts = time.strftime('%H:%M:%S')
        entry = f'[{ts}] {msg}'
        self.logs.append(entry)
        if len(self.logs) > 500:
            self.logs = self.logs[-300:]
        print(entry)

R = RadioState()


# =============================================
#  SHOUTCAST MULTI-PROTOCOL SOURCE CONNECTION
# =============================================
class ShoutcastSource:
    """
    Támogatott protokollok:
      1) SHOUTcast v1 legacy (port+1) — DNAS v2 kompatibilis
      2) SHOUTcast v2 / Icecast SOURCE metódus
      3) SHOUTcast v1 legacy (base port)
      4) SHOUTcast v1 alternatív (\\n line ending)
    """

    def __init__(self):
        self.sock  = None
        self.alive = False

    def connect(self):
        """Végigpróbálja az összes csatlakozási módszert"""

        methods = [
            ('SHOUTcast v1 Legacy (port+1)',   self._try_v1_port_plus),
            ('SHOUTcast v2 SOURCE (base port)', self._try_v2_source),
            ('SHOUTcast v1 Legacy (base port)', self._try_v1_base),
            ('SHOUTcast v1 Alt (\\n ending)',   self._try_v1_alt),
            ('Icecast PUT kompatibilis',        self._try_icecast_put),
        ]

        for name, method in methods:
            R.log(f'🔌 Próba: {name}...')
            try:
                if method():
                    self.alive     = True
                    R.connected    = True
                    R.conn_method  = name
                    R.log(f'✅ SIKER! Csatlakozva: {name}')
                    return True
                else:
                    R.log(f'   ✗ {name} — nem sikerült')
            except Exception as e:
                R.log(f'   ✗ {name} — hiba: {e}')
            self._close()

        R.log('❌ Egyik csatlakozási módszer sem működött!')
        R.log('   ► Ellenőrizd, hogy a szerver BE VAN-E KAPCSOLVA')
        R.log('     a listen2myradio.com panelen!')
        R.connected = False
        return False

    # ---- Method 1: SHOUTcast v1 on port+1 ----
    def _try_v1_port_plus(self):
        """
        SHOUTcast DNAS v2 legacy kompatibilitás:
        A source/DJ port = base_port + 1
        """
        source_port = SHOUTCAST_PORT + 1
        R.log(f'   Csatlakozás: {SHOUTCAST_HOST}:{source_port}')

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect((SHOUTCAST_HOST, source_port))
        R.log(f'   TCP OK (port {source_port})')

        # Jelszó küldése
        self.sock.sendall(f'{SHOUTCAST_PASSWORD}\r\n'.encode())
        time.sleep(2)

        resp = self._recv()
        R.log(f'   Válasz: {repr(resp)}')

        if 'OK' in resp.upper():
            self._send_icy_headers()
            return True

        # Néha a v1 nem mond "OK"-t, hanem egyből fogadja a headert
        if resp == '':
            R.log('   Üres válasz, próba headerekkel...')
            self._send_icy_headers()
            time.sleep(1)
            resp2 = self._recv()
            R.log(f'   Válasz headerek után: {repr(resp2)}')
            if 'OK' in resp2.upper() or resp2 == '':
                # Teszteljünk egy kis adat küldéssel
                return self._test_send()

        return False

    # ---- Method 2: SHOUTcast v2 SOURCE protocol ----
    def _try_v2_source(self):
        """
        SHOUTcast v2 DNAS / Icecast-kompatibilis SOURCE metódus
        HTTP-szerű kérés base64 hitelesítéssel
        """
        R.log(f'   Csatlakozás: {SHOUTCAST_HOST}:{SHOUTCAST_PORT}')

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect((SHOUTCAST_HOST, SHOUTCAST_PORT))
        R.log(f'   TCP OK (port {SHOUTCAST_PORT})')

        # Base64 hitelesítés
        auth_str = base64.b64encode(
            f'source:{SHOUTCAST_PASSWORD}'.encode()
        ).decode()

        # SOURCE kérés (SHOUTcast v2 stílus)
        http_req = (
            f'SOURCE /sid=1 ICE/1.0\r\n'
            f'Content-Type: audio/mpeg\r\n'
            f'Authorization: Basic {auth_str}\r\n'
            f'User-Agent: SzabyRadio/2.0\r\n'
            f'ice-name: {STATION_NAME}\r\n'
            f'ice-genre: {STATION_GENRE}\r\n'
            f'ice-bitrate: {BITRATE}\r\n'
            f'ice-private: 0\r\n'
            f'ice-public: 1\r\n'
            f'ice-url: {STATION_URL}\r\n'
            f'\r\n'
        )

        R.log(f'   SOURCE kérés küldése...')
        self.sock.sendall(http_req.encode())
        time.sleep(2)

        resp = self._recv()
        R.log(f'   Válasz: {repr(resp)}')

        if '200' in resp or 'OK' in resp.upper():
            return True
        if resp == '':
            return self._test_send()

        return False

    # ---- Method 3: SHOUTcast v1 on base port ----
    def _try_v1_base(self):
        """Klasszikus v1 protokoll az alap porton"""
        R.log(f'   Csatlakozás: {SHOUTCAST_HOST}:{SHOUTCAST_PORT}')

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect((SHOUTCAST_HOST, SHOUTCAST_PORT))
        R.log(f'   TCP OK (port {SHOUTCAST_PORT})')

        # Jelszó küldése \r\n-nel
        self.sock.sendall(f'{SHOUTCAST_PASSWORD}\r\n'.encode())
        time.sleep(2)

        resp = self._recv()
        R.log(f'   Válasz: {repr(resp)}')

        if 'OK' in resp.upper():
            self._send_icy_headers()
            return True
        return False

    # ---- Method 4: v1 with just \n ----
    def _try_v1_alt(self):
        """v1 protokoll csak \\n sorvégzéssel"""
        R.log(f'   Csatlakozás: {SHOUTCAST_HOST}:{SHOUTCAST_PORT}')

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect((SHOUTCAST_HOST, SHOUTCAST_PORT))
        R.log(f'   TCP OK')

        self.sock.sendall(f'{SHOUTCAST_PASSWORD}\n'.encode())
        time.sleep(2)

        resp = self._recv()
        R.log(f'   Válasz: {repr(resp)}')

        if 'OK' in resp.upper():
            self._send_icy_headers_alt()
            return True
        return False

    # ---- Method 5: Icecast PUT ----
    def _try_icecast_put(self):
        """Icecast-kompatibilis PUT metódus"""
        R.log(f'   Csatlakozás: {SHOUTCAST_HOST}:{SHOUTCAST_PORT}')

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect((SHOUTCAST_HOST, SHOUTCAST_PORT))

        auth_str = base64.b64encode(
            f'source:{SHOUTCAST_PASSWORD}'.encode()
        ).decode()

        http_req = (
            f'PUT / HTTP/1.1\r\n'
            f'Host: {SHOUTCAST_HOST}:{SHOUTCAST_PORT}\r\n'
            f'Authorization: Basic {auth_str}\r\n'
            f'User-Agent: SzabyRadio/2.0\r\n'
            f'Content-Type: audio/mpeg\r\n'
            f'ice-name: {STATION_NAME}\r\n'
            f'ice-genre: {STATION_GENRE}\r\n'
            f'ice-bitrate: {BITRATE}\r\n'
            f'ice-public: 1\r\n'
            f'Expect: 100-continue\r\n'
            f'Transfer-Encoding: chunked\r\n'
            f'\r\n'
        )

        self.sock.sendall(http_req.encode())
        time.sleep(2)

        resp = self._recv()
        R.log(f'   Válasz: {repr(resp)}')

        if '200' in resp or '100' in resp or 'OK' in resp.upper():
            return True
        return False

    # ---- Helper methods ----
    def _recv(self):
        try:
            data = self.sock.recv(4096)
            return data.decode(errors='ignore').strip()
        except socket.timeout:
            return ''
        except Exception:
            return ''

    def _send_icy_headers(self):
        headers = (
            f'content-type:audio/mpeg\r\n'
            f'icy-name:{STATION_NAME}\r\n'
            f'icy-genre:{STATION_GENRE}\r\n'
            f'icy-url:{STATION_URL}\r\n'
            f'icy-pub:1\r\n'
            f'icy-br:{BITRATE}\r\n'
            f'\r\n'
        )
        self.sock.sendall(headers.encode())

    def _send_icy_headers_alt(self):
        headers = (
            f'content-type:audio/mpeg\n'
            f'icy-name:{STATION_NAME}\n'
            f'icy-genre:{STATION_GENRE}\n'
            f'icy-url:{STATION_URL}\n'
            f'icy-pub:1\n'
            f'icy-br:{BITRATE}\n'
            f'\n'
        )
        self.sock.sendall(headers.encode())

    def _test_send(self):
        """Teszt: küldünk néhány byte üres MP3 adatot"""
        try:
            # Minimal MP3 frame header (silent frame)
            silent = b'\xff\xfb\x90\x00' + b'\x00' * 417
            self.sock.sendall(silent)
            time.sleep(0.5)
            self.sock.settimeout(None)
            return True
        except Exception as e:
            R.log(f'   Teszt küldés hiba: {e}')
            return False

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
#  FFMPEG DIRECT ICECAST MODE
#  (Alternatív módszer - ffmpeg maga csatlakozik)
# =============================================
class FFmpegDirectMode:
    """
    Nem socket-alapú: ffmpeg közvetlenül csatlakozik
    a SHOUTcast szerverre icecast protokollon.
    """

    @staticmethod
    def play(youtube_url):
        title = yt_get_title(youtube_url)
        R.current = {'title': title, 'url': youtube_url}
        R.log(f'🎵 [Direct] Most szól: {title}')

        audio_url = yt_get_audio_url(youtube_url)
        if not audio_url:
            R.log('❌ Nem sikerült a YouTube audio URL lekérése')
            return True

        # ffmpeg icecast output URL
        icecast_url = (
            f'icecast://source:{SHOUTCAST_PASSWORD}'
            f'@{SHOUTCAST_HOST}:{SHOUTCAST_PORT}/'
        )

        cmd = [
            'ffmpeg', '-hide_banner', '-loglevel', 'warning',
            '-reconnect', '1',
            '-reconnect_streamed', '1',
            '-reconnect_delay_max', '5',
            '-i', audio_url,
            '-vn',
            '-c:a', 'libmp3lame',
            '-b:a', f'{BITRATE}k',
            '-ar', '44100',
            '-ac', '2',
            '-f', 'mp3',
            '-content_type', 'audio/mpeg',
            icecast_url
        ]

        R.skip_event.clear()
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            R.ffmpeg_proc = proc
            R.connected   = True

            while proc.poll() is None and not R.skip_event.is_set() \
                  and R.streaming:
                time.sleep(0.5)

            if R.skip_event.is_set():
                R.log(f'⏭ Kihagyva: {title}')
            else:
                R.log(f'✅ Kész: {title}')

            stderr_out = ''
            try:
                proc.terminate()
                proc.wait(timeout=5)
                stderr_out = proc.stderr.read().decode(errors='ignore')
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

            if stderr_out:
                for line in stderr_out.strip().split('\n')[-3:]:
                    if line.strip():
                        R.log(f'   ffmpeg: {line.strip()[:120]}')

            return True

        except Exception as e:
            R.log(f'❌ FFmpeg direct hiba: {e}')
            return True
        finally:
            R.ffmpeg_proc = None


# =============================================
#  YOUTUBE HELPERS
# =============================================
def yt_get_title(url):
    try:
        r = subprocess.run(
            ['yt-dlp', '--get-title', '--no-playlist', url],
            capture_output=True, text=True, timeout=30
        )
        t = r.stdout.strip()
        return t if t else 'Ismeretlen cím'
    except Exception:
        return 'Ismeretlen cím'


def yt_get_audio_url(url):
    try:
        r = subprocess.run(
            ['yt-dlp',
             '-f', 'bestaudio[ext=m4a]/bestaudio',
             '--get-url', '--no-playlist', url],
            capture_output=True, text=True, timeout=30
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
        R.log(f'   yt-dlp hiba: {r.stderr[:200] if r.stderr else "?"}')
        return None
    except Exception as e:
        R.log(f'   yt-dlp kivétel: {e}')
        return None


# =============================================
#  STREAMING ENGINE (SOCKET MODE)
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
    title = yt_get_title(youtube_url)
    R.current = {'title': title, 'url': youtube_url}
    R.log(f'🎵 Most szól: {title}')

    audio_url = yt_get_audio_url(youtube_url)
    if not audio_url:
        R.log('❌ Audio URL hiba, pipe mód...')
        return _stream_with_pipe(sc, youtube_url, title)

    return _stream_with_url(sc, audio_url, title)


def _stream_with_url(sc, audio_url, title):
    cmd = [
        'ffmpeg', '-hide_banner', '-loglevel', 'error',
        '-reconnect', '1',
        '-reconnect_streamed', '1',
        '-reconnect_delay_max', '5',
        '-i', audio_url,
        '-vn',
        '-c:a', 'libmp3lame',
        '-b:a', f'{BITRATE}k',
        '-ar', '44100',
        '-ac', '2',
        '-f', 'mp3',
        'pipe:1'
    ]
    return _do_stream(sc, cmd, title)


def _stream_with_pipe(sc, youtube_url, title):
    yt_cmd = [
        'yt-dlp', '-f', 'bestaudio', '-o', '-',
        '--no-playlist', '--no-warnings', youtube_url
    ]
    ff_cmd = [
        'ffmpeg', '-hide_banner', '-loglevel', 'error',
        '-i', 'pipe:0',
        '-vn', '-c:a', 'libmp3lame',
        '-b:a', f'{BITRATE}k',
        '-ar', '44100', '-ac', '2',
        '-f', 'mp3', 'pipe:1'
    ]
    try:
        yt_proc = subprocess.Popen(
            yt_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        R.yt_proc = yt_proc
        ff_proc = subprocess.Popen(
            ff_cmd, stdin=yt_proc.stdout,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        yt_proc.stdout.close()
        return _do_stream_proc(sc, ff_proc, yt_proc, title)
    except Exception as e:
        R.log(f'❌ Pipe hiba: {e}')
        return True


def _do_stream(sc, ffmpeg_cmd, title):
    try:
        proc = subprocess.Popen(
            ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        return _do_stream_proc(sc, proc, None, title)
    except Exception as e:
        R.log(f'❌ FFmpeg hiba: {e}')
        return True


def _do_stream_proc(sc, ff_proc, yt_proc, title):
    R.ffmpeg_proc = ff_proc
    R.skip_event.clear()

    bps       = (BITRATE * 1000) / 8
    chunk     = 4096
    t_start   = time.time()
    total_sent = 0

    try:
        while R.streaming and not R.skip_event.is_set():
            data = ff_proc.stdout.read(chunk)
            if not data:
                break

            if not sc.send(data):
                _kill(ff_proc)
                if yt_proc:
                    _kill(yt_proc)
                R.ffmpeg_proc = None
                R.yt_proc     = None
                return False

            total_sent += len(data)
            expected = total_sent / bps
            elapsed  = time.time() - t_start
            if expected > elapsed:
                time.sleep(expected - elapsed)

    except Exception as e:
        R.log(f'❌ Stream hiba: {e}')

    _kill(ff_proc)
    if yt_proc:
        _kill(yt_proc)
    R.ffmpeg_proc = None
    R.yt_proc     = None

    if R.skip_event.is_set():
        R.log(f'⏭ Kihagyva: {title}')
    else:
        R.log(f'✅ Kész: {title}')
    return True


# =============================================
#  STREAM WORKER THREAD
# =============================================
def stream_worker_socket():
    """Socket-alapú streaming (próbálja az összes protokollt)"""
    sc = ShoutcastSource()

    if not sc.connect():
        R.streaming = False
        return

    while R.streaming:
        try:
            url = R.song_queue.get(timeout=3)
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
            time.sleep(3)
            if not sc.connect():
                R.log('❌ Újracsatlakozás sikertelen!')
                break

    sc.disconnect()
    R.streaming = False
    R.connected = False
    R.current   = {'title': 'Nincs zene lejátszás alatt', 'url': ''}
    R.log('⏹ Stream worker leállt.')


def stream_worker_direct():
    """FFmpeg direct mode (ffmpeg maga csatlakozik a szerverre)"""
    R.log('🎧 FFmpeg Direct mód indítása...')
    R.connected = True

    while R.streaming:
        try:
            url = R.song_queue.get(timeout=3)
        except queue.Empty:
            continue

        with R.lock:
            if R.display_queue:
                R.display_queue.pop(0)

        FFmpegDirectMode.play(url)
        try:
            R.song_queue.task_done()
        except ValueError:
            pass

    R.streaming = False
    R.connected = False
    R.current   = {'title': 'Nincs zene lejátszás alatt', 'url': ''}
    R.log('⏹ Direct worker leállt.')


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
        station    = STATION_NAME,
        connected  = R.connected,
        streaming  = R.streaming,
        conn_method = R.conn_method,
        current    = R.current,
        queue      = dq,
        logs       = R.logs[-50:],
        msg        = msg,
        msg_type   = msg_type,
        host       = SHOUTCAST_HOST,
        port       = SHOUTCAST_PORT
    )


@app.route('/add', methods=['POST'])
def add_song():
    url = request.form.get('url', '').strip()
    if not url:
        return redirect('/?msg=Add+meg+a+YouTube+linket!&t=error')
    if 'youtube.com' not in url and 'youtu.be' not in url:
        return redirect('/?msg=Csak+YouTube+link+elfogadott!&t=error')

    title = yt_get_title(url)
    with R.lock:
        R.display_queue.append({'title': title, 'url': url})
    R.song_queue.put(url)
    R.log(f'➕ Hozzáadva: {title}')
    return redirect('/?msg=Hozzáadva!&t=success')


@app.route('/start', methods=['POST'])
def start_stream():
    mode = request.form.get('mode', 'socket')
    if R.streaming:
        return redirect('/?msg=Már+fut!&t=error')
    R.streaming = True

    if mode == 'direct':
        R.log('▶ Indítás FFmpeg Direct módban...')
        R.conn_method = 'FFmpeg Direct (Icecast)'
        R.worker_thread = threading.Thread(
            target=stream_worker_direct, daemon=True
        )
    else:
        R.log('▶ Indítás Socket módban (auto-detect)...')
        R.worker_thread = threading.Thread(
            target=stream_worker_socket, daemon=True
        )

    R.worker_thread.start()
    time.sleep(3)
    return redirect('/?msg=Stream+elindítva!&t=success')


@app.route('/stop', methods=['POST'])
def stop_stream():
    R.streaming = False
    R.skip_event.set()
    if R.ffmpeg_proc:
        _kill(R.ffmpeg_proc)
    if R.yt_proc:
        _kill(R.yt_proc)
    R.log('⏹ Leállítás...')
    return redirect('/?msg=Leállítva!&t=success')


@app.route('/skip', methods=['POST'])
def skip_song():
    if not R.streaming:
        return redirect('/?msg=Nincs+aktív+stream!&t=error')
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
    R.log('🗑 Várólista törölve')
    return redirect('/?msg=Törölve!&t=success')


@app.route('/test', methods=['POST'])
def test_connection():
    """Tesztel minden csatlakozási módszert"""
    R.log('🔍 === KAPCSOLAT TESZT INDÍTÁSA ===')

    # DNS teszt
    try:
        ip = socket.gethostbyname(SHOUTCAST_HOST)
        R.log(f'   DNS OK: {SHOUTCAST_HOST} → {ip}')
    except Exception as e:
        R.log(f'   ❌ DNS hiba: {e}')
        return redirect('/?msg=DNS+hiba!&t=error')

    # Port tesztek
    for port in [SHOUTCAST_PORT, SHOUTCAST_PORT + 1]:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect((SHOUTCAST_HOST, port))
            R.log(f'   ✅ Port {port} NYITVA')

            # Próbáljunk olvasni - hátha a szerver küld valamit
            try:
                s.settimeout(3)
                initial = s.recv(4096).decode(errors='ignore')
                if initial:
                    R.log(f'      Szerver üzenet: {repr(initial[:200])}')
                else:
                    R.log(f'      (nincs kezdeti üzenet)')
            except socket.timeout:
                R.log(f'      (nincs kezdeti üzenet)')

            s.close()
        except Exception as e:
            R.log(f'   ❌ Port {port} ZÁRT: {e}')

    # SHOUTcast admin panel teszt
    try:
        import urllib.request
        url = f'http://{SHOUTCAST_HOST}:{SHOUTCAST_PORT}'
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0'
        })
        resp = urllib.request.urlopen(req, timeout=10)
        body = resp.read(2000).decode(errors='ignore')
        R.log(f'   HTTP válasz ({resp.status}): {body[:200]}')
        if 'shoutcast' in body.lower() or 'SHOUTcast' in body:
            R.log('   ✅ SHOUTcast szerver észlelve!')
        if 'icecast' in body.lower():
            R.log('   ✅ Icecast szerver észlelve!')
    except Exception as e:
        R.log(f'   HTTP teszt: {e}')

    R.log('🔍 === TESZT VÉGE ===')
    return redirect('/?msg=Teszt+kész,+nézd+a+naplót!&t=success')


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
        'logs':        R.logs[-50:]
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
<title>{{ station }} — DJ Panel v2</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{
  font-family:'Segoe UI',Tahoma,sans-serif;
  background:linear-gradient(135deg,#0f0c29,#302b63,#24243e);
  color:#e0e0e0;min-height:100vh;
}
.wrap{max-width:960px;margin:0 auto;padding:20px}

.hdr{text-align:center;padding:24px 0;margin-bottom:24px;
     border-bottom:2px solid rgba(255,255,255,.08)}
.hdr h1{font-size:2.2em;
  background:linear-gradient(45deg,#ff6b6b,#feca57,#48dbfb,#a29bfe);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;
  background-clip:text}
.hdr .sub{color:#777;font-size:.95em;margin-top:4px}
.hdr .ver{color:#555;font-size:.75em;margin-top:2px}

.flash{padding:12px 20px;border-radius:10px;margin-bottom:16px;font-weight:500}
.flash.success{background:rgba(0,184,148,.15);border:1px solid #00b894;color:#55efc4}
.flash.error{background:rgba(214,48,49,.15);border:1px solid #d63031;color:#ff7675}

/* Warning box */
.warn{background:rgba(253,203,110,.1);border:1px solid #fdcb6e;
      border-radius:12px;padding:16px 20px;margin-bottom:18px;color:#ffeaa7}
.warn b{color:#fdcb6e}
.warn a{color:#74b9ff;text-decoration:underline}

.status-bar{display:flex;justify-content:space-between;align-items:center;
  background:rgba(255,255,255,.04);border-radius:14px;padding:14px 22px;
  margin-bottom:18px;border:1px solid rgba(255,255,255,.08);flex-wrap:wrap;gap:10px}
.status-ind{display:flex;align-items:center;gap:10px;font-weight:500}
.dot{width:13px;height:13px;border-radius:50%;flex-shrink:0}
.dot.on{background:#00ff88;box-shadow:0 0 12px #00ff88;animation:pulse 2s infinite}
.dot.off{background:#ff4757;box-shadow:0 0 8px #ff4757}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.method-badge{font-size:.72em;background:rgba(108,92,231,.3);
  padding:3px 10px;border-radius:6px;color:#a29bfe;margin-left:6px}
.controls{display:flex;gap:6px;flex-wrap:wrap}

.btn{padding:9px 18px;border:none;border-radius:8px;cursor:pointer;
     font-size:12px;font-weight:700;transition:.25s;text-transform:uppercase;
     letter-spacing:.6px;color:#fff}
.btn:hover{transform:translateY(-2px);box-shadow:0 5px 15px rgba(0,0,0,.35)}
.btn-go{background:linear-gradient(135deg,#00b894,#00cec9)}
.btn-go2{background:linear-gradient(135deg,#6c5ce7,#a29bfe)}
.btn-stop{background:linear-gradient(135deg,#e17055,#d63031)}
.btn-skip{background:linear-gradient(135deg,#fdcb6e,#e17055)}
.btn-clr{background:linear-gradient(135deg,#636e72,#2d3436)}
.btn-test{background:linear-gradient(135deg,#0984e3,#74b9ff)}

.card{background:rgba(255,255,255,.04);border-radius:14px;padding:20px 24px;
      margin-bottom:18px;border:1px solid rgba(255,255,255,.07)}
.card h3{font-size:.8em;text-transform:uppercase;letter-spacing:2px;
         margin-bottom:10px;font-weight:700}
.card h3.cyan{color:#48dbfb}
.card h3.yellow{color:#feca57}
.card h3.red{color:#ff6b6b}
.card h3.green{color:#00ff88}
.card h3.purple{color:#a29bfe}

#current-title{font-size:1.3em;font-weight:600;color:#fff;word-break:break-word}

.input-row{display:flex;gap:10px}
.input-row input[type=text]{flex:1;padding:12px 16px;border-radius:10px;
  border:1px solid rgba(255,255,255,.15);background:rgba(255,255,255,.06);
  color:#fff;font-size:14px;outline:none;transition:.3s}
.input-row input[type=text]:focus{border-color:#a29bfe}
.input-row input::placeholder{color:#555}
.btn-add{background:linear-gradient(135deg,#6c5ce7,#a29bfe);color:#fff;
  padding:12px 24px;border:none;border-radius:10px;cursor:pointer;
  font-size:14px;font-weight:700;transition:.25s;white-space:nowrap}
.btn-add:hover{transform:translateY(-2px);box-shadow:0 5px 15px rgba(108,92,231,.4)}

.q-item{display:flex;align-items:center;padding:9px 13px;
  background:rgba(255,255,255,.025);border-radius:8px;margin-bottom:6px;
  border-left:3px solid #6c5ce7}
.q-item .num{color:#a29bfe;font-weight:700;margin-right:12px;min-width:22px;
             text-align:right}
.q-item .stitle{flex:1;font-size:.9em;word-break:break-word}
.q-empty{color:#555;text-align:center;padding:16px;font-style:italic}

.log-box{background:rgba(0,0,0,.3);border-radius:12px;padding:16px;
         margin-bottom:18px;border:1px solid rgba(255,255,255,.04)}
.log-box h3{color:#55efc4;font-size:.8em;text-transform:uppercase;
            letter-spacing:2px;margin-bottom:8px}
#log-content{max-height:260px;overflow-y:auto;font-family:'Courier New',monospace;
  font-size:.78em;color:#888;line-height:1.65}
#log-content::-webkit-scrollbar{width:5px}
#log-content::-webkit-scrollbar-thumb{background:#444;border-radius:3px}

.links{text-align:center;padding:16px;background:rgba(255,255,255,.025);
       border-radius:12px;border:1px solid rgba(255,255,255,.06)}
.links h3{color:#48dbfb;font-size:.8em;text-transform:uppercase;
          letter-spacing:2px;margin-bottom:8px}
.links a{color:#feca57;text-decoration:none;margin:0 10px;font-weight:500}
.links a:hover{text-decoration:underline}

.mode-box{display:flex;gap:10px;margin-top:12px;flex-wrap:wrap;
          align-items:center;justify-content:center}
.mode-box label{font-size:.85em;color:#aaa}
.mode-desc{font-size:.72em;color:#666;margin-top:6px;text-align:center}
</style>
</head>
<body>
<div class="wrap">

  <div class="hdr">
    <h1>🎵 {{ station }}</h1>
    <div class="sub">DJ Control Panel</div>
    <div class="ver">v2.0 — Multi-protocol • {{ host }}:{{ port }}</div>
  </div>

  {% if msg %}
  <div class="flash {{ msg_type }}">{{ msg }}</div>
  {% endif %}

  <!-- WARNING -->
  <div class="warn">
    <b>⚠️ FONTOS:</b> A listen2myradio.com panelen a szerverednek
    <b>ON</b> állapotban kell lennie!
    <a href="http://uk3freenew.listen2myradio.com:31822/index.html"
       target="_blank">Admin Panel megnyitása →</a>
  </div>

  <!-- STATUS -->
  <div class="status-bar">
    <div class="status-ind">
      <div class="dot {{ 'on' if connected else 'off' }}" id="status-dot"></div>
      <span id="status-text">
        {{ 'ONLINE' if connected else 'OFFLINE' }}
      </span>
      {% if conn_method and connected %}
      <span class="method-badge" id="method-badge">{{ conn_method }}</span>
      {% endif %}
    </div>
    <div class="controls">
      {% if not streaming %}
      <!-- Socket mode -->
      <form action="/start" method="post" style="display:inline">
        <input type="hidden" name="mode" value="socket">
        <button class="btn btn-go" type="submit"
                title="Socket mód - auto-detect protokoll">
          ▶ Socket Mód
        </button>
      </form>
      <!-- Direct mode -->
      <form action="/start" method="post" style="display:inline">
        <input type="hidden" name="mode" value="direct">
        <button class="btn btn-go2" type="submit"
                title="FFmpeg közvetlenül csatlakozik icecast protokollon">
          ▶ Direct Mód
        </button>
      </form>
      <!-- Test -->
      <form action="/test" method="post" style="display:inline">
        <button class="btn btn-test" type="submit">🔍 Teszt</button>
      </form>
      {% else %}
      <form action="/stop" method="post" style="display:inline">
        <button class="btn btn-stop" type="submit">■ Leállítás</button>
      </form>
      <form action="/skip" method="post" style="display:inline">
        <button class="btn btn-skip" type="submit">⏭ Következő</button>
      </form>
      {% endif %}
      <form action="/clear" method="post" style="display:inline">
        <button class="btn btn-clr" type="submit">🗑</button>
      </form>
    </div>
  </div>

  {% if not streaming %}
  <div class="mode-desc">
    <b>Socket Mód:</b> Automatikusan próbálja: v1 port+1, v2 SOURCE,
    v1 alap, Icecast PUT &nbsp;|&nbsp;
    <b>Direct Mód:</b> FFmpeg közvetlenül csatlakozik (Icecast protokoll)
    &nbsp;|&nbsp;
    <b>Teszt:</b> Portok és szerver ellenőrzése
  </div>
  {% endif %}

  <!-- NOW PLAYING -->
  <div class="card">
    <h3 class="cyan">🎧 Most Szól</h3>
    <div id="current-title">{{ current.title }}</div>
  </div>

  <!-- ADD -->
  <div class="card">
    <h3 class="yellow">➕ Zene Hozzáadása</h3>
    <form action="/add" method="post">
      <div class="input-row">
        <input type="text" name="url"
               placeholder="YouTube link beillesztése..." required>
        <button type="submit" class="btn-add">Hozzáadás</button>
      </div>
    </form>
  </div>

  <!-- QUEUE -->
  <div class="card">
    <h3 class="red">📋 Várólista
      (<span id="queue-count">{{ queue|length }}</span>)</h3>
    <div id="queue-list">
      {% if queue %}
        {% for item in queue %}
        <div class="q-item">
          <span class="num">{{ loop.index }}.</span>
          <span class="stitle">{{ item.title }}</span>
        </div>
        {% endfor %}
      {% else %}
        <div class="q-empty">Üres várólista</div>
      {% endif %}
    </div>
  </div>

  <!-- LOG -->
  <div class="log-box">
    <h3>📊 Napló</h3>
    <div id="log-content">
      {% for l in logs %}<div>{{ l }}</div>{% endfor %}
    </div>
  </div>

  <!-- LINKS -->
  <div class="links">
    <h3>🔗 Hallgatás</h3>
    <a href="http://szaby.radio12345.com" target="_blank">
      szaby.radio12345.com</a>
    <a href="http://szaby.radiostream321.com" target="_blank">
      szaby.radiostream321.com</a>
    <a href="http://szaby.radiostream123.com" target="_blank">
      szaby.radiostream123.com</a>
  </div>

</div>

<script>
function esc(s){
  var d=document.createElement('div');d.textContent=s;return d.innerHTML;
}
setInterval(function(){
  fetch('/api/status')
    .then(function(r){return r.json()})
    .then(function(d){
      document.getElementById('status-dot').className=
        'dot '+(d.connected?'on':'off');
      document.getElementById('status-text').textContent=
        d.connected?'ONLINE':'OFFLINE';
      document.getElementById('current-title').textContent=
        d.current.title;
      document.getElementById('queue-count').textContent=
        d.queue_count;

      var html='';
      if(d.queue.length===0){
        html='<div class="q-empty">Üres várólista</div>';
      }else{
        d.queue.forEach(function(it,i){
          html+='<div class="q-item"><span class="num">'+(i+1)+
            '.</span><span class="stitle">'+esc(it.title)+'</span></div>';
        });
      }
      document.getElementById('queue-list').innerHTML=html;

      var lh='';
      d.logs.forEach(function(l){lh+='<div>'+esc(l)+'</div>';});
      var el=document.getElementById('log-content');
      el.innerHTML=lh;
      el.scrollTop=el.scrollHeight;
    }).catch(function(){});
},3000);
(function(){
  var el=document.getElementById('log-content');
  if(el)el.scrollTop=el.scrollHeight;
})();
</script>
</body>
</html>
"""


# =============================================
#  STARTUP
# =============================================
def check_deps():
    missing = []
    for prog in ['yt-dlp', 'ffmpeg']:
        try:
            subprocess.run(
                [prog, '--version'],
                capture_output=True, timeout=10
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            missing.append(prog)

    if missing:
        print(f'\n❌ Hiányzó: {", ".join(missing)}')
        if 'yt-dlp' in missing:
            print('  pip install yt-dlp')
        if 'ffmpeg' in missing:
            print('  https://ffmpeg.org/download.html')
        sys.exit(1)


if __name__ == '__main__':
    check_deps()

    print(f"""
╔═══════════════════════════════════════════════════╗
║   🎵  {STATION_NAME} — DJ Panel v2.0              ║
║                                                   ║
║   Web:    http://localhost:{WEB_PORT}                    ║
║   Server: {SHOUTCAST_HOST}:{SHOUTCAST_PORT}            ║
║                                                   ║
║   ⚠️  Először kapcsold BE a szervert a             ║
║      listen2myradio.com admin panelen!             ║
╚═══════════════════════════════════════════════════╝
    """)

    app.run(host='0.0.0.0', port=WEB_PORT, debug=False, threaded=True)
