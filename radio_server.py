import socket
import subprocess
import threading
import queue
import time
import sys
from flask import (
    Flask, render_template_string, request, redirect, jsonify
)

# =============================================
#  BEÁLLÍTÁSOK / SETTINGS
# =============================================
SHOUTCAST_HOST     = 'uk3freenew.listen2myradio.com'
SHOUTCAST_PORT     = 31822
SHOUTCAST_PASSWORD = '2002'
BITRATE            = 128        # kbps
STATION_NAME       = 'Szaby Radio'
STATION_GENRE      = 'Various'
WEB_PORT           = 5000       # Web panel port
# =============================================

app = Flask(__name__)


# =============================================
#  GLOBAL STATE
# =============================================
class RadioState:
    def __init__(self):
        self.song_queue   = queue.Queue()
        self.display_queue = []
        self.lock          = threading.Lock()
        self.current       = {'title': 'Nincs zene lejátszás alatt', 'url': ''}
        self.streaming     = False
        self.connected     = False
        self.worker_thread = None
        self.ffmpeg_proc   = None
        self.yt_proc       = None
        self.skip_event    = threading.Event()
        self.logs          = []

    def log(self, msg):
        ts = time.strftime('%H:%M:%S')
        entry = f'[{ts}] {msg}'
        self.logs.append(entry)
        if len(self.logs) > 300:
            self.logs = self.logs[-200:]
        print(entry)

R = RadioState()


