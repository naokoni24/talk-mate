#!/usr/bin/env python3
"""talk-mate: stdlib-only server for a WebRTC-direct Realtime voice consultation app."""
import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "talk_mate.db"
OPENAI_KEY = os.getenv("OPENAI_API_KEY", "")
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
MAX_SECONDS = max(60, int(os.getenv("MAX_SESSION_SECONDS", "600")))
DAILY_BUDGET = max(0, float(os.getenv("DAILY_BUDGET_USD", "1.0")))
USD_JPY_RATE = max(0, float(os.getenv("USD_JPY_RATE", "160.0")))
USER_TRANSCRIPT = os.getenv("USER_TRANSCRIPT") == "1"
BASIC_USER, BASIC_PASS = os.getenv("BASIC_USER", ""), os.getenv("BASIC_PASS", "")
COOKIE_SECRET = os.getenv("COOKIE_SECRET", secrets.token_urlsafe(32)).encode()
AUTH_ENABLED = bool(BASIC_USER and BASIC_PASS)

COMMON = """必ず日本語で話してください。1回の発話は簡潔に、目安20〜30秒以内にしてください。長い説明はユーザーが求めた場合のみ行ってください。深刻な自傷・危機の話題が出たら、今すぐ身近な人や緊急窓口につながるよう促し、いのちの電話（0570-783-556）などの相談窓口を案内してください。"""
PERSONAS = {
    "fortune": {"name":"占い・雑談", "icon":"🔮", "description":"気軽な占いと、楽しい雑談。エンタメとしてお楽しみください。", "voice":"marin", "opening":"こんにちは。今日はどんなことを占ったり、お話ししたりしましょうか？", "instructions": COMMON + "占い・雑談の相手です。占いはエンタメとして楽しく表現し、人生を決めつけず、気軽な会話を大切にしてください。画面の『エンタメです』という注意書きに沿ってください。"},
    "health": {"name":"健康の一般相談", "icon":"🏥", "description":"一般的な健康情報を、やさしく整理します。", "voice":"coral", "opening":"こんにちは。気になることを教えてください。一般的な情報として一緒に整理します。", "instructions": COMMON + "一般的な健康情報を伝える相談相手です。診断、治療の決定、薬の服用指示はしません。受診を勧める場合は穏やかに理由を説明してください。胸痛、呼吸困難、意識障害、片側の麻痺など緊急性が高い症状には、ただちに119または救急受診を案内してください。"},
    "listener": {"name":"愚痴聞き", "icon":"🫂", "description":"まずは気持ちを受け止め、ゆっくり整理します。", "voice":"cedar", "opening":"こんにちは。ここでは気兼ねなく話してください。何があったのか、聞かせてもらえますか？", "instructions": COMMON + "傾聴に特化した相手です。気持ちを否定せず共感し、要点を短く言い換えて整理します。ユーザーが明確に求めるまで助言や解決策を出さず、質問も負担にならないよう一度に一つにしてください。"},
    "love": {"name":"恋愛相談", "icon":"💘", "description":"気持ちに寄り添いながら、一緒に考えます。", "voice":"verse", "opening":"こんにちは。恋愛のことで、今どんな気持ちですか？ よければ聞かせてください。", "instructions": COMMON + "恋愛相談の相手です。共感を基盤に、選択肢や気持ちを一緒に整理してください。断定せず、相手の悪口や攻撃への同調はせず、ユーザー自身が納得して選べるよう支えてください。"},
}

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS sessions (
          id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, persona_id TEXT NOT NULL,
          duration_seconds INTEGER NOT NULL, input_tokens INTEGER NOT NULL DEFAULT 0,
          output_tokens INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0,
          transcript TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '')""")

def today_cost():
    today = datetime.now().astimezone().date().isoformat()
    with db() as con:
        return float(con.execute("SELECT COALESCE(SUM(cost_usd),0) FROM sessions WHERE substr(created_at,1,10)=?", (today,)).fetchone()[0])

def estimate_cost(inp, out):
    return round(max(0, int(inp)) * 10 / 1_000_000 + max(0, int(out)) * 20 / 1_000_000, 8)

def json_body(handler):
    try:
        n = int(handler.headers.get("Content-Length", "0"))
        return json.loads(handler.rfile.read(n) or b"{}")
    except (ValueError, json.JSONDecodeError):
        return None

def openai_json(url, payload, key, timeout=20):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST", headers={"Authorization":"Bearer " + key, "Content-Type":"application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r: return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"OpenAI API error ({e.code}): {detail}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"OpenAI APIへ接続できません: {e.reason}")

def sign(value): return hmac.new(COOKIE_SECRET, value.encode(), hashlib.sha256).hexdigest()
def logged_in(headers):
    if not AUTH_ENABLED: return True
    try:
        c = SimpleCookie(headers.get("Cookie")); raw = c.get("talk_mate_auth").value
        user, expiry, sig = raw.split(".")
        return user == BASIC_USER and int(expiry) > time.time() and hmac.compare_digest(sig, sign(user + "." + expiry))
    except Exception: return False

class App(BaseHTTPRequestHandler):
    server_version = "talk-mate/1.0"
    def log_message(self, fmt, *args): print("[%s] %s" % (self.log_date_time_string(), fmt % args))
    def send_json(self, value, status=200):
        data = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(data)
    def send_html(self, content):
        data = content.encode(); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(data)
    def auth(self):
        if logged_in(self.headers): return True
        self.send_response(302); self.send_header("Location", "/login"); self.end_headers(); return False
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/login": return self.login_page()
        if path == "/logout":
            self.send_response(302); self.send_header("Set-Cookie", "talk_mate_auth=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax"); self.send_header("Location", "/login"); self.end_headers(); return
        if not self.auth(): return
        if path == "/": return self.send_html(PAGE)
        if path == "/api/status": return self.send_json({"openaiConfigured":bool(OPENAI_KEY),"geminiConfigured":bool(GEMINI_KEY),"todayCost":today_cost(),"dailyBudget":DAILY_BUDGET,"usdJpyRate":USD_JPY_RATE,"maxSessionSeconds":MAX_SECONDS,"userTranscript":USER_TRANSCRIPT})
        if path == "/api/personas": return self.send_json({"personas":[{k:v for k,v in p.items() if k not in ("instructions", "opening")} | {"id":i} for i,p in PERSONAS.items()]})
        if path == "/api/sessions":
            with db() as con: rows=[dict(r) for r in con.execute("SELECT id,created_at,persona_id,duration_seconds,input_tokens,output_tokens,cost_usd,summary FROM sessions ORDER BY id DESC LIMIT 50")]
            for r in rows: r["persona"] = PERSONAS.get(r["persona_id"],{}).get("name", r["persona_id"])
            return self.send_json({"sessions":rows})
        return self.send_json({"error":"Not found"}, 404)
    def do_POST(self):
        path=urlparse(self.path).path
        if path == "/login": return self.login_post()
        if not self.auth(): return
        body=json_body(self)
        if body is None: return self.send_json({"error":"JSON形式が不正です"},400)
        if path == "/api/realtime/secret": return self.realtime_secret(body)
        if path == "/api/sessions": return self.save_session(body)
        if path == "/api/summarize": return self.summarize(body)
        return self.send_json({"error":"Not found"},404)
    def login_page(self):
        if not AUTH_ENABLED: self.send_response(302); self.send_header("Location","/"); self.end_headers(); return
        self.send_html("""<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'><style>body{font-family:system-ui;background:#f7f5ff;display:grid;place-items:center;min-height:90vh}form{background:white;padding:2rem;border-radius:18px;box-shadow:0 8px 30px #ddd}input,button{display:block;width:100%;box-sizing:border-box;padding:.8rem;margin:.6rem 0}button{background:#6855d9;color:white;border:0;border-radius:9px}</style><form method=post><h1>talk-mate</h1><p>ログインしてください</p><input name=user placeholder=ユーザー名 required><input name=password type=password placeholder=パスワード required><button>ログイン</button></form>""")
    def login_post(self):
        n=int(self.headers.get("Content-Length","0")); form=parse_qs(self.rfile.read(n).decode())
        if not AUTH_ENABLED or not (hmac.compare_digest(form.get("user",[""])[0],BASIC_USER) and hmac.compare_digest(form.get("password",[""])[0],BASIC_PASS)):
            return self.send_html("<p>ログインに失敗しました。<a href='/login'>戻る</a></p>")
        expiry=str(int(time.time()+7*86400)); raw=BASIC_USER+"."+expiry+"."+sign(BASIC_USER+"."+expiry)
        self.send_response(302); self.send_header("Set-Cookie",f"talk_mate_auth={raw}; Max-Age=604800; Path=/; HttpOnly; SameSite=Lax"); self.send_header("Location","/"); self.end_headers()
    def realtime_secret(self, body):
        persona=PERSONAS.get(body.get("persona_id"))
        if not persona: return self.send_json({"error":"ペルソナが不正です"},400)
        if not OPENAI_KEY: return self.send_json({"error":"OPENAI_API_KEY が未設定です"},503)
        if today_cost() >= DAILY_BUDGET: return self.send_json({"error":"本日の利用上限に達しました"},429)
        payload={"session":{"type":"realtime","model":"gpt-realtime-mini","audio":{"output":{"voice":persona["voice"]},"input":{"transcription":{"model":"gpt-4o-mini-transcribe"} if USER_TRANSCRIPT else None}},"instructions":persona["instructions"]}}
        if not USER_TRANSCRIPT: del payload["session"]["audio"]["input"]
        try:
            data=openai_json("https://api.openai.com/v1/realtime/client_secrets",payload,OPENAI_KEY)
            return self.send_json({"client_secret":data.get("value") or data.get("client_secret",{}).get("value"),"opening":persona["opening"],"expires_at":data.get("expires_at")})
        except RuntimeError as e: return self.send_json({"error":str(e)},502)
    def save_session(self, b):
        pid=b.get("persona_id")
        if pid not in PERSONAS: return self.send_json({"error":"ペルソナが不正です"},400)
        inp=max(0,min(int(b.get("input_tokens",0) or 0),10_000_000)); out=max(0,min(int(b.get("output_tokens",0) or 0),10_000_000)); dur=max(0,min(int(b.get("duration_seconds",0) or 0),MAX_SECONDS+60))
        transcript=str(b.get("transcript", ""))[:30000]; cost=estimate_cost(inp,out); now=datetime.now().astimezone().isoformat(timespec="seconds")
        with db() as con: cur=con.execute("INSERT INTO sessions(created_at,persona_id,duration_seconds,input_tokens,output_tokens,cost_usd,transcript) VALUES(?,?,?,?,?,?,?)",(now,pid,dur,inp,out,cost,transcript))
        return self.send_json({"id":cur.lastrowid,"cost_usd":cost})
    def summarize(self,b):
        if not GEMINI_KEY: return self.send_json({"error":"GEMINI_API_KEY が未設定です"},503)
        sid=int(b.get("session_id",0));
        with db() as con: row=con.execute("SELECT transcript,persona_id FROM sessions WHERE id=?",(sid,)).fetchone()
        if not row: return self.send_json({"error":"相談記録が見つかりません"},404)
        prompt="次の日本語の相談内容を、個人情報を補わず、120字以内でやさしく要約してください。助言・診断はしないでください。\n\n"+row["transcript"]
        try:
            url="https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key="+GEMINI_KEY
            req=urllib.request.Request(url,data=json.dumps({"contents":[{"parts":[{"text":prompt}]}]}).encode(),headers={"Content-Type":"application/json"})
            with urllib.request.urlopen(req,timeout=20) as r: data=json.load(r)
            summary=data["candidates"][0]["content"]["parts"][0]["text"].strip()[:1000]
            with db() as con: con.execute("UPDATE sessions SET summary=? WHERE id=?",(summary,sid))
            return self.send_json({"summary":summary})
        except Exception as e: return self.send_json({"error":"要約生成に失敗しました"},502)

