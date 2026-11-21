"""Build the .zip to upload to Discloud (standard library only).

    python scripts/montar_discloud.py

Never packs the local database, the virtualenv or the local .env. The server reads
.env.producao instead (created here on the first run, with a new SECRET_KEY; settings.py
loads it only where there is no .env), and the site address comes from ID= in discloud.config. The database is created on the
server by "migrate" (see start.sh); because it is never in the zip, uploading a new
version keeps it.
"""
import fnmatch
import secrets
import shutil
from datetime import datetime
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT.parent / "discloud-financas.zip"
PROD_ENV = ROOT / ".env.producao"
# A clean copy of exactly what is in the zip. Publishing from here (VS Code extension or CLI)
# cannot pick up the virtualenv, the database or the local .env, whatever the tool ignores.
STAGE = ROOT.parent / "discloud-publicar"
STAGE_MARK = ".pasta-gerada-pelo-montar_discloud"


def stage_folder():
    if STAGE.exists():
        if not (STAGE / STAGE_MARK).exists() and any(STAGE.iterdir()):
            sys.exit(f"A pasta {STAGE} existe e não foi criada por este script. Renomeie ou apague e rode de novo.")
        # Empty it but keep the folder itself: it may be open in VS Code (Windows locks open folders).
        for child in STAGE.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    else:
        STAGE.mkdir()
    with zipfile.ZipFile(OUTPUT) as zf:
        zf.extractall(STAGE)
    (STAGE / STAGE_MARK).write_text("Gerada por scripts/montar_discloud.py. Não edite: é refeita a cada pacote.\n",
                                    encoding="utf-8")

SKIP_DIRS = {"venv", ".venv", "__pycache__", ".git", ".github", ".cache", "docs", "staticfiles", "node_modules",
             ".screenshots-tmp", "htmlcov"}
SKIP_FILES = ["*.pyc", "*.sqlite3", "*.sqlite3-wal", "*.sqlite3-shm", "db.sqlite3*", ".env", ".env.example", "*.zip",
              ".coverage", "*.log", "client_secret*.json", "backup_drive.json", "backup_drive.lock"]

TEMPLATE = """# Configuração do site na Discloud (o servidor lê este arquivo; o .env do seu computador nunca sobe).
# Fica só no seu computador (não vai para o GitHub). Não compartilhe.
SECRET_KEY={key}
DEBUG=0

# Domínio próprio (opcional), sem https://. Ex.: financas.meusite.com.br (vários: separe por vírgula).
DOMINIO_PROPRIO=
# ALLOWED_HOSTS e CSRF_TRUSTED_ORIGINS são refeitos a cada pacote a partir do ID do
# discloud.config e do DOMINIO_PROPRIO. Não edite essas duas linhas à mão.

# Nomes dos arquivos de tela com uma assinatura do conteúdo: o navegador nunca mistura versões.
STATIC_MANIFEST=1

# A Discloud entrega o HTTPS na frente do programa.
SECURE_SSL_REDIRECT=0
TRUST_X_FORWARDED_PROTO=1

LANGUAGE_CODE=pt-br
TIME_ZONE=America/Sao_Paulo
DEFAULT_CURRENCY_CODE=BRL
DEFAULT_CURRENCY_SYMBOL=R$
DEFAULT_NUMBER_FORMAT=eu
ALLOW_NEGATIVE_BALANCE=1

# Deixe 1 só até criar a sua conta no site. Depois troque para 0, gere o pacote e envie de novo.
ALLOW_REGISTRATION=1

LOGIN_MAX_ATTEMPTS=5
LOGIN_LOCKOUT_SECONDS=900
REGISTER_MAX_PER_HOUR=10

# Leitura automática de PDF (opcional, paga à parte). Vazio = use a skill no seu chat.
ANTHROPIC_API_KEY=
"""


def read_config():
    config = {}
    for line in (ROOT / "discloud.config").read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            config[key.strip().upper()] = value.strip()
    return config


def active_keys(text):
    return {line.split("=", 1)[0].strip() for line in text.splitlines()
            if "=" in line and not line.lstrip().startswith("#")}


