#!/usr/bin/env python3
"""
Publica o painel de combustivel no PythonAnywhere.

Sobe combustivel.html com dois nomes (index.html e combustivel.html) para
/home/<user>/combustivel/ e garante o mapeamento estatico /combustivel/ no
web app. O historico.json vai junto (e ele que permite a arbitragem entre os
dois coletores, Actions e Mac) e o stamp.txt tambem (e ele que faz a aba
aberta se recarregar sozinha quando ha coleta nova).
Reaproveita o PA_TOKEN de painel/.env, o mesmo dos outros paineis.

Uso:
  python3 combustivel/deploy.py
  python3 combustivel/deploy.py --no-reload
"""
import json
import os
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BASE = Path(__file__).parent
RAIZ = BASE.parent
ARQUIVO = BASE / "combustivel.html"
HISTORICO = BASE / "historico.json"
STAMP = BASE / "stamp.txt"
ENV = RAIZ / "painel" / ".env"
REMOTO = "combustivel"
API = "https://www.pythonanywhere.com"


def ler_env():
    """painel/.env no Mac, variavel de ambiente no GitHub Actions. Quem esta
    no ambiente ganha do arquivo."""
    cfg = {}
    if ENV.exists():
        for line in ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    for k in ("PA_TOKEN", "PA_USER", "PAINEL_URL"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg


def chamar(url, token, data=None, method="GET", headers=None):
    h = {"Authorization": f"Token {token}"}
    h.update(headers or {})
    req = Request(url, data=data, method=method, headers=h)
    with urlopen(req, timeout=90) as r:
        corpo = r.read()
        return r.getcode(), corpo


def upload(user, token, path, nome_remoto):
    dest = f"/home/{user}/{REMOTO}/{nome_remoto}"
    url = f"{API}/api/v0/user/{user}/files/path{dest}"
    boundary = "----pa" + uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="content"; filename="{nome_remoto}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    code, _ = chamar(url, token, body, "POST",
                     {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    return code, dest


def garante_mapeamento(user, dominio, token):
    """O plano free so tem um web app. O painel e estatico, entao vive de um
    mapeamento de diretorio, sem tocar no WSGI do painel de prazos."""
    url = f"{API}/api/v0/user/{user}/webapps/{dominio}/static_files/"
    _, corpo = chamar(url, token)
    atuais = json.loads(corpo)
    for m in atuais:
        if m.get("url") == f"/{REMOTO}/":
            return "ja existia"
    dados = json.dumps({"url": f"/{REMOTO}/",
                        "path": f"/home/{user}/{REMOTO}/"}).encode()
    code, _ = chamar(url, token, dados, "POST", {"Content-Type": "application/json"})
    return f"criado (HTTP {code})"


def main():
    cfg = ler_env()
    token = cfg.get("PA_TOKEN")
    if not token:
        print("ERRO: falta PA_TOKEN em painel/.env")
        return 1
    user = (cfg.get("PA_USER")
            or cfg.get("PAINEL_URL", "").split("//")[-1].split("/")[0].split(".")[0])
    if not user:
        print("ERRO: nao consegui deduzir o usuario do PythonAnywhere")
        return 1
    dominio = f"{user}.pythonanywhere.com"

    if not ARQUIVO.exists():
        print(f"ERRO: {ARQUIVO.name} nao existe. Rode combustivel/pagina.py antes.")
        return 1

    try:
        print(f"Mapeamento /{REMOTO}/: {garante_mapeamento(user, dominio, token)}")
    except Exception as e:
        print(f"Aviso: nao consegui conferir o mapeamento ({type(e).__name__}: {e})")

    kb = ARQUIVO.stat().st_size / 1024
    for nome in ("index.html", "combustivel.html"):
        for tent in range(1, 4):
            try:
                code, dest = upload(user, token, ARQUIVO, nome)
                print(f"OK {code}: {dest} ({kb:.0f} KB)")
                break
            except HTTPError as e:
                erro = f"HTTP {e.code}: {e.read().decode(errors='replace')[:120]}"
            except Exception as e:
                erro = f"{type(e).__name__}: {e}"
            if tent < 3:
                print(f"  tentativa {tent} de {nome} falhou ({erro}); repetindo")
                time.sleep(tent * 10)
            else:
                print(f"FALHOU upload de {nome}: {erro}")
                return 1

    for extra in (HISTORICO, STAMP):
        if extra.exists():
            try:
                code, dest = upload(user, token, extra, extra.name)
                print(f"OK {code}: {dest}")
            except Exception as e:
                print(f"Aviso: {extra.name} nao subiu ({type(e).__name__}: {e})")

    if "--no-reload" not in sys.argv:
        try:
            code, _ = chamar(f"{API}/api/v0/user/{user}/webapps/{dominio}/reload/",
                             token, b"", "POST")
            print(f"Reload OK ({code}).")
        except Exception as e:
            print(f"Aviso: reload nao concluiu ({type(e).__name__}: {e}). "
                  f"O painel e estatico, o upload ja vale.")

    print(f"No ar: https://{dominio}/{REMOTO}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