PAGE = r'''<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#f8f7ff"><title>talk-mate</title><style>
:root{--p:#6d5bd0;--p2:#8b7ae6;--ink:#262438;--muted:#737083;--bg:#f8f7ff;--card:#fff;--line:#ebe8f6;--danger:#c6495d;--shadow:0 12px 35px rgba(48,38,94,.08)}html{-webkit-text-size-adjust:100%;text-size-adjust:100%}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 85% -5%,#e8e1ff 0,transparent 32%),var(--bg);font-family:system-ui,-apple-system,"Hiragino Sans",sans-serif;color:var(--ink);min-height:100vh}main{max-width:680px;margin:auto;padding:calc(18px + env(safe-area-inset-top)) 18px calc(52px + env(safe-area-inset-bottom))}header{display:flex;justify-content:space-between;align-items:center;margin:4px 0 30px}.brand{display:flex;align-items:center;gap:10px}.brand-mark{width:38px;height:38px;display:grid;place-items:center;border-radius:13px;background:linear-gradient(135deg,var(--p),#a391f3);color:#fff;font-size:19px;box-shadow:0 8px 20px #7461d844}h1{font-size:23px;letter-spacing:-.02em;margin:0}h2{font-size:25px;letter-spacing:-.03em;margin:0 0 8px}.eyebrow{font-size:12px;color:var(--p);font-weight:800;letter-spacing:.08em;margin:0 0 8px}.lead{font-size:15px;color:var(--muted);line-height:1.75;margin:0 0 22px}button{font:inherit;cursor:pointer;border:0;border-radius:14px;padding:13px 16px;background:linear-gradient(135deg,var(--p),var(--p2));color:#fff;font-weight:750;transition:transform .18s,box-shadow .18s}button:active{transform:scale(.98)}.icon-button{padding:10px 14px;background:#fff;color:var(--p);border:1px solid var(--line);box-shadow:0 5px 18px #40377b0b}.card{background:rgba(255,255,255,.94);border:1px solid var(--line);border-radius:22px;padding:20px;margin:14px 0;box-shadow:var(--shadow)}.persona-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.persona{min-height:164px;width:100%;text-align:left;color:var(--ink);background:rgba(255,255,255,.95);border:1px solid var(--line);display:flex;flex-direction:column;gap:12px;align-items:flex-start;padding:18px;box-shadow:0 7px 25px rgba(48,38,94,.055)}.persona:hover{border-color:#b8afea;transform:translateY(-2px);box-shadow:var(--shadow)}.persona-top{display:flex;align-items:center;justify-content:space-between;width:100%}.emoji{font-size:31px;line-height:1}.arrow{color:#aaa3c9;font-size:20px}.persona b{font-size:16px}.persona small{display:block;color:var(--muted);line-height:1.55;margin-top:5px;font-weight:500}.notice{display:flex;align-items:flex-start;gap:10px;padding:13px 15px;margin-top:18px;border-radius:16px;background:#f0edff;color:#5d50a5;font-size:13px;line-height:1.55}.notice.bad{background:#fff0f1;color:#a2384a}.tag{display:inline-flex;font-size:12px;color:#72579a;background:#f1eaff;padding:5px 9px;border-radius:20px}.hidden{display:none!important}.status{display:flex;justify-content:center;align-items:center;gap:8px;padding:11px;border-radius:30px;background:#eeebff;color:#5545b6;font-size:14px;font-weight:700}.status:before{content:"";width:8px;height:8px;border-radius:50%;background:#7a67e0;box-shadow:0 0 0 5px #7a67e020}.call-head{text-align:center;margin:24px 0}.timer{font-size:48px;font-weight:750;letter-spacing:-.04em;font-variant-numeric:tabular-nums;text-align:center;margin:30px 0 0}.remaining{text-align:center;color:var(--muted);font-size:14px}.warning{background:#fff2d8;color:#895c00;padding:11px;border-radius:12px;text-align:center}.subtitle{min-height:140px;line-height:1.8;font-size:17px;white-space:pre-wrap;position:relative;padding-top:46px}.subtitle:before{content:"AIの発話";position:absolute;top:17px;left:20px;color:var(--p);font-size:11px;font-weight:800;letter-spacing:.08em}.actions{display:flex;gap:10px;margin-top:16px}.actions button{flex:1}.secondary{color:var(--p);background:#eeebff}.danger{background:var(--danger)}.result-cost{font-size:30px;font-weight:800;color:var(--p);margin:8px 0 2px}.yen{font-size:15px;color:var(--muted);font-weight:650}.result-detail{color:var(--muted);font-size:14px;line-height:1.7}.history-row{display:flex;gap:13px}.history-icon{width:42px;height:42px;flex:0 0 42px;display:grid;place-items:center;background:#f1eeff;border-radius:14px;font-size:21px}.history-main{min-width:0;flex:1}.history-top{display:flex;justify-content:space-between;gap:10px}.history-cost{text-align:right;font-weight:800;color:var(--p);white-space:nowrap}.history small{color:var(--muted);line-height:1.5}.empty{text-align:center;padding:38px 20px;color:var(--muted)}#toast{position:fixed;bottom:18px;left:16px;right:16px;max-width:648px;margin:auto;background:#302c42;color:white;padding:13px 16px;border-radius:14px;z-index:4;box-shadow:0 12px 30px #211d3f40}a{color:var(--p)}@media(max-width:430px){.persona-grid{grid-template-columns:1fr}.persona{min-height:0;display:grid;grid-template-columns:auto 1fr;align-items:center}.persona-top{width:auto}.persona .arrow{display:none}.persona-copy{padding-right:4px}.actions{position:sticky;bottom:10px}.timer{font-size:43px}}
</style><body><main><header><div class="brand"><span class="brand-mark">◌</span><h1>talk-mate</h1></div><button class="icon-button" onclick="showHistory()">履歴</button></header>
<section id="choose"><p class="eyebrow">VOICE CONSULTATION</p><h2>今日は、誰と話す？</h2><p class="lead">気持ちに合う相談相手を選んでください。声で話すだけで、やさしく受け止めます。</p><div id="personaList" class="persona-grid"></div><div id="config" class="notice"></div></section>
<section id="call" class="hidden"><button class="icon-button" onclick="endCall('user')">← 終了して戻る</button><div class="call-head"><p class="eyebrow">NOW TALKING</p><h2 id="callTitle"></h2><span id="entertainment" class="tag hidden">エンタメとしてお楽しみください</span></div><div id="connection" class="status">接続準備中…</div><div class="timer" id="timer">00:00</div><p class="remaining" id="remaining"></p><p id="warning" class="warning hidden"></p><div class="card subtitle" id="subtitle">接続すると、AIの字幕がここに表示されます。</div><div class="actions"><button id="mute" class="secondary" onclick="toggleMute()">マイクをミュート</button><button class="danger" onclick="endCall('user')">通話を終了</button></div></section>
<section id="result" class="hidden"><p class="eyebrow">SESSION COMPLETE</p><h2>おつかれさまでした</h2><p class="lead">話した内容は、いつでも履歴から振り返れます。</p><div class="card" id="resultText"></div><div id="summaryBox" class="card hidden"></div><button onclick="backHome()">別の相手と話す</button></section>
<section id="history" class="hidden"><button class="icon-button" onclick="backHome()">← 戻る</button><p class="eyebrow" style="margin-top:28px">YOUR SESSIONS</p><h2>相談履歴</h2><p class="lead">これまでの相談と利用料金を確認できます。</p><div id="historyList"></div></section><div id="toast" class="hidden"></div></main><script>
let status={}, personas=[], pc=null, stream=null, selected=null, started=0, timerId=null, idleId=null, muted=false, usage={input:0,output:0}, transcript='', savedId=null, ended=false;
const $=id=>document.getElementById(id), money=n=>'$'+Number(n||0).toFixed(4), yen=n=>'約'+Math.round(Number(n||0)*(status.usdJpyRate||160)).toLocaleString()+'円', esc=s=>String(s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
async function api(path,opt={}){let r=await fetch(path,{headers:{'Content-Type':'application/json'},...opt});let j=await r.json().catch(()=>({}));if(!r.ok)throw Error(j.error||'通信に失敗しました');return j}
function toast(s){$('toast').textContent=s;$('toast').classList.remove('hidden');setTimeout(()=>$('toast').classList.add('hidden'),4200)}
function page(n){['choose','call','result','history'].forEach(x=>$(x).classList.toggle('hidden',x!==n))}
async function boot(){try{[status,{personas}]=await Promise.all([api('/api/status'),api('/api/personas')]);let l=$('personaList');l.innerHTML=personas.map(p=>`<button class="persona" onclick="startCall('${p.id}')"><span class="persona-top"><span class=emoji>${p.icon}</span><span class=arrow>→</span></span><span class="persona-copy"><b>${p.name}</b><small>${p.description}</small></span></button>`).join('');$('config').classList.toggle('bad',!status.openaiConfigured);$('config').innerHTML=status.openaiConfigured?`<span>✓</span><span>通話できます。上限 ${Math.floor(status.maxSessionSeconds/60)}分・本日の利用 ${money(status.todayCost)}（${yen(status.todayCost)}）</span>`:'<span>!</span><span>OPENAI_API_KEY が未設定のため、通話は開始できません。</span>';}catch(e){toast(e.message)}}
async function startCall(id){if(!status.openaiConfigured)return toast('OPENAI_API_KEY が未設定です');selected=personas.find(p=>p.id===id);usage={input:0,output:0};transcript='';savedId=null;ended=false;page('call');$('callTitle').textContent=selected.icon+' '+selected.name;$('entertainment').classList.toggle('hidden',id!=='fortune');$('subtitle').textContent='短命キーを発行しています…';$('connection').textContent='接続準備中…';try{let secret=await api('/api/realtime/secret',{method:'POST',body:JSON.stringify({persona_id:id})});if(!secret.client_secret)throw Error('短命キーを取得できませんでした');stream=await navigator.mediaDevices.getUserMedia({audio:true});pc=new RTCPeerConnection();stream.getTracks().forEach(t=>pc.addTrack(t,stream));pc.ontrack=e=>{let a=new Audio();a.srcObject=e.streams[0];a.autoplay=true};pc.onconnectionstatechange=()=>{$('connection').textContent=pc.connectionState==='connected'?'接続中':'接続状態: '+pc.connectionState;if(['failed','disconnected'].includes(pc.connectionState)&&!ended)endCall('connection')};let dc=pc.createDataChannel('oai-events');dc.onmessage=e=>event(JSON.parse(e.data));dc.onopen=()=>{dc.send(JSON.stringify({type:'response.create',response:{modalities:['audio','text'],instructions:'最初に次の一言だけを自然に話してください: '+secret.opening}}));$('connection').textContent='接続中'};let offer=await pc.createOffer();await pc.setLocalDescription(offer);let r=await fetch('https://api.openai.com/v1/realtime/calls',{method:'POST',headers:{'Authorization':'Bearer '+secret.client_secret,'Content-Type':'application/sdp'},body:offer.sdp});if(!r.ok)throw Error('Realtime接続に失敗しました');await pc.setRemoteDescription({type:'answer',sdp:await r.text()});started=Date.now();tick();timerId=setInterval(tick,1000);resetIdle();}catch(e){toast(e.message);endCall('error')}}
function event(e){if(e.type==='response.output_audio_transcript.delta'){transcript+=e.delta||'';$('subtitle').textContent=transcript.slice(-1600)}if(e.type==='response.done'){let u=e.response&&e.response.usage||{};usage.input+=Number(u.input_tokens||u.input_token_details&&u.input_token_details.audio_tokens||0);usage.output+=Number(u.output_tokens||u.output_token_details&&u.output_token_details.audio_tokens||0);resetIdle()}if(e.type==='input_audio_buffer.speech_started')resetIdle()}
function resetIdle(){clearTimeout(idleId);idleId=setTimeout(()=>endCall('silent'),90000)}
function tick(){let sec=Math.min(status.maxSessionSeconds,Math.floor((Date.now()-started)/1000)), left=status.maxSessionSeconds-sec;$('timer').textContent=String(Math.floor(sec/60)).padStart(2,'0')+':'+String(sec%60).padStart(2,'0');$('remaining').textContent='残り '+Math.ceil(left/60)+' 分';if(left<=60){$('warning').textContent='残り1分です。まもなく自動で終了します。';$('warning').classList.remove('hidden')}if(left<=0)endCall('limit')}
function toggleMute(){if(!stream)return;muted=!muted;stream.getAudioTracks().forEach(t=>t.enabled=!muted);$('mute').textContent=muted?'ミュートを解除':'マイクをミュート'}
async function endCall(reason){if(ended)return;ended=true;clearInterval(timerId);clearTimeout(idleId);if(pc){pc.close();pc=null}if(stream){stream.getTracks().forEach(t=>t.stop());stream=null}let sec=started?Math.floor((Date.now()-started)/1000):0;started=0;if(!selected){return page('choose')}let cost=usage.input*10/1e6+usage.output*20/1e6;try{let r=await api('/api/sessions',{method:'POST',body:JSON.stringify({persona_id:selected.id,duration_seconds:sec,input_tokens:usage.input,output_tokens:usage.output,transcript})});savedId=r.id;cost=r.cost_usd}catch(e){toast('利用記録を保存できませんでした: '+e.message)}$('resultText').innerHTML=`<b>${reason==='limit'?'上限時間に達したため終了しました。':reason==='silent'?'90秒の無音のため終了しました。':'通話を終了しました。'}</b><div class="result-cost">${money(cost)} <span class="yen">（${yen(cost)}）</span></div><div class="result-detail">通話時間 ${Math.floor(sec/60)}分${sec%60}秒<br>入力 ${usage.input.toLocaleString()} token ／ 出力 ${usage.output.toLocaleString()} token<br>円換算レート $1 = ${Number(status.usdJpyRate||160).toFixed(1)}円</div>`;if(status.geminiConfigured&&savedId&&transcript){$('summaryBox').classList.remove('hidden');$('summaryBox').textContent='相談内容を要約しています…';try{let x=await api('/api/summarize',{method:'POST',body:JSON.stringify({session_id:savedId})});$('summaryBox').innerHTML='<b>相談の要約</b><p>'+esc(x.summary)+'</p>'}catch(e){$('summaryBox').textContent='要約を生成できませんでした'}}else $('summaryBox').classList.add('hidden');page('result')}
function backHome(){if(pc)endCall('user');page('choose');boot()}
async function showHistory(){page('history');$('historyList').textContent='読み込み中…';try{let x=await api('/api/sessions');$('historyList').innerHTML=x.sessions.length?x.sessions.map(s=>{let p=personas.find(p=>p.id===s.persona_id)||{};return `<div class="card history"><div class="history-row"><span class="history-icon">${p.icon||'💬'}</span><div class="history-main"><div class="history-top"><div><b>${esc(s.persona)}</b><br><small>${new Date(s.created_at).toLocaleString('ja-JP')}<br>${Math.floor(s.duration_seconds/60)}分${s.duration_seconds%60}秒</small></div><div class="history-cost">${money(s.cost_usd)}<br><span class="yen">${yen(s.cost_usd)}</span></div></div>${s.summary?'<p>'+esc(s.summary)+'</p>':''}</div></div></div>`}).join(''):'<div class="card empty">まだ相談履歴はありません。</div>'}catch(e){$('historyList').textContent=e.message}}
boot();
</script></body></html>'''

if __name__ == "__main__":
    init_db()
    port = int(os.getenv("PORT", "8788"))
    print(f"talk-mate running at http://localhost:{port}")
    ThreadingHTTPServer(("", port), App).serve_forever()
