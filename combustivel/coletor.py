#!/usr/bin/env python3
"""
Coletor do painel /combustivel/: gasolina comum e aditivada nos postos da
regiao central sul de Brasilia, mais a parcela Petrobras de refinaria no DF.

Fontes, todas validadas em 06/08/2026:
- ANP, Levantamento de Precos de Combustiveis: arquivos semanais
  revendas_lpc_<ini>_<fim>.xlsx listados na pagina das ultimas semanas
  pesquisadas. Preco POR POSTO (CNPJ, bairro, bandeira). Amostra rotativa,
  ~50 postos do DF por semana. Publicado as segundas.
- ANP, serie historica semestral (ca-AAAA-SS.zip): mesma granularidade,
  usada como semente (--seed) para o grafico nascer com meses de historia.
- Petrobras (precos.petrobras.com.br): parcela da refinaria por litro de
  gasolina no DF, embutida no HTML estatico da pagina.

A ANP nao separa a Podium da aditivada generica: o produto e "GASOLINA
ADITIVADA". A aditivada entra como referencia e o painel avisa que a Podium
costuma custar mais. Menor Preco Brasil (NFC-e) foi testado e descartado:
so existe como aplicativo, sem endpoint web publico, e a versao atual exige
login gov.br. Nao reinserir sem tecnica nova.

Historico e append-only: fonte que falha nao apaga nada, so nao acrescenta.
Rodada em que TODAS as fontes falham nao carimba o campo "ultima".

Uso:
  python3 combustivel/coletor.py           # rodada normal (semanais novas + Petrobras)
  python3 combustivel/coletor.py --seed    # inclui a semente semestral da ANP
"""
import io
import json
import os
import re
import socket
import ssl
import sys
import time
import unicodedata
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

# O runner do GitHub nao roteia IPv6 e o gov.br anuncia AAAA: a primeira
# rodada no Actions morreu com "Network is unreachable" na ANP enquanto a
# Petrobras passava. Forcar IPv4 resolve e nao muda nada no Mac.
_getaddrinfo = socket.getaddrinfo


def _so_ipv4(host, port, family=0, *args, **kw):
    return _getaddrinfo(host, port, socket.AF_INET, *args, **kw)


socket.getaddrinfo = _so_ipv4

BASE = Path(__file__).parent
HIST = BASE / "historico.json"

ANP_SEMANAS = ("https://www.gov.br/anp/pt-br/assuntos/precos-e-defesa-da-concorrencia/"
               "precos/levantamento-de-precos-de-combustiveis-ultimas-semanas-pesquisadas")
ANP_SEMENTES = ["https://www.gov.br/anp/pt-br/centrais-de-conteudo/dados-abertos/"
                "arquivos/shpc/dsas/ca/ca-2026-01.zip"]
PETROBRAS = "https://precos.petrobras.com.br/web/precos-dos-combustiveis/w/gasolina/df"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# Do IP de datacenter (GitHub Actions) nao vale insistir muito: a variavel
# encurta as retentativas, mesma ideia do PRECOS_DATACENTER do painel de
# precos. Explicita de proposito, nunca autodetectada.
DATACENTER = os.environ.get("COMBUSTIVEL_DATACENTER") == "1"
TENTATIVAS = 1 if DATACENTER else 3

# Piso e teto por litro: piso barra erro de digitacao, teto barra absurdo.
# Licao do painel de precos: filtro que so valida por baixo deixa passar o
# absurdo por cima.
PRECO_MIN, PRECO_MAX = 4.50, 9.50
PETRO_MIN, PETRO_MAX = 1.00, 3.50

PRODUTOS = {"GASOLINA": "c", "GASOLINA COMUM": "c", "GASOLINA ADITIVADA": "a"}

