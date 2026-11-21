"""Copy the system's Python code into the Android app project (standard library only).

    python scripts/montar_android.py

The app runs the same Django code as the website (see expensemanager/localserver.py). Only the
code goes: never the local database, the virtualenv, the .env files or the tests. Django itself
is installed into the app by Chaquopy (D:/prj/financas-android/app/build.gradle.kts).
"""
import fnmatch
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# The Android build tools do not work with accents in the path on Windows (FinançasPessoais has
# a "ç"), so the Android project lives in D:/prj/financas-android.
ANDROID = Path("D:/prj/financas-android")
TARGET = ANDROID / "app" / "src" / "main" / "python"
MARK = ".pasta-gerada-pelo-montar_android"
PACKAGES = ("app", "expensemanager")
SKIP_DIRS = {"__pycache__", "tests", "staticfiles"}
SKIP_FILES = ["*.pyc", "*.sqlite3*", ".env*", "*.zip", "*.log"]


def wanted(path):
    rel = path.relative_to(ROOT)
    if set(rel.parts) & SKIP_DIRS:
        return False
    return not any(fnmatch.fnmatch(path.name, pattern) for pattern in SKIP_FILES)


def main():
    if not TARGET.parent.exists():
        sys.exit(f"Projeto Android não encontrado em {ANDROID}.")
    if TARGET.exists():
        if any(TARGET.iterdir()) and not (TARGET / MARK).exists():
            sys.exit(f"A pasta {TARGET} existe e não foi criada por este script. Renomeie ou apague e rode de novo.")
        shutil.rmtree(TARGET)
    TARGET.mkdir(parents=True)
    count = 0
    for package in PACKAGES:
        for path in sorted((ROOT / package).rglob("*")):
            if path.is_file() and wanted(path):
                dest = TARGET / path.relative_to(ROOT)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)
                count += 1
    (TARGET / MARK).write_text("Gerada por finance-manager/scripts/montar_android.py. Não edite: é refeita a cada build.\n",
                               encoding="utf-8")
    bad = [p for p in TARGET.rglob("*") if p.name.startswith(".env") or "sqlite3" in p.name or "tests" in p.parts]
    if bad:
        shutil.rmtree(TARGET)
        sys.exit(f"Cancelado: arquivos que não podem ir para o app: {bad[:3]}")
    print(f"Código do sistema copiado para o app: {count} arquivos em {TARGET}")


if __name__ == "__main__":
    main()
