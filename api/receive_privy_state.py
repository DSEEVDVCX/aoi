"""Local one-shot receiver: capture Privy creds straight from YOUR normal browser.

No automation at all, so Google never blocks you. Flow:

    python receive_privy_state.py

1. This starts a tiny local server on http://127.0.0.1:8799 and prints a one-line
   snippet.
2. You open https://fomo.family in your OWN normal Chrome and log in normally.
3. You press F12 -> Console, paste the one line, press Enter.
4. The browser POSTs the creds to this local server; it saves them, proves a
   browserless refresh works, reads live data, and exits.

Nothing leaves your machine (localhost only). Prints only handles/counts.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ.setdefault("FOMO_API_UPSTREAM_IMPERSONATE", "true")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

PORT = 8799
_received: dict = {}

# The one line you paste in the fomo.family Console. It reads the Privy secrets
# from localStorage and POSTs them to this local server.
SNIPPET = (
    "fetch('http://127.0.0.1:8799/creds',{method:'POST',headers:{'Content-Type':'application/json'},"
    "body:JSON.stringify((()=>{const g=k=>{const v=localStorage.getItem(k);return v?v.replace(/^\"|\"$/g,''):null};"
    "let a=null,c=null;for(let i=0;i<localStorage.length;i++){const k=localStorage.key(i);"
    "const m=k&&k.match(/^privy:([a-z0-9]{20,}):recent-login-method$/i);if(m)a=m[1];"
    "const v=localStorage.getItem(k)||'';const cm=v.match(/client-[A-Za-z0-9]{20,}/);if(cm&&!c)c=cm[0];}"
    "return{access_token:g('privy:token'),refresh_token:g('privy:refresh_token'),pat:g('privy:pat'),"
    "app_id:a,client_id:c,ca_id:g('privy:caid')};})())}).then(r=>r.text()).then(t=>console.log('%c'+t,"
    "'color:#0a0;font-weight:bold')).catch(e=>console.error('فشل الإرسال للخادم المحلي:',e));"
)


class _Handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self):
        if self.path != "/creds":
            self.send_response(404)
            self._cors()
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            body = {}
        _received.update(body if isinstance(body, dict) else {})
        ok = bool(_received.get("refresh_token"))
        msg = ("[OK] استُلمت الأسرار — عُد إلى الطرفية، يكمل الإعداد تلقائياً."
               if ok else "لم أجد refresh_token — تأكّد أنك سجّلت الدخول في fomo.family.")
        payload = msg.encode("utf-8")
        self.send_response(200 if ok else 400)
        self._cors()
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_):  # silence default logging
        pass


def _wait_for_creds() -> dict:
    server = HTTPServer(("127.0.0.1", PORT), _Handler)
    print("=" * 72, flush=True)
    print(f">>> خادم محلي يعمل على http://127.0.0.1:{PORT} (لا يخرج شيء من جهازك).", flush=True)
    print(">>> الخطوات:", flush=True)
    print("    1) افتح https://fomo.family في متصفحك العادي وسجّل الدخول.", flush=True)
    print("    2) اضغط F12 ثم تبويب Console.", flush=True)
    print("    3) الصق السطر التالي كاملاً واضغط Enter:", flush=True)
    print("-" * 72, flush=True)
    print(SNIPPET, flush=True)
    print("-" * 72, flush=True)
    print(">>> بانتظار استلام الأسرار من متصفحك...", flush=True)
    while not _received.get("refresh_token"):
        server.handle_request()
    server.server_close()
    return dict(_received)


async def main() -> None:
    creds_raw = await asyncio.get_event_loop().run_in_executor(None, _wait_for_creds)

    from fomo_api.auth.credential_store import CredentialStore, StoredCredentials
    from fomo_api.config import settings

    def _c(v):
        return v.strip().strip('"') if isinstance(v, str) and v.strip() else None

    creds = StoredCredentials(
        access_token=_c(creds_raw.get("access_token")),
        refresh_token=_c(creds_raw.get("refresh_token")),
        pat=_c(creds_raw.get("pat")),
        app_id=_c(creds_raw.get("app_id")),
        client_id=_c(creds_raw.get("client_id")),
        ca_id=_c(creds_raw.get("ca_id")),
    )
    store = CredentialStore(settings.credential_state_file)
    store.save(creds)
    print(f"\n>>> حُفظت بيانات الاعتماد في {store.path} | قابلة للتجديد: {creds.is_refreshable()}", flush=True)

    from fomo_api.auth.token_refresher import _call_privy_refresh

    try:
        r = await _call_privy_refresh(
            refresh_token=creds.refresh_token, app_id=creds.app_id, pat=creds.pat,
            client_id=creds.client_id, ca_id=creds.ca_id,
            current_access=creds.access_token,
        )
        store.save(StoredCredentials(access_token=r.get("access"), refresh_token=r.get("refresh"), pat=r.get("pat")))
        access = r.get("access")
        print(">>> [OK] تجديد بلا متصفح نجح — النظام سيجدّد نفسه تلقائياً من الآن.", flush=True)
    except Exception as exc:
        print(f">>> تحذير: التجديد الفوري فشل ({exc})؛ سيُستخدم الرمز الملتقط.", flush=True)
        access = creds.access_token

    from fomo_api.clients.fomo_client import FomoClient

    client = FomoClient(access)
    try:
        lb = await client.get_leaderboard(page=1, page_size=5, period="all")
        traders = lb["traders"] if isinstance(lb, dict) else lb.traders
        total = lb["total_items"] if isinstance(lb, dict) else lb.total_items
        print(f">>> بيانات حيّة: {total} متداولاً في المتصدّرين", flush=True)
        for t in (traders[:3] if isinstance(traders, list) else traders):
            print(f"      @{t['handle'] if isinstance(t, dict) else t.handle}", flush=True)
        print(">>> اكتمل الإعداد. شغّل الخادم وسيعمل الاستخراج تلقائياً بلا تدخّل.", flush=True)
    except Exception as exc:
        print(f">>> فشل القراءة الحيّة ({type(exc).__name__}): الرمز غير صالح أو منتهٍ. أعد المحاولة.", flush=True)
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