# Regiao definida pelo usuario em 06/08/2026 num recorte de mapa: Plano
# Piloto sul e adjacencias. Jardim Botanico e Altiplano Leste estao no mapa
# mas nunca apareceram na amostra da ANP; ficam na lista para quando
# aparecerem. Asa Norte fica FORA (o recorte para no Eixo Monumental).
REGIAO_EXATA = {
    "ASA SUL", "SETOR SUDOESTE", "SUDOESTE", "OCTOGONAL",
    "CRUZEIRO", "CRUZEIRO VELHO", "CRUZEIRO NOVO",
    "GUARA", "GUARA I", "GUARA II", "ZONA INDUSTRIAL (GUARA)",
    "SIA", "SIG", "SETORES COMPLEMENTARES",
    "N BANDEIRANTE", "NUCLEO BANDEIRANTE", "CANDANGOLANDIA",
    "PARK WAY", "LAGO SUL", "SETOR DE HABITACOES INDIVIDUAIS SUL",
    "VILA PLANALTO", "JARDIM BOTANICO", "ALTIPLANO LESTE",
}
REGIAO_CONTEM = ("HABITACOES INDIVIDUAIS SUL", "JARDIM BOTANICO", "ALTIPLANO",
                 "ZONA INDUSTRIAL (GUARA)", "BANDEIRANTE", "CANDANGOLANDIA")
REGIAO_NUNCA = ("NORTE", "AGUAS CLARAS")  # barra Lago Norte, SHIN, Aguas Claras