# =============================================
#  SHOUTCAST SOURCE CONNECTION
# =============================================
class ShoutcastSource:
    """SHOUTcast v1 source/DJ protocol implementation"""

    def __init__(self):
        self.sock  = None
        self.alive = False

    def connect(self):
        try:
            R.log(f'🔌 Csatlakozás: {SHOUTCAST_HOST}:{SHOUTCAST_PORT}')
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(15)
            self.sock.connect((SHOUTCAST_HOST, SHOUTCAST_PORT))
            R.log('   TCP kapcsolat OK')

            # 1) Jelszó küldése
            self.sock.sendall(f'{SHOUTCAST_PASSWORD}\r\n'.encode())
            time.sleep(1.5)

            # 2) Válasz fogadása
            resp = self.sock.recv(4096).decode(errors='ignore')
            R.log(f'   Szerver válasz: {repr(resp.strip())}')
            if 'OK' not in resp.upper():
                raise Exception(f'Elutasítva: {resp.strip()}')

            # 3) ICY fejlécek küldése
            headers = (
                f'content-type:audio/mpeg\r\n'
                f'icy-name:{STATION_NAME}\r\n'
                f'icy-genre:{STATION_GENRE}\r\n'
                f'icy-br:{BITRATE}\r\n'
                f'icy-pub:1\r\n'
                f'\r\n'
            )
            self.sock.sendall(headers.encode())
            self.sock.settimeout(None)
            self.alive     = True
            R.connected    = True
            R.log('✅ Sikeresen csatlakozva a SHOUTcast szerverhez!')
            return True

        except Exception as e:
            R.log(f'❌ Csatlakozási hiba: {e}')
            self.alive  = False
            R.connected = False
            self._close()
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
        R.log('🔌 Lecsatlakozva a szerverről')

    def _close(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None


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
            ['yt-dlp', '-f', 'bestaudio', '--get-url', '--no-playlist', url],
            capture_output=True, text=True, timeout=30
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
        R.log(f'   yt-dlp hiba: {r.stderr[:200] if r.stderr else "unknown"}')
        return None
    except Exception as e:
        R.log(f'   yt-dlp kivétel: {e}')
        return None


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
    """
    Letölti a YouTube audiot és streameli a SHOUTcast szerverre.
    Return True  = folytatható a következő dal
    Return False = kapcsolat megszakadt, újra kell csatlakozni
    """
    title = yt_get_title(youtube_url)
    R.current = {'title': title, 'url': youtube_url}
    R.log(f'🎵 Most szól: {title}')

    # --- 1. próba: --get-url + ffmpeg ---
    audio_url = yt_get_audio_url(youtube_url)
    if audio_url:
        return _stream_with_url(sc, audio_url, title)

    # --- 2. próba: pipe mód (yt-dlp → ffmpeg) ---
    R.log('   Alternatív letöltési mód (pipe)...')
    return _stream_with_pipe(sc, youtube_url, title)


def _stream_with_url(sc, audio_url, title):
    """FFmpeg közvetlenül az URL-ről olvas"""
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
    """yt-dlp stdout → ffmpeg stdin → SHOUTcast"""
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
    """Elindítja az ffmpeg-et és streameli a kimenetet"""
    try:
        proc = subprocess.Popen(
            ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        return _do_stream_proc(sc, proc, None, title)
    except Exception as e:
        R.log(f'❌ FFmpeg indítási hiba: {e}')
        return True


def _do_stream_proc(sc, ff_proc, yt_proc, title):
    """Olvassa az ffmpeg stdout-ot és küldi a SHOUTcast-nak"""
    R.ffmpeg_proc = ff_proc
    R.skip_event.clear()

    bps       = (BITRATE * 1000) / 8   # bytes per second
    chunk     = 4096
    t_start   = time.time()
    total_sent = 0

    try:
        while R.streaming and not R.skip_event.is_set():
            data = ff_proc.stdout.read(chunk)
            if not data:
                break

            if not sc.send(data):
                # Kapcsolat megszakadt
                _kill(ff_proc)
                if yt_proc:
                    _kill(yt_proc)
                R.ffmpeg_proc = None
                R.yt_proc    = None
                return False    # jelzi, hogy reconnect kell

            total_sent += len(data)

            # Valós idejű tempó szabályozás
            expected_time = total_sent / bps
            elapsed       = time.time() - t_start
            if expected_time > elapsed:
                time.sleep(expected_time - elapsed)

    except Exception as e:
        R.log(f'❌ Stream hiba: {e}')

    _kill(ff_proc)
    if yt_proc:
        _kill(yt_proc)
    R.ffmpeg_proc = None
    R.yt_proc    = None

    if R.skip_event.is_set():
        R.log(f'⏭ Kihagyva: {title}')
    else:
        R.log(f'✅ Kész: {title}')
    return True


# =============================================
#  STREAM WORKER THREAD
# =============================================
def stream_worker():
    sc = ShoutcastSource()

    if not sc.connect():
        R.log('❌ Nem sikerült csatlakozni! Leállás.')
        R.streaming = False
        return

    while R.streaming:
        # Várakozás a következő dalra
        try:
            url = R.song_queue.get(timeout=3)
        except queue.Empty:
            continue

        # Eltávolítás a megjelenítési listából
        with R.lock:
            if R.display_queue:
                R.display_queue.pop(0)

        # Lejátszás
        ok = play_song(sc, url)
        try:
            R.song_queue.task_done()
        except ValueError:
            pass

        # Ha a kapcsolat megszakadt
        if not ok and R.streaming:
            R.log('🔄 Újracsatlakozás...')
            sc.disconnect()
            time.sleep(3)
            if not sc.connect():
                R.log('❌ Újracsatlakozás sikertelen!')
                break

    sc.disconnect()
    R.streaming  = False
    R.connected  = False
    R.current    = {'title': 'Nincs zene lejátszás alatt', 'url': ''}
    R.log('⏹ Stream worker leállt.')


# =============================================
#  WEB INTERFACE - ROUTES
# =============================================
@app.route('/')
def index():
    msg      = request.args.get('msg', '')
    msg_type = request.args.get('t', 'success')
    with R.lock:
        dq = list(R.display_queue)
    return render_template_string(
        HTML_TEMPLATE,
        station   = STATION_NAME,
        connected = R.connected,
        streaming = R.streaming,
        current   = R.current,
        queue     = dq,
        logs      = R.logs[-40:],
        msg       = msg,
        msg_type  = msg_type
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
    if R.streaming:
        return redirect('/?msg=Már+fut+a+stream!&t=error')
    R.streaming     = True
    R.worker_thread = threading.Thread(target=stream_worker, daemon=True)
    R.worker_thread.start()
    R.log('▶ Stream indítása...')
    time.sleep(2)
    return redirect('/?msg=Stream+elindítva!&t=success')


@app.route('/stop', methods=['POST'])
def stop_stream():
    R.streaming = False
    R.skip_event.set()
    if R.ffmpeg_proc:
        _kill(R.ffmpeg_proc)
    if R.yt_proc:
        _kill(R.yt_proc)
    R.log('⏹ Stream leállítása...')
    return redirect('/?msg=Stream+leállítva!&t=success')


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
    return redirect('/?msg=Kihagyva,+következő...&t=success')


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
    return redirect('/?msg=Várólista+törölve!&t=success')


@app.route('/api/status')
def api_status():
    with R.lock:
        dq = list(R.display_queue)
    return jsonify({
        'connected':   R.connected,
        'streaming':   R.streaming,
        'current':     R.current,
        'queue':       dq,
        'queue_count': len(dq),
        'logs':        R.logs[-40:]
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
<title>{{ station }} — DJ Panel</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{
  font-family:'Segoe UI',Tahoma,Geneva,Verdana,sans-serif;
  background:linear-gradient(135deg,#0f0c29,#302b63,#24243e);
  color:#e0e0e0;min-height:100vh;
}
.wrap{max-width:920px;margin:0 auto;padding:20px}

/* ---- Header ---- */
.hdr{text-align:center;padding:28px 0;margin-bottom:28px;
     border-bottom:2px solid rgba(255,255,255,.08)}
.hdr h1{font-size:2.4em;
  background:linear-gradient(45deg,#ff6b6b,#feca57,#48dbfb,#a29bfe);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;
  background-clip:text;margin-bottom:6px}
.hdr .sub{color:#777;font-size:1.05em}

/* ---- Flash message ---- */
.flash{padding:12px 20px;border-radius:10px;margin-bottom:18px;font-weight:500}
.flash.success{background:rgba(0,184,148,.15);border:1px solid #00b894;color:#55efc4}
.flash.error{background:rgba(214,48,49,.15);border:1px solid #d63031;color:#ff7675}

/* ---- Status bar ---- */
.status-bar{display:flex;justify-content:space-between;align-items:center;
  background:rgba(255,255,255,.04);border-radius:14px;padding:16px 24px;
  margin-bottom:22px;border:1px solid rgba(255,255,255,.08);flex-wrap:wrap;gap:12px}
.status-ind{display:flex;align-items:center;gap:10px;font-weight:500}
.dot{width:13px;height:13px;border-radius:50%;flex-shrink:0}
.dot.on{background:#00ff88;box-shadow:0 0 12px #00ff88;animation:pulse 2s infinite}
.dot.off{background:#ff4757;box-shadow:0 0 8px #ff4757}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.controls{display:flex;gap:8px;flex-wrap:wrap}

/* ---- Buttons ---- */
.btn{padding:10px 22px;border:none;border-radius:8px;cursor:pointer;
     font-size:13px;font-weight:700;transition:.25s;text-transform:uppercase;
     letter-spacing:.8px;color:#fff}
.btn:hover{transform:translateY(-2px);box-shadow:0 6px 18px rgba(0,0,0,.35)}
.btn-go{background:linear-gradient(135deg,#00b894,#00cec9)}
.btn-stop{background:linear-gradient(135deg,#e17055,#d63031)}
.btn-skip{background:linear-gradient(135deg,#fdcb6e,#e17055)}
.btn-clr{background:linear-gradient(135deg,#636e72,#2d3436)}

/* ---- Cards ---- */
.card{background:rgba(255,255,255,.04);border-radius:14px;padding:22px 26px;
      margin-bottom:22px;border:1px solid rgba(255,255,255,.07)}
.card h3{font-size:.82em;text-transform:uppercase;letter-spacing:2.5px;
         margin-bottom:12px;font-weight:700}
.card h3.cyan{color:#48dbfb}
.card h3.yellow{color:#feca57}
.card h3.red{color:#ff6b6b}
.card h3.green{color:#00ff88}

/* ---- Now playing ---- */
#current-title{font-size:1.35em;font-weight:600;color:#fff;word-break:break-word}

/* ---- Input ---- */
.input-row{display:flex;gap:10px}
.input-row input[type=text]{flex:1;padding:13px 18px;border-radius:10px;
  border:1px solid rgba(255,255,255,.15);background:rgba(255,255,255,.06);
  color:#fff;font-size:15px;outline:none;transition:.3s}
.input-row input[type=text]:focus{border-color:#a29bfe}
.input-row input::placeholder{color:#555}
.btn-add{background:linear-gradient(135deg,#6c5ce7,#a29bfe);color:#fff;
  padding:13px 28px;border:none;border-radius:10px;cursor:pointer;
  font-size:15px;font-weight:700;transition:.25s;white-space:nowrap}
.btn-add:hover{transform:translateY(-2px);box-shadow:0 6px 18px rgba(108,92,231,.45)}

/* ---- Queue ---- */
.q-item{display:flex;align-items:center;padding:10px 14px;
  background:rgba(255,255,255,.025);border-radius:8px;margin-bottom:7px;
  border-left:3px solid #6c5ce7}
.q-item .num{color:#a29bfe;font-weight:700;margin-right:14px;min-width:24px;
             text-align:right}
.q-item .stitle{flex:1;font-size:.93em;word-break:break-word}
.q-empty{color:#555;text-align:center;padding:18px;font-style:italic}

/* ---- Logs ---- */
.log-box{background:rgba(0,0,0,.3);border-radius:12px;padding:18px;
         margin-bottom:22px;border:1px solid rgba(255,255,255,.04)}
.log-box h3{color:#55efc4;font-size:.82em;text-transform:uppercase;
            letter-spacing:2.5px;margin-bottom:10px}
#log-content{max-height:220px;overflow-y:auto;font-family:'Courier New',monospace;
  font-size:.82em;color:#888;line-height:1.7}
#log-content::-webkit-scrollbar{width:5px}
#log-content::-webkit-scrollbar-track{background:rgba(255,255,255,.03)}
#log-content::-webkit-scrollbar-thumb{background:#444;border-radius:3px}

/* ---- Footer links ---- */
.links{text-align:center;padding:18px;background:rgba(255,255,255,.025);
       border-radius:12px;border:1px solid rgba(255,255,255,.06)}
.links h3{color:#48dbfb;font-size:.82em;text-transform:uppercase;
          letter-spacing:2.5px;margin-bottom:10px}
.links a{color:#feca57;text-decoration:none;margin:0 12px;font-weight:500}
.links a:hover{text-decoration:underline}
</style>
</head>
<body>
<div class="wrap">

  <!-- HEADER -->
  <div class="hdr">
    <h1>🎵 {{ station }}</h1>
    <div class="sub">DJ Control Panel</div>
  </div>

  <!-- FLASH MESSAGE -->
  {% if msg %}
  <div class="flash {{ msg_type }}">{{ msg }}</div>
  {% endif %}

  <!-- STATUS BAR -->
  <div class="status-bar">
    <div class="status-ind">
      <div class="dot {{ 'on' if connected else 'off' }}" id="status-dot"></div>
      <span id="status-text">{{ 'ONLINE — Streaming' if connected else 'OFFLINE' }}</span>
    </div>
    <div class="controls">
      {% if not streaming %}
      <form action="/start" method="post" style="display:inline">
        <button class="btn btn-go" type="submit">▶ Indítás</button>
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
        <button class="btn btn-clr" type="submit">🗑 Törlés</button>
      </form>
    </div>
  </div>

  <!-- NOW PLAYING -->
  <div class="card">
    <h3 class="cyan">🎧 Most Szól</h3>
    <div id="current-title">{{ current.title }}</div>
  </div>

  <!-- ADD SONG -->
  <div class="card">
    <h3 class="yellow">➕ Zene Hozzáadása</h3>
    <form action="/add" method="post">
      <div class="input-row">
        <input type="text" name="url"
               placeholder="YouTube link beillesztése ide..." required>
        <button type="submit" class="btn-add">Hozzáadás</button>
      </div>
    </form>
  </div>

  <!-- QUEUE -->
  <div class="card">
    <h3 class="red">📋 Várólista (<span id="queue-count">{{ queue|length }}</span>)</h3>
    <div id="queue-list">
      {% if queue %}
        {% for item in queue %}
        <div class="q-item">
          <span class="num">{{ loop.index }}.</span>
          <span class="stitle">{{ item.title }}</span>
        </div>
        {% endfor %}
      {% else %}
        <div class="q-empty">A várólista üres — adj hozzá YouTube linkeket!</div>
      {% endif %}
    </div>
  </div>

  <!-- LOGS -->
  <div class="log-box">
    <h3>📊 Napló</h3>
    <div id="log-content">
      {% for l in logs %}<div>{{ l }}</div>{% endfor %}
    </div>
  </div>

  <!-- LISTEN LINKS -->
  <div class="links">
    <h3>🔗 Hallgatási Linkek</h3>
    <a href="http://szaby.radio12345.com" target="_blank">szaby.radio12345.com</a>
    <a href="http://szaby.radiostream321.com" target="_blank">szaby.radiostream321.com</a>
    <a href="http://szaby.radiostream123.com" target="_blank">szaby.radiostream123.com</a>
  </div>

</div>

<script>
// ---- Auto-refresh via AJAX every 3 seconds ----
function esc(s){
  var d=document.createElement('div');d.textContent=s;return d.innerHTML;
}
setInterval(function(){
  fetch('/api/status')
    .then(function(r){return r.json()})
    .then(function(d){
      // Status dot
      var dot=document.getElementById('status-dot');
      dot.className='dot '+(d.connected?'on':'off');
      document.getElementById('status-text').textContent=
        d.connected?'ONLINE — Streaming':'OFFLINE';

      // Current song
      document.getElementById('current-title').textContent=d.current.title;

      // Queue
      document.getElementById('queue-count').textContent=d.queue_count;
      var html='';
      if(d.queue.length===0){
        html='<div class="q-empty">A várólista üres — adj hozzá YouTube linkeket!</div>';
      }else{
        d.queue.forEach(function(item,i){
          html+='<div class="q-item"><span class="num">'+(i+1)+
                '.</span><span class="stitle">'+esc(item.title)+'</span></div>';
        });
      }
      document.getElementById('queue-list').innerHTML=html;

      // Logs
      var logHtml='';
      d.logs.forEach(function(l){logHtml+='<div>'+esc(l)+'</div>';});
      var el=document.getElementById('log-content');
      el.innerHTML=logHtml;
      el.scrollTop=el.scrollHeight;
    })
    .catch(function(){});
},3000);

// Scroll log to bottom on load
(function(){
  var el=document.getElementById('log-content');
  if(el) el.scrollTop=el.scrollHeight;
})();
</script>
</body>
</html>
"""


# =============================================
#  STARTUP
# =============================================
def check_deps():
    """Ellenőrzi, hogy a szükséges programok telepítve vannak-e"""
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
        print(f'\n❌ Hiányzó program(ok): {", ".join(missing)}\n')
        print('Telepítési útmutató:')
        if 'yt-dlp' in missing:
            print('  pip install yt-dlp')
        if 'ffmpeg' in missing:
            print('  Windows : https://ffmpeg.org/download.html  (tedd a PATH-ba!)')
            print('  Linux   : sudo apt install ffmpeg')
            print('  macOS   : brew install ffmpeg')
        print()
        sys.exit(1)


if __name__ == '__main__':
    check_deps()

    print(f"""
╔═══════════════════════════════════════════════════╗
║                                                   ║
║   🎵  {STATION_NAME} — DJ Control Panel            ║
║                                                   ║
║   Web Panel:  http://localhost:{WEB_PORT}                ║
║                                                   ║
║   SHOUTcast:  {SHOUTCAST_HOST}:{SHOUTCAST_PORT}       ║
║   Jelszó:     {SHOUTCAST_PASSWORD}                             ║
║                                                   ║
║   Hallgatás:                                      ║
║     http://szaby.radio12345.com                   ║
║     http://szaby.radiostream321.com               ║
║                                                   ║
╚═══════════════════════════════════════════════════╝
    """)

    app.run(host='0.0.0.0', port=WEB_PORT, debug=False, threaded=True)