def prepare_server_env(site_id):
    """Create .env.producao if needed and make sure it has the site address. Returns its text."""
    if not PROD_ENV.exists():
        PROD_ENV.write_text(TEMPLATE.format(key=secrets.token_urlsafe(50)), encoding="utf-8")
        print(f"Criei {PROD_ENV.name} com uma chave secreta nova. Revise se quiser e rode de novo quando mudar algo.")
    lines = PROD_ENV.read_text(encoding="utf-8").splitlines()
    keys = active_keys("\n".join(lines))
    if "SECRET_KEY" not in keys or "DEBUG" not in keys:
        sys.exit(f"{PROD_ENV.name} precisa ter SECRET_KEY e DEBUG=0.")
    if "STATIC_MANIFEST" not in keys:
        lines += ["", "# Nomes dos arquivos de tela com uma assinatura do conteúdo: o navegador nunca mistura versões.",
                  "STATIC_MANIFEST=1"]
    if "DOMINIO_PROPRIO" not in keys:
        lines += ["", "# Domínio próprio (opcional), sem https://. Vários: separe por vírgula.", "DOMINIO_PROPRIO="]
    own = next((l.split("=", 1)[1] for l in lines if l.startswith("DOMINIO_PROPRIO=")), "")
    own = [h.strip().lower().removeprefix("https://").removeprefix("http://").strip("/") for h in own.split(",")]
    hosts = [f"{site_id}.discloud.app", f"{site_id}.discloud.dev"] + [h for h in own if h]
    # The address lines are rebuilt every time, so a new ID or domain is always picked up.
    lines = [l for l in lines if not l.startswith(("ALLOWED_HOSTS=", "CSRF_TRUSTED_ORIGINS="))]
    while lines and not lines[-1].strip():
        lines.pop()
    lines += ["ALLOWED_HOSTS=" + ",".join(hosts), "CSRF_TRUSTED_ORIGINS=" + ",".join(f"https://{h}" for h in hosts)]
    text = "\n".join(lines) + "\n"
    PROD_ENV.write_text(text, encoding="utf-8")
    return text


def skipped(path):
    rel = path.relative_to(ROOT)
    if set(rel.parts[:-1]) & SKIP_DIRS or rel.parts[0] in SKIP_DIRS:
        return True
    return any(fnmatch.fnmatch(path.name, pattern) for pattern in SKIP_FILES)


def main():
    config = read_config()
    site_id = config.get("ID", "")
    if not site_id or site_id == "meu-financas":
        print("Aviso: o ID em discloud.config ainda é o exemplo \"meu-financas\". Troque pelo subdomínio que você "
              "criou na Discloud (aba Domínios) e gere o pacote de novo.")
    prepare_server_env(site_id or "meu-financas")

    count = 0
    files = [p for p in sorted(ROOT.rglob("*")) if p.is_file() and not skipped(p)]
    folders = sorted({parent for p in files for parent in p.relative_to(ROOT).parents if parent != Path(".")},
                     key=lambda d: d.as_posix())
    # A plain, Unix-style archive (folder entries, rw-r--r-- files), like the usual zip tools make.
    with zipfile.ZipFile(OUTPUT, "w", zipfile.ZIP_DEFLATED) as zf:
        for folder in folders:
            info = zipfile.ZipInfo(folder.as_posix() + "/")
            info.create_system = 3
            info.external_attr = (0o40755 << 16) | 0x10
            zf.writestr(info, b"")
        for path in files:
            data = path.read_bytes()
            if path.suffix == ".sh":
                data = data.replace(b"\r\n", b"\n")   # Linux shell scripts must not have Windows line ends
            info = zipfile.ZipInfo(path.relative_to(ROOT).as_posix(),
                                   date_time=datetime.fromtimestamp(path.stat().st_mtime).timetuple()[:6])
            info.create_system = 3
            info.external_attr = (0o100755 if path.suffix == ".sh" else 0o100644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
            count += 1
    with zipfile.ZipFile(OUTPUT) as zf:
        names = zf.namelist()
    bad = [n for n in names if "sqlite3" in n or n == ".env" or n.startswith(("venv/", ".venv/", ".git/"))]
    if bad:
        OUTPUT.unlink()
        sys.exit(f"Pacote cancelado: arquivos que não podem subir: {bad[:5]}")
    for required in ("discloud.config", "start.sh", "manage.py", "requirements.txt", ".env.producao"):
        if required not in names:
            sys.exit(f"Pacote incompleto: falta {required}.")
    stage_folder()
    size = OUTPUT.stat().st_size / 1024
    print(f"Pacote pronto: {OUTPUT}  ({count} arquivos, {size:.0f} KB)")
    print(f"Pasta para publicar pela extensão ou pela linha de comando: {STAGE}")
    print("Não vão no pacote: o banco de dados (criado no servidor), a venv e o .env deste computador.")
    if site_id:
        print(f"Endereço do site: https://{site_id}.discloud.app (ou .discloud.dev, conforme a Discloud definir)")


if __name__ == "__main__":
    main()