def normaliza(txt):
    txt = unicodedata.normalize("NFD", str(txt or ""))
    txt = "".join(c for c in txt if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", txt).strip().upper()


def na_regiao(bairro):
    b = normaliza(bairro)
    if not b:
        return False
    if any(n in b for n in REGIAO_NUNCA):
        return False
    if b in REGIAO_EXATA:
        return True
    return any(c in b for c in REGIAO_CONTEM)


def baixa(url, binario=False):
    """GET com cara de navegador e retentativas. Devolve bytes ou texto."""
    erro = None
    for t in range(1, TENTATIVAS + 1):
        try:
            req = Request(url, headers={"User-Agent": UA, "Accept": "*/*",
                                        "Accept-Language": "pt-BR,pt;q=0.9"})
            ctx = ssl.create_default_context()
            with urlopen(req, timeout=60 if binario else 30, context=ctx) as r:
                dados = r.read()
            return dados if binario else dados.decode("utf-8", errors="replace")
        except Exception as e:
            erro = e
            if t < TENTATIVAS:
                time.sleep(10 * t)
    raise RuntimeError(f"{type(erro).__name__}: {erro}")


def limpa_cnpj(v):
    dig = re.sub(r"\D", "", str(v or ""))
    return dig.zfill(14) if dig else ""


def carrega():
    """Uniao do historico local com o publicado. Append-only dos dois lados,
    entao a uniao e sempre segura; "ultima" fica com o instante mais recente.
    Evita o acidente do painel de precos, em que rodar so a pagina apagava
    as coletas que o Actions tinha juntado no meio tempo."""
    locais = {}
    if HIST.exists():
        try:
            locais = json.loads(HIST.read_text(encoding="utf-8"))
        except Exception:
            locais = {}
    user = os.environ.get("PA_USER", "rafaelcortopassi")
    publicado = {}
    try:
        with urlopen(Request(f"https://{user}.pythonanywhere.com/combustivel/historico.json",
                             headers={"User-Agent": UA}), timeout=30) as r:
            publicado = json.load(r)
    except Exception:
        publicado = {}
    return _funde(locais, publicado)


_GEO_NIVEL = {"bairro": 0, "quadra": 1, "cep": 2}


def _funde(a, b):
    h = {"postos": {}, "coletas": [], "semanas": [], "semanas_ruins": [],
         "sementes": [], "petrobras": [], "ultima": None}
    for fonte in (a, b):
        if not isinstance(fonte, dict):
            continue
        # Fusao campo a campo: um lado pode ter enriquecido o cadastro (cep,
        # lat/lng da geocodificacao) que o outro ainda nao tem. Substituir o
        # dicionario inteiro ja apagou coordenadas uma vez; nunca de novo.
        for cnpj, posto in (fonte.get("postos") or {}).items():
            alvo = h["postos"].setdefault(cnpj, {})
            novo = {k: v for k, v in posto.items() if v is not None}
            # Posicao: fica a mais precisa dos dois lados (cep > quadra >
            # bairro). Sem isso o publicado, que vem por ultimo, devolvia o
            # posto ao centroide do bairro logo depois de geocodificado.
            if _GEO_NIVEL.get(alvo.get("geo"), -1) > _GEO_NIVEL.get(novo.get("geo"), -1):
                for k in ("lat", "lng", "geo"):
                    novo.pop(k, None)
            alvo.update(novo)
        h["semanas"] = sorted(set(h["semanas"]) | set(fonte.get("semanas") or []))
        h["semanas_ruins"] = sorted(set(h["semanas_ruins"])
                                    | set(fonte.get("semanas_ruins") or []))
        h["sementes"] = sorted(set(h["sementes"]) | set(fonte.get("sementes") or []))
        for c in (fonte.get("coletas") or []):
            h["coletas"].append(tuple(c))
        for p in (fonte.get("petrobras") or []):
            h["petrobras"].append(tuple(p))
        u = fonte.get("ultima")
        if u and (not h["ultima"] or u > h["ultima"]):
            h["ultima"] = u
    h["coletas"] = sorted(set(map(tuple, h["coletas"])), key=lambda c: (c[2], c[0], c[1]))
    # Petrobras: um valor por dia, o ultimo lido vale.
    por_dia = {}
    for d, v in sorted(h["petrobras"]):
        por_dia[d] = v
    h["petrobras"] = sorted(por_dia.items())
    h["coletas"] = [list(c) for c in h["coletas"]]
    h["petrobras"] = [list(p) for p in h["petrobras"]]
    return h


def registra(h, cnpj, nome, endereco, bairro, bandeira, produto, data_iso, preco,
             cep=None):
    if not cnpj or produto not in PRODUTOS:
        return 0
    if not (PRECO_MIN <= preco <= PRECO_MAX):
        return 0
    if not na_regiao(bairro):
        return 0
    p = h["postos"].setdefault(cnpj, {})
    # O cadastro do posto e atualizado sempre que vem preenchido: o arquivo
    # semanal as vezes traz FANTASIA vazia que a semente semestral tem.
    if nome:
        p["nome"] = normaliza(nome)
    if endereco:
        p.setdefault("endereco", normaliza(endereco))
    if bairro:
        p["bairro"] = normaliza(bairro)
    if bandeira:
        p["bandeira"] = normaliza(bandeira)
    cep_dig = re.sub(r"\D", "", str(cep or ""))
    if len(cep_dig) == 8:
        p.setdefault("cep", cep_dig)
    c = [cnpj, PRODUTOS[produto], data_iso, round(float(preco), 3)]
    if c not in h["coletas"]:
        h["coletas"].append(c)
        return 1
    return 0


def anp_semanais(h):
    """Descobre na pagina da ANP as semanas publicadas e processa as que o
    historico ainda nao tem. E a conferencia horaria: quase sempre nao ha
    arquivo novo e a rodada termina em um GET."""
    pagina = baixa(ANP_SEMANAS)
    links = re.findall(r'href="(https://[^"]*revendas_lpc_(\d{4}-\d{2}-\d{2})_'
                       r'(\d{4}-\d{2}-\d{2})\.xlsx)"', pagina)
    novas, coletas = 0, 0
    vistos = set()
    for url, ini, fim in links:
        semana = f"{ini}_{fim}"
        if semana in vistos or semana in h["semanas"] or semana in h["semanas_ruins"]:
            continue
        vistos.add(semana)
        try:
            corpo = baixa(url, binario=True)
        except Exception as e:
            # Falha de rede fica de fora da lista ruim: vale tentar de novo
            # na proxima rodada.
            print(f"  ANP semana {semana}: download FALHOU ({e})")
            continue
        try:
            coletas += _processa_xlsx(h, corpo)
            h["semanas"].append(semana)
            h["semanas"].sort()
            novas += 1
            print(f"  ANP semana {semana}: processada")
        except Exception as e:
            # XLSX que o openpyxl nao abre (formato antigo de 2022/2023).
            # Entra na lista ruim para a rodada horaria nao rebaixar para
            # sempre um arquivo que nunca vai abrir.
            h["semanas_ruins"].append(semana)
            h["semanas_ruins"].sort()
            print(f"  ANP semana {semana}: XLSX ilegivel, na lista ruim ({e})")
    return len(links), novas, coletas


def _processa_xlsx(h, corpo):
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(corpo), read_only=True)
    ws = wb.worksheets[0]
    n, comecou = 0, False
    for row in ws.iter_rows(values_only=True):
        if not comecou:
            comecou = str(row[0]).strip() == "CNPJ"
            continue
        if not row[0]:
            continue
        estado = normaliza(row[9])
        if estado != "DISTRITO FEDERAL":
            continue
        data = row[14]
        data_iso = data.strftime("%Y-%m-%d") if hasattr(data, "strftime") else str(data)[:10]
        try:
            preco = float(row[13])
        except (TypeError, ValueError):
            continue
        endereco = " ".join(str(x) for x in (row[3], row[4]) if x)
        n += registra(h, limpa_cnpj(row[0]), row[2] or row[1], endereco,
                      row[6], row[10], normaliza(row[11]), data_iso, preco,
                      cep=row[7])
    return n


