"""One-time authorisation of the owner's Google account for the daily Drive backup.

    venv/Scripts/python.exe scripts/autorizar_google_drive.py CAMINHO/client_secret.json [--gravar .env.producao]

The client_secret.json comes from Google Cloud Console: APIs e serviços > Credenciais >
Criar credenciais > ID do cliente OAuth > tipo "App para computador" (with the Google Drive API
enabled in the project). The browser opens, you allow access, and the script prints the three
GDRIVE_* lines for .env.producao (or appends them with --gravar). Scope drive.file: the app only
sees the files it creates in your Drive.
"""
import base64
import hashlib
import http.server
import json
import secrets
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/drive.file"


def main():
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    target = None
    if "--gravar" in args:
        i = args.index("--gravar")
        target = Path(args[i + 1])
        del args[i:i + 2]
    raw = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    client = raw.get("installed") or raw.get("web")
    if not client:
        sys.exit("Este arquivo não é um ID do cliente OAuth (precisa ser do tipo App para computador).")

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    got = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))
            if query.get("state") == state:
                got.update(query)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            text = "Pronto, pode fechar esta aba." if "code" in query else "Autorização não concluída."
            self.wfile.write(f"<p>{text}</p>".encode())

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{server.server_address[1]}"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code", "scope": SCOPE,
        "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    print("Abrindo o navegador para você autorizar. Se não abrir, copie este endereço:\n" + url + "\n")
    threading.Thread(target=server.handle_request, daemon=True).start()
    webbrowser.open(url)
    for _ in range(600):
        if got:
            break
        threading.Event().wait(0.5)
    server.server_close()
    if "code" not in got:
        sys.exit("Não recebi a autorização (" + got.get("error", "tempo esgotado") + ").")

    data = urllib.parse.urlencode({
        "code": got["code"], "client_id": client["client_id"], "client_secret": client.get("client_secret", ""),
        "redirect_uri": redirect, "grant_type": "authorization_code", "code_verifier": verifier,
    }).encode()
    with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=data)) as response:
        tokens = json.loads(response.read())
    if "refresh_token" not in tokens:
        sys.exit("O Google não devolveu o refresh token. Remova o acesso do app em myaccount.google.com/permissions e rode de novo.")
    lines = [f"GDRIVE_CLIENT_ID={client['client_id']}", f"GDRIVE_CLIENT_SECRET={client.get('client_secret', '')}",
             f"GDRIVE_REFRESH_TOKEN={tokens['refresh_token']}"]
    if target:
        with target.open("a", encoding="utf-8") as handle:
            handle.write("\n# Backup diário no Google Drive (scripts/autorizar_google_drive.py)\n" + "\n".join(lines) + "\n")
        print(f"Gravado em {target}. Não envie esse arquivo para ninguém.")
    else:
        print("Coloque estas linhas no .env.producao (são segredos, não compartilhe):\n")
        print("\n".join(lines))


if __name__ == "__main__":
    main()