def anp_semente(h):
    """Serie semestral por posto, para o grafico nascer com meses. Roda uma
    vez por arquivo; o nome do zip fica em h["sementes"]."""
    import csv
    total = 0
    for url in ANP_SEMENTES:
        rotulo = url.rsplit("/", 1)[-1].replace(".zip", "")
        if rotulo in h["sementes"]:
            continue
        corpo = baixa(url, binario=True)
        z = zipfile.ZipFile(io.BytesIO(corpo))
        nome = z.namelist()[0]
        with z.open(nome) as f:
            texto = io.TextIOWrapper(f, encoding="utf-8", errors="replace")
            leitor = csv.reader(texto, delimiter=";")
            next(leitor)
            n = 0
            for row in leitor:
                if len(row) < 16 or row[1].strip() != "DF":
                    continue
                produto = normaliza(row[10])
                if produto not in PRODUTOS:
                    continue
                try:
                    preco = float(row[12].replace(",", "."))
                    d, m, a = row[11].strip().split("/")
                    data_iso = f"{a}-{m}-{d}"
                except (ValueError, IndexError):
                    continue
                endereco = " ".join(x.strip() for x in (row[5], row[6]) if x.strip())
                n += registra(h, limpa_cnpj(row[4]), row[3], endereco,
                              row[8], row[15], produto, data_iso, preco,
                              cep=row[9])
        h["sementes"].append(rotulo)
        total += n
        print(f"  Semente {rotulo}: {n} coletas da regiao")
    return total


# Centroides aproximados por bairro, reserva para posto sem CEP geocodificavel.
# O pino ganha a etiqueta de posicao aproximada no mapa.
BAIRRO_CENTRO = {
    "ASA SUL": (-15.8146, -47.9033),
    "SETOR SUDOESTE": (-15.7952, -47.9265), "SUDOESTE": (-15.7952, -47.9265),
    "OCTOGONAL": (-15.8046, -47.9330),
    "CRUZEIRO": (-15.7906, -47.9370), "CRUZEIRO VELHO": (-15.7925, -47.9330),
    "CRUZEIRO NOVO": (-15.7860, -47.9435),
    "VILA PLANALTO": (-15.7838, -47.8600),
    "GUARA": (-15.8270, -47.9750), "GUARA I": (-15.8200, -47.9700),
    "GUARA II": (-15.8340, -47.9780),
    "ZONA INDUSTRIAL (GUARA)": (-15.8110, -47.9600),
    "SIA": (-15.8000, -47.9540), "SIG": (-15.7980, -47.9380),
    "SETORES COMPLEMENTARES": (-15.8030, -47.9470),
    "N BANDEIRANTE": (-15.8710, -47.9660), "NUCLEO BANDEIRANTE": (-15.8710, -47.9660),
    "CANDANGOLANDIA": (-15.8530, -47.9550),
    "PARK WAY": (-15.8900, -47.9600),
    "LAGO SUL": (-15.8320, -47.8700),
    "SETOR DE HABITACOES INDIVIDUAIS SUL": (-15.8320, -47.8700),
    "JARDIM BOTANICO": (-15.8720, -47.8000),
    "ALTIPLANO LESTE": (-15.8000, -47.8200),
}
# Caixa de sanidade do DF: coordenada fora dela e CEP errado na base de
# geocodificacao, e o posto cai para o centroide do bairro.
DF_LAT = (-16.12, -15.40)
DF_LNG = (-48.35, -47.25)


def _geo_brasilapi(cep):
    with urlopen(Request(f"https://brasilapi.com.br/api/cep/v2/{cep}",
                         headers={"User-Agent": UA}), timeout=15) as r:
        j = json.load(r)
    c = (j.get("location") or {}).get("coordinates") or {}
    if c.get("latitude") and c.get("longitude"):
        return float(c["latitude"]), float(c["longitude"])
    return None


def _geo_awesome(cep):
    with urlopen(Request(f"https://cep.awesomeapi.com.br/json/{cep}",
                         headers={"User-Agent": UA}), timeout=15) as r:
        j = json.load(r)
    if j.get("lat") and j.get("lng"):
        return float(j["lat"]), float(j["lng"])
    return None

# Padroes de quadra no endereco da ANP. O Nominatim (OSM) nao acha "SQS 212
# BLOCO A PAG S/N", mas acha "SQS 212, Brasilia": a quadra tem no do OSM, o
# bloco e o lote nao. Posicao em nivel de quadra ja e ordens de grandeza
# melhor que o centroide do bairro (Asa Sul tem 5 km de comprimento).
_QUADRAS = [
    (r"\b(?:SQS|SHCS|SHC/SUL|SHC/S|SQS-SUPERQUADRA SUL)\b[^0-9]*(\d{3})\b", "SQS {}"),
    (r"\bCLSW\b[^0-9]*(\d{3})\b", "CLSW {}"),
    (r"\bSQSW\b[^0-9]*(\d{3})\b", "SQSW {}"),
    (r"\bQI\s*(\d{1,2})\b", "SHIS QI {}"),
    (r"\bQE\s*(\d{1,2})\b", "QE {} Guara"),
    (r"\bSIA\s+TRECHO\s*(\d{1,2})\b", "SIA Trecho {}"),
    (r"\bSCIA\s+QUADRA\s*(\d{1,2})\b", "SCIA Quadra {}"),
    (r"\bSHCES\s+QUADRA\s*(\d{3,4})\b", "SHCES Quadra {}"),
    (r"\bSTRC\b", "STRC"),
    (r"\bSPM\b|\bSPMS\b|\bSETOR DE POSTOS E MOTEIS\b", "Setor de Postos e Moteis Sul"),
]


def quadra_do_endereco(endereco):
    e = normaliza(endereco)
    for rx, fmt in _QUADRAS:
        m = re.search(rx, e)
        if m:
            return fmt.format(*[str(int(g)) if g.isdigit() else g for g in m.groups()])
    return None


def _geo_nominatim(endereco):
    """Geocodificacao por quadra via Nominatim. Uma chamada por segundo no
    maximo, com User-Agent identificado, como pede a politica do OSM."""
    q = quadra_do_endereco(endereco)
    if not q:
        return None
    from urllib.parse import quote
    url = ("https://nominatim.openstreetmap.org/search?format=json&limit=1"
           "&countrycodes=br&q=" + quote(q + ", Brasilia, Distrito Federal"))
    req = Request(url, headers={"User-Agent": "painel-combustivel-df/1.0 "
                                "(rafaelmcortopassi@gmail.com)"})
    with urlopen(req, timeout=20) as r:
        j = json.load(r)
    time.sleep(1.1)
    if j and j[0].get("lat"):
        return float(j[0]["lat"]), float(j[0]["lon"])
    return None


def _perto_do_bairro(p, lat, lng, km=7.0):
    """Rejeita acerto do Nominatim longe demais do bairro declarado pela ANP
    (uma "QE 2" pode existir em outra cidade-satelite)."""
    c = BAIRRO_CENTRO.get(p.get("bairro") or "")
    if not c:
        return True
    dlat = (lat - c[0]) * 111.0
    dlng = (lng - c[1]) * 111.0 * 0.96
    return (dlat * dlat + dlng * dlng) ** 0.5 <= km


def geocodifica(h, limite=30):
    """Da lat/lng a cada posto, uma vez so (fica gravado no historico).
    Por CEP via BrasilAPI e AwesomeAPI (nesta ordem: a Awesome recusa IP de
    datacenter estrangeiro, licao do cambio do painel de precos); sem CEP ou
    sem acerto, centroide do bairro com geo="bairro". Posto marcado como
    "bairro" que ganhar CEP depois tenta de novo a via precisa."""
    pendentes = [(c, p) for c, p in h["postos"].items()
                 if p.get("lat") is None
                 or (p.get("geo") == "bairro" and p.get("cep"))
                 or (p.get("geo") == "bairro" and not p.get("qtent"))]
    feitos = 0
    for cnpj, p in pendentes[:limite]:
        lat = lng = None
        origem = None
        if p.get("cep"):
            for fn in (_geo_brasilapi, _geo_awesome):
                try:
                    r = fn(p["cep"])
                except Exception:
                    r = None
                if r and DF_LAT[0] <= r[0] <= DF_LAT[1] and DF_LNG[0] <= r[1] <= DF_LNG[1]:
                    lat, lng = r
                    origem = "cep"
                    break
                time.sleep(0.3)
        if lat is None and not p.get("qtent"):
            # Sem CEP (ou CEP que nao resolveu): tenta a quadra no OSM, uma
            # vez so por posto; a marca qtent evita bater no Nominatim toda
            # rodada por um endereco que ele nunca vai achar.
            p["qtent"] = True
            try:
                r = _geo_nominatim(p.get("endereco") or "")
            except Exception:
                r = None
            if (r and DF_LAT[0] <= r[0] <= DF_LAT[1] and DF_LNG[0] <= r[1] <= DF_LNG[1]
                    and _perto_do_bairro(p, *r)):
                lat, lng = r
                origem = "quadra"
        if lat is None:
            if p.get("geo") == "bairro":
                continue  # ja esta no centroide e nem CEP nem quadra destravaram
            c = BAIRRO_CENTRO.get(p.get("bairro") or "")
            if not c:
                continue
            lat, lng = c
            origem = "bairro"
        p["lat"], p["lng"], p["geo"] = round(lat, 6), round(lng, 6), origem
        feitos += 1
    return feitos, len(pendentes)


def petrobras(h):
    """Parcela da refinaria por litro de gasolina no DF. Valor raro de mudar;
    guarda um por dia."""
    pagina = baixa(PETROBRAS)
    m = (re.search(r'id="preco1"[^>]*>\s*([\d]+,[\d]+)\s*<', pagina)
         or re.search(r'telafinal-tarifa5-numero"[^>]*>\s*([\d]+,[\d]+)\s*<', pagina))
    if not m:
        raise RuntimeError("nao achei o valor na pagina")
    valor = float(m.group(1).replace(",", "."))
    if not (PETRO_MIN <= valor <= PETRO_MAX):
        raise RuntimeError(f"valor fora da faixa de sanidade: {valor}")
    hoje = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    por_dia = {d: v for d, v in h["petrobras"]}
    mudou = por_dia.get(hoje) != valor
    por_dia[hoje] = valor
    h["petrobras"] = [list(p) for p in sorted(por_dia.items())]
    return valor, mudou


def main():
    h = carrega()
    antes = len(h["coletas"])
    fontes_ok = 0

    if "--seed" in sys.argv:
        try:
            anp_semente(h)
            fontes_ok += 1
        except Exception as e:
            print(f"  Semente semestral FALHOU ({e})")

    try:
        listadas, novas, coletas = anp_semanais(h)
        fontes_ok += 1
        print(f"  ANP: {listadas} semanas listadas, {novas} novas, "
              f"{coletas} coletas novas da regiao")
    except Exception as e:
        print(f"  ANP semanal FALHOU ({e})")

    try:
        valor, mudou = petrobras(h)
        fontes_ok += 1
        print(f"  Petrobras DF: R$ {valor:.2f}/litro na refinaria"
              + (" (novo)" if mudou else ""))
    except Exception as e:
        print(f"  Petrobras FALHOU ({e})")

    if fontes_ok == 0:
        print("NADA COLETADO: todas as fontes falharam; historico intocado.")
        return 1

    try:
        feitos, pend = geocodifica(h)
        if pend:
            print(f"  Geocodificacao: {feitos} de {pend} postos pendentes resolvidos")
    except Exception as e:
        print(f"  Geocodificacao FALHOU ({e}); o mapa segue com o que ja tem")

    h["ultima"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    HIST.write_text(json.dumps(h, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8")
    print(f"Historico: {len(h['coletas'])} coletas ({len(h['coletas']) - antes} novas), "
          f"{len(h['postos'])} postos, {len(h['semanas'])} semanas, "
          f"{len(h['petrobras'])} dias de Petrobras.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
