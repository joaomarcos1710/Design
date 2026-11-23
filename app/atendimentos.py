#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Controle de Atendimentos - versão em Python da planilha.

Como funciona, em uma frase: este arquivo sobe um pequeno site que só existe
no seu computador (endereço 127.0.0.1) e guarda os dados em um arquivo SQLite.

- Usa apenas a biblioteca padrão do Python (3.8+): não precisa de "pip install".
- Não acessa a internet e não é acessível por outros computadores.
- Os dados ficam em  dados/atendimentos.db  (ao lado deste arquivo), ou no
  caminho indicado na variável de ambiente ATENDIMENTOS_DB.

Uso:
    python atendimentos.py            # abre o navegador sozinho
    python atendimentos.py --porta 9000 --sem-navegador

Organização do código (de cima para baixo):
    1. Configuração e banco de dados
    2. Regras de negócio (dias úteis, prazos, CPF)
    3. Páginas HTML
    4. Servidor web local
"""
import argparse
import csv
import html
import io
import os
import secrets
import shutil
import sqlite3
import sys
import webbrowser
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

# ---------------------------------------------------------------------------
# 1. CONFIGURAÇÃO E BANCO DE DADOS
# ---------------------------------------------------------------------------

PASTA = os.path.dirname(os.path.abspath(__file__))
CAMINHO_DB = os.environ.get("ATENDIMENTOS_DB") or os.path.join(PASTA, "dados", "atendimentos.db")
CONCLUIDO = "Concluído"  # status especial: as regras dependem dele
TOKEN = secrets.token_urlsafe(24)  # protege os formulários (ver Servidor)

# Campos de um atendimento: (nome no banco, rótulo na tela, tipo)
# tipo: data | texto | longo | lista:<nome da lista>
CAMPOS = [
    ("data", "Data do atendimento", "data"),
    ("cliente", "Cliente", "texto"),
    ("cpf", "CPF", "texto"),
    ("contrato", "Contrato", "texto"),
    ("produto", "Produto", "lista:produto"),
    ("indicador", "Indicador", "lista:indicador"),
    ("telefone", "Telefone", "texto"),
    ("email", "E-mail", "texto"),
    ("canal", "Canal de atendimento", "lista:canal"),
    ("demanda", "Código da demanda", "texto"),
    ("feito", "O que foi feito", "longo"),
    ("pendencia", "Pendência", "longo"),
    ("tipo", "Tipo de pendência", "lista:tipo"),
    ("prioridade", "Prioridade", "lista:prioridade"),
    ("proxima_acao", "Próxima ação", "longo"),
    ("data_proxima", "Data da próxima ação", "data"),
    ("status", "Status", "lista:status"),
    ("prazo", "Prazo para atendimento", "data"),
    ("data_resposta", "Data da resposta", "data"),
]
NOMES = [c[0] for c in CAMPOS]

# Opções iniciais das listas suspensas (editáveis depois na tela "Listas").
# A lista de indicadores começa vazia de propósito: cada pessoa preenche a sua.
LISTAS_PADRAO = {
    "canal": ["Telefone", "E-mail", "WhatsApp", "Chat", "Presencial", "Sistema / Demanda"],
    "status": ["Pendente", "Em andamento", "Aguardando cliente", "Aguardando terceiro", CONCLUIDO],
    "produto": ["Habitacional", "Consignado", "Cheque especial", "Cartão de crédito", "Crédito pessoal", "Outro"],
    "tipo": ["Documentação", "Financeiro / pagamento", "Análise interna", "Sistema", "Outra área", "Cliente", "Outro"],
    "prioridade": ["Alta", "Média", "Baixa"],
    "indicador": [],
}
ROTULO_LISTA = {
    "canal": "Canais de atendimento", "status": "Status", "produto": "Produtos",
    "tipo": "Tipos de pendência", "prioridade": "Prioridades", "indicador": "Indicadores",
}


def conectar():
    """Abre o banco. Cada requisição usa uma conexão curta e a fecha no fim."""
    con = sqlite3.connect(CAMINHO_DB)
    con.row_factory = sqlite3.Row
    return con


def preparar_banco():
    """Cria a pasta, as tabelas e as listas padrão na primeira execução."""
    os.makedirs(os.path.dirname(CAMINHO_DB), exist_ok=True)
    con = conectar()
    colunas = ", ".join("%s TEXT NOT NULL DEFAULT ''" % n for n in NOMES)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS atendimentos (
            id INTEGER PRIMARY KEY AUTOINCREMENT, %s,
            criado_em TEXT, atualizado_em TEXT);
        CREATE TABLE IF NOT EXISTS listas (
            tipo TEXT NOT NULL, valor TEXT NOT NULL, ordem INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS feriados (data TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS config (chave TEXT PRIMARY KEY, valor TEXT);
        """ % colunas
    )
    if con.execute("SELECT COUNT(*) FROM listas").fetchone()[0] == 0:
        for tipo, valores in LISTAS_PADRAO.items():
            for i, v in enumerate(valores):
                con.execute("INSERT INTO listas VALUES (?,?,?)", (tipo, v, i))
        con.execute("INSERT OR IGNORE INTO config VALUES ('alerta_dias', '3')")
    con.commit()
    con.close()


def fazer_backup():
    """Uma cópia do banco por dia em dados/backup, mantendo as 10 mais recentes."""
    if not os.path.exists(CAMINHO_DB):
        return
    pasta = os.path.join(os.path.dirname(CAMINHO_DB), "backup")
    os.makedirs(pasta, exist_ok=True)
    destino = os.path.join(pasta, "atendimentos-%s.db" % date.today().isoformat())
    if not os.path.exists(destino):
        shutil.copy2(CAMINHO_DB, destino)
    antigos = sorted(f for f in os.listdir(pasta) if f.endswith(".db"))[:-10]
    for f in antigos:
        os.remove(os.path.join(pasta, f))


def ler_lista(con, tipo):
    return [r["valor"] for r in con.execute(
        "SELECT valor FROM listas WHERE tipo=? ORDER BY ordem", (tipo,))]


def ler_feriados(con):
    return set(r["data"] for r in con.execute("SELECT data FROM feriados"))


def ler_alerta(con):
    r = con.execute("SELECT valor FROM config WHERE chave='alerta_dias'").fetchone()
    try:
        return max(0, int(r["valor"]))
    except (TypeError, ValueError):
        return 3


# ---------------------------------------------------------------------------
# 2. REGRAS DE NEGÓCIO
# ---------------------------------------------------------------------------

def para_data(texto):
    """'2026-11-18' -> date. Devolve None se estiver vazio ou inválido."""
    try:
        return datetime.strptime(texto or "", "%Y-%m-%d").date()
    except ValueError:
        return None


def br(texto):
    """'2026-11-18' -> '18/11/2026' (só para exibir)."""
    d = para_data(texto)
    return d.strftime("%d/%m/%Y") if d else ""


def dias_uteis(inicio, fim, feriados):
    """Dias úteis decorridos entre duas datas, sem contar o dia inicial.

    Mesma conta da planilha: DIATRABALHOTOTAL(inicio; fim; feriados) - 1.
    Exemplo: de sexta para a segunda seguinte = 1 dia útil.
    """
    if not inicio or not fim or fim < inicio:
        return 0
    total, dia = 0, inicio
    while dia <= fim:
        if dia.weekday() < 5 and dia.isoformat() not in feriados:
            total += 1
        dia += timedelta(days=1)
    return max(0, total - 1)


def so_digitos(texto):
    return "".join(ch for ch in (texto or "") if ch.isdigit())


def validar_cpf(texto):
    """Devolve (cpf formatado, mensagem de erro). Campo vazio é aceito."""
    d = so_digitos(texto)
    if not d:
        return "", None
    if len(d) != 11:
        return texto, "CPF deve ter 11 dígitos."
    # Dígitos verificadores: pega erro de digitação na hora.
    for n in (9, 10):
        soma = sum(int(d[i]) * (n + 1 - i) for i in range(n))
        if (soma * 10) % 11 % 10 != int(d[n]):
            return texto, "CPF inválido: confira os dígitos."
    return "%s.%s.%s-%s" % (d[:3], d[3:6], d[6:9], d[9:]), None


def enriquecer(linha, hoje, feriados, alerta):
    """Acrescenta a um atendimento os campos calculados (os da 'coluna cinza')."""
    a = dict(linha)
    inicio = para_data(a["data"])
    prazo = para_data(a["prazo"])
    resposta = para_data(a["data_resposta"])
    proxima = para_data(a["data_proxima"])
    a["aberto"] = bool(a["status"]) and a["status"] != CONCLUIDO
    a["dias_aberto"] = dias_uteis(inicio, hoje, feriados) if a["aberto"] and inicio else None
    a["tempo_resolucao"] = (
        dias_uteis(inicio, resposta, feriados)
        if a["status"] == CONCLUIDO and inicio and resposta else None)
    # Vencida: passou do prazo; sem prazo, vale o limite de dias em aberto.
    if a["aberto"] and prazo:
        a["vencida"] = prazo < hoje
    else:
        a["vencida"] = bool(a["aberto"] and a["dias_aberto"] is not None and a["dias_aberto"] > alerta)
    a["acao_hoje"] = bool(a["aberto"] and proxima and proxima <= hoje)
    a["no_prazo"] = (resposta <= prazo) if (a["status"] == CONCLUIDO and prazo and resposta) else None
    return a


def carregar(con, hoje=None):
    """Todos os atendimentos, já com os campos calculados."""
    hoje = hoje or date.today()
    feriados, alerta = ler_feriados(con), ler_alerta(con)
    linhas = con.execute("SELECT * FROM atendimentos ORDER BY data DESC, id DESC").fetchall()
    return [enriquecer(l, hoje, feriados, alerta) for l in linhas]


def indicadores(ats, hoje):
    """Os números do painel (equivalente à aba Resumo)."""
    abertos = [a for a in ats if a["aberto"]]
    concl = [a for a in ats if a["status"] == CONCLUIDO]
    tempos = [a["tempo_resolucao"] for a in concl if a["tempo_resolucao"] is not None]
    com_prazo = [a for a in concl if a["no_prazo"] is not None]
    semana = hoje - timedelta(days=7)

    def prazo_de(a):
        return para_data(a["prazo"])

    return [
        ("Em aberto", len(abertos), ""),
        ("Vencidas", sum(1 for a in abertos if a["vencida"]), "alerta"),
        ("Vencem hoje", sum(1 for a in abertos if prazo_de(a) == hoje), ""),
        ("Vencem em até 3 dias", sum(
            1 for a in abertos if prazo_de(a) and hoje < prazo_de(a) <= hoje + timedelta(days=3)), ""),
        ("Ações para hoje ou atrasadas", sum(1 for a in abertos if a["acao_hoje"]), "acao"),
        ("Abertas com prioridade Alta", sum(1 for a in abertos if a["prioridade"] == "Alta"), ""),
        ("Atendimentos hoje", sum(1 for a in ats if para_data(a["data"]) == hoje), ""),
        ("Últimos 7 dias", sum(
            1 for a in ats if para_data(a["data"]) and semana < para_data(a["data"]) <= hoje), ""),
        ("Concluídos", len(concl), ""),
        ("Tempo médio de resolução (dias úteis)",
         ("%.1f" % (sum(tempos) / len(tempos))).replace(".", ",") if tempos else "-", ""),
        ("Concluído dentro do prazo",
         "%d%%" % round(100 * sum(1 for a in com_prazo if a["no_prazo"]) / len(com_prazo)) if com_prazo else "-", ""),
        ("Total registrado", len(ats), ""),
    ]


def contar_por(ats, campo, opcoes):
    """[(opção, total, em aberto)] para os quadros 'por status', 'por produto' etc."""
    saida = []
    extras = sorted(set(a[campo] for a in ats if a[campo] and a[campo] not in opcoes))
    for op in list(opcoes) + extras:
        grupo = [a for a in ats if a[campo] == op]
        saida.append((op, len(grupo), sum(1 for a in grupo if a["aberto"])))
    return saida


def validar(form, con):
    """Confere um formulário. Devolve (dados limpos, lista de erros)."""
    dados = dict((n, (form.get(n) or "").strip()) for n in NOMES)
    erros = []
    if not para_data(dados["data"]):
        erros.append("Informe a data do atendimento.")
    if not dados["cliente"]:
        erros.append("Informe o cliente.")
    dados["cpf"], erro_cpf = validar_cpf(dados["cpf"])
    if erro_cpf:
        erros.append(erro_cpf)
    for nome in ("data_proxima", "prazo", "data_resposta"):
        if dados[nome] and not para_data(dados[nome]):
            erros.append("Data inválida em: %s." % nome)
    for nome, _, tipo in CAMPOS:
        if tipo.startswith("lista:") and dados[nome]:
            if dados[nome] not in ler_lista(con, tipo.split(":")[1]):
                erros.append("Opção inválida em: %s." % nome)
    # Concluir sem data de resposta: assume hoje, em vez de deixar furo no histórico.
    if dados["status"] == CONCLUIDO and not dados["data_resposta"]:
        dados["data_resposta"] = date.today().isoformat()
    inicio, resposta = para_data(dados["data"]), para_data(dados["data_resposta"])
    if inicio and resposta and resposta < inicio:
        erros.append("A data da resposta não pode ser anterior à data do atendimento.")
    return dados, erros


# ---------------------------------------------------------------------------
# 3. PÁGINAS HTML
# ---------------------------------------------------------------------------

e = html.escape  # tudo que vem do banco passa por aqui antes de ir para a tela

CSS = """
:root{--azul:#1f3864;--borda:#d9dee7;--fundo:#f4f6f9;--amarelo:#fff2cc;--vermelho:#f8cbad;
--verde:#e2efda;--laranja:#ed7d31;--texto:#1c2430;--suave:#5c6675}
*{box-sizing:border-box}body{margin:0;font-family:Arial,Helvetica,sans-serif;font-size:14px;
color:var(--texto);background:var(--fundo)}
header{background:var(--azul);color:#fff;padding:10px 20px;display:flex;gap:24px;align-items:center;flex-wrap:wrap}
header b{font-size:16px}header a{color:#dfe7f5;text-decoration:none;padding:4px 2px;border-bottom:2px solid transparent}
header a.ativo,header a:hover{color:#fff;border-color:#fff}
main{padding:20px;max-width:1500px;margin:0 auto}h1{font-size:20px;margin:0 0 14px;color:var(--azul)}
h2{font-size:15px;margin:22px 0 8px;color:var(--azul)}
.cartoes{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:10px}
.cartao{background:#fff;border:1px solid var(--borda);border-radius:6px;padding:12px;border-left:5px solid var(--azul)}
.cartao.alerta{border-left-color:#c00000}.cartao.acao{border-left-color:var(--laranja)}
.cartao .n{font-size:26px;font-weight:bold}.cartao .r{color:var(--suave);font-size:12px;margin-top:2px}
.tabela{overflow-x:auto;background:#fff;border:1px solid var(--borda);border-radius:6px}
table{border-collapse:collapse;width:100%}th{background:var(--azul);color:#fff;text-align:left;padding:7px 8px;
font-size:12px;white-space:nowrap}td{padding:7px 8px;border-top:1px solid var(--borda);vertical-align:top}
tr.aberto td{background:var(--amarelo)}tr.vencida td{background:var(--vermelho)}
tr.concluido td{background:var(--verde);color:var(--suave)}
td.acao{background:var(--laranja)!important;color:#fff;font-weight:bold}.alta{color:#c00000;font-weight:bold}
.centro{text-align:center}.vazio{padding:18px;color:var(--suave)}
.grade{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}
form.ficha{background:#fff;border:1px solid var(--borda);border-radius:6px;padding:16px;
display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px}
label{display:block;font-size:12px;color:var(--suave);margin-bottom:3px}
input,select,textarea{width:100%;padding:7px;border:1px solid #b8c0cc;border-radius:4px;font:inherit;background:#fff}
textarea{min-height:70px;resize:vertical}.largo{grid-column:1/-1}
.botao,button{display:inline-block;background:var(--azul);color:#fff;border:0;border-radius:4px;padding:8px 14px;
font:inherit;cursor:pointer;text-decoration:none}.botao.sec,button.sec{background:#fff;color:var(--azul);
border:1px solid var(--azul)}button.perigo{background:#c00000}button.mini{padding:3px 8px;font-size:12px}
.barra{display:flex;gap:8px;flex-wrap:wrap;align-items:end;margin-bottom:12px}.barra>div{min-width:160px}
.erros{background:#fde2e1;border:1px solid #c00000;border-radius:6px;padding:10px 14px;margin-bottom:12px}
.aviso{background:#fff;border:1px solid var(--borda);border-left:5px solid #c00000;border-radius:6px;
padding:10px 14px;color:var(--suave);font-size:12px;margin-top:24px}
.acoes{display:flex;gap:6px;white-space:nowrap}.acoes form{margin:0}
"""


def pagina(titulo, corpo, ativo=""):
    menu = [("/", "Painel"), ("/registro", "Registro"), ("/novo", "Novo atendimento"), ("/listas", "Listas")]
    links = "".join('<a href="%s" class="%s">%s</a>' % (u, "ativo" if u == ativo else "", t) for u, t in menu)
    return (
        '<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>%s - Atendimentos</title><style>%s</style></head><body>"
        "<header><b>Controle de Atendimentos</b>%s</header><main>%s"
        '<div class="aviso">Sigilo bancário e LGPD: este programa guarda dados de clientes no arquivo '
        "<b>%s</b>. Mantenha essa pasta apenas em local autorizado pela empresa e não copie o arquivo "
        "para e-mail pessoal, celular ou ferramentas externas.</div></main></body></html>"
    ) % (e(titulo), CSS, links, corpo, e(CAMINHO_DB))


def token_campo():
    return '<input type="hidden" name="token" value="%s">' % TOKEN


def classe_linha(a):
    if a["status"] == CONCLUIDO:
        return "concluido"
    if a["vencida"]:
        return "vencida"
    return "aberto" if a["aberto"] else ""


def tabela_atendimentos(ats, completa=True):
    """Tabela usada no Painel (resumida) e no Registro (completa)."""
    if not ats:
        return '<div class="tabela"><div class="vazio">Nenhum atendimento aqui.</div></div>'
    cab = ["Data", "Cliente", "Demanda", "Pendência", "Próxima ação", "Data próx. ação",
           "Status", "Prioridade", "Prazo", "Dias úteis"]
    if completa:
        cab[2:2] = ["CPF", "Contrato", "Produto"]
    linhas = []
    for a in ats:
        dias = a["dias_aberto"] if a["aberto"] else a["tempo_resolucao"]
        cel = [
            "<td>%s</td>" % br(a["data"]),
            '<td><a href="/editar?id=%d">%s</a></td>' % (a["id"], e(a["cliente"])),
            "<td>%s</td>" % e(a["demanda"]),
            "<td>%s</td>" % e(a["pendencia"]),
            "<td>%s</td>" % e(a["proxima_acao"]),
            '<td class="%s">%s</td>' % ("acao" if a["acao_hoje"] else "", br(a["data_proxima"])),
            "<td>%s</td>" % e(a["status"]),
            '<td class="%s">%s</td>' % ("alta" if a["prioridade"] == "Alta" and a["aberto"] else "", e(a["prioridade"])),
            "<td>%s</td>" % br(a["prazo"]),
            '<td class="centro">%s</td>' % ("" if dias is None else dias),
        ]
        if completa:
            cel[2:2] = ["<td>%s</td>" % e(a["cpf"]), "<td>%s</td>" % e(a["contrato"]), "<td>%s</td>" % e(a["produto"])]
            botoes = '<a class="botao sec mini" style="padding:3px 8px;font-size:12px" href="/editar?id=%d">Editar</a>' % a["id"]
            if a["aberto"]:
                botoes += ('<form method="post" action="/concluir">%s<input type="hidden" name="id" value="%d">'
                           '<button class="mini">Concluir</button></form>') % (token_campo(), a["id"])
            cel.append('<td><div class="acoes">%s</div></td>' % botoes)
        linhas.append('<tr class="%s">%s</tr>' % (classe_linha(a), "".join(cel)))
    if completa:
        cab.append("")
    return '<div class="tabela"><table><tr>%s</tr>%s</table></div>' % (
        "".join("<th>%s</th>" % c for c in cab), "".join(linhas))


def quadro(titulo, linhas):
    """Quadro pequeno 'por status', 'por produto' etc."""
    total = sum(n for _, n, _ in linhas) or 1
    corpo = "".join(
        '<tr><td>%s</td><td class="centro">%d</td><td class="centro">%d%%</td><td class="centro">%d</td></tr>'
        % (e(nome), n, round(100 * n / total), ab) for nome, n, ab in linhas)
    if not linhas:
        corpo = '<tr><td colspan="4" class="vazio">Lista vazia. Preencha em Listas.</td></tr>'
    return ('<div><h2>%s</h2><div class="tabela"><table><tr><th>%s</th><th>Qtde</th><th>%%</th>'
            "<th>Em aberto</th></tr>%s</table></div></div>") % (e(titulo), e(titulo.replace("Por ", "").capitalize()), corpo)


def pagina_painel(con):
    hoje = date.today()
    ats = carregar(con, hoje)
    cartoes = "".join(
        '<div class="cartao %s"><div class="n">%s</div><div class="r">%s</div></div>' % (cls, valor, e(rot))
        for rot, valor, cls in indicadores(ats, hoje))
    # Prioridades: as 15 pendências abertas há mais tempo.
    abertos = sorted((a for a in ats if a["aberto"]), key=lambda a: (-(a["dias_aberto"] or 0), a["id"]))[:15]
    quadros = "".join([
        quadro("Por status", contar_por(ats, "status", ler_lista(con, "status"))),
        quadro("Por produto", contar_por(ats, "produto", ler_lista(con, "produto"))),
        quadro("Por indicador", contar_por(ats, "indicador", ler_lista(con, "indicador"))),
        quadro("Por canal", contar_por(ats, "canal", ler_lista(con, "canal"))),
        quadro("Por tipo de pendência", contar_por(ats, "tipo", ler_lista(con, "tipo"))),
    ])
    meses = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]
    por_mes = ""
    for m in range(1, 13):
        chave = "%d-%02d" % (hoje.year, m)
        entradas = sum(1 for a in ats if a["data"].startswith(chave))
        concl = sum(1 for a in ats if a["status"] == CONCLUIDO and a["data_resposta"].startswith(chave))
        por_mes += '<tr><td>%s</td><td class="centro">%d</td><td class="centro">%d</td></tr>' % (meses[m - 1], entradas, concl)
    quadros += ('<div><h2>Por mês (%d)</h2><div class="tabela"><table><tr><th>Mês</th><th>Entradas</th>'
                "<th>Concluídos</th></tr>%s</table></div></div>") % (hoje.year, por_mes)
    corpo = ("<h1>Painel - %s</h1><div class='cartoes'>%s</div><h2>Prioridades: pendências abertas há mais tempo</h2>%s"
             "<div class='grade'>%s</div>") % (hoje.strftime("%d/%m/%Y"), cartoes, tabela_atendimentos(abertos, False), quadros)
    return pagina("Painel", corpo, "/")


def pagina_registro(con, q):
    busca = (q.get("q") or "").strip()
    status = q.get("status") or ""
    ats = carregar(con)
    if status == "abertos":
        ats = [a for a in ats if a["aberto"]]
    elif status == "vencidas":
        ats = [a for a in ats if a["vencida"]]
    elif status:
        ats = [a for a in ats if a["status"] == status]
    if busca:
        termo, digitos = busca.lower(), so_digitos(busca)

        def bate(a):
            texto = " ".join(a[n] for n in ("cliente", "cpf", "contrato", "email", "demanda", "telefone",
                                              "pendencia", "feito", "proxima_acao")).lower()
            # CPF/telefone: acha tanto com pontuação quanto só com os dígitos.
            sem_letras = not any(ch.isalpha() for ch in busca)
            if sem_letras and len(digitos) >= 3 and any(
                    digitos in so_digitos(a[n]) for n in ("cpf", "telefone", "contrato", "demanda")):
                return True
            return termo in texto
        ats = [a for a in ats if bate(a)]
    opcoes = [("", "Todos"), ("abertos", "Em aberto"), ("vencidas", "Vencidas")] + [(s, s) for s in ler_lista(con, "status")]
    sel = "".join('<option value="%s"%s>%s</option>' % (e(v), " selected" if v == status else "", e(t)) for v, t in opcoes)
    barra = ('<form class="barra" method="get" action="/registro"><div style="flex:1"><label>Buscar (nome, CPF, contrato, '
             'demanda, e-mail, telefone, texto)</label><input name="q" value="%s" autofocus></div><div><label>Mostrar</label>'
             '<select name="status">%s</select></div><button>Filtrar</button><a class="botao sec" href="/registro">Limpar</a>'
             '<a class="botao sec" href="/exportar.csv">Exportar para Excel (CSV)</a></form>') % (e(busca), sel)
    corpo = "<h1>Registro (%d)</h1>%s%s" % (len(ats), barra, tabela_atendimentos(ats, True))
    return pagina("Registro", corpo, "/registro")


def pagina_ficha(con, dados, erros=(), id_=None):
    """Formulário de novo atendimento e de edição."""
    campos = []
    for nome, rotulo, tipo in CAMPOS:
        valor = dados.get(nome, "")
        if tipo == "data":
            campo = '<input type="date" name="%s" value="%s">' % (nome, e(valor))
        elif tipo == "longo":
            campo = '<textarea name="%s">%s</textarea>' % (nome, e(valor))
        elif tipo.startswith("lista:"):
            ops = ler_lista(con, tipo.split(":")[1])
            if valor and valor not in ops:
                ops = ops + [valor]
            campo = '<select name="%s"><option value=""></option>%s</select>' % (nome, "".join(
                '<option%s>%s</option>' % (" selected" if o == valor else "", e(o)) for o in ops))
        else:
            campo = '<input name="%s" value="%s">' % (nome, e(valor))
        campos.append('<div class="%s"><label>%s</label>%s</div>' % ("largo" if tipo == "longo" else "", e(rotulo), campo))
    bloco_erros = '<div class="erros">%s</div>' % "<br>".join(e(x) for x in erros) if erros else ""
    oculto = '<input type="hidden" name="id" value="%d">' % id_ if id_ else ""
    excluir = ""
    if id_:
        excluir = ('<form method="post" action="/excluir" style="margin-top:12px" onsubmit="return confirm('
                   "'Excluir este atendimento? Não dá para desfazer.')\">%s<input type=\"hidden\" name=\"id\" "
                   'value="%d"><button class="perigo">Excluir atendimento</button></form>') % (token_campo(), id_)
    corpo = ('<h1>%s</h1>%s<form class="ficha" method="post" action="/salvar">%s%s%s<div class="largo">'
             '<button>Salvar</button> <a class="botao sec" href="/registro">Cancelar</a></div></form>%s') % (
        "Editar atendimento" if id_ else "Novo atendimento", bloco_erros, token_campo(), oculto, "".join(campos), excluir)
    return pagina("Atendimento", corpo, "" if id_ else "/novo")


def pagina_listas(con, mensagem=""):
    blocos = []
    for tipo in ("indicador", "produto", "canal", "tipo", "prioridade", "status"):
        blocos.append('<div><label>%s (uma opção por linha)</label><textarea name="lista_%s" style="min-height:150px">%s</textarea></div>'
                      % (ROTULO_LISTA[tipo], tipo, e("\n".join(ler_lista(con, tipo)))))
    feriados = "\n".join(br(d) for d in sorted(ler_feriados(con)))
    blocos.append('<div><label>Feriados (dd/mm/aaaa, um por linha)</label><textarea name="feriados" style="min-height:150px">%s</textarea></div>' % e(feriados))
    blocos.append('<div><label>Alerta sem prazo: dias úteis em aberto acima de</label><input type="number" min="0" name="alerta" value="%d"></div>' % ler_alerta(con))
    msg = '<div class="erros" style="background:#e2efda;border-color:#548235">%s</div>' % e(mensagem) if mensagem else ""
    corpo = ('<h1>Listas e configurações</h1>%s<form class="ficha" method="post" action="/listas">%s%s<div class="largo">'
             "<button>Salvar listas</button></div></form><p style='color:#5c6675'>O status “%s” é fixo: as regras de prazo "
             "dependem dele. A prioridade “Alta” é a que aparece em destaque.</p>") % (msg, token_campo(), "".join(blocos), CONCLUIDO)
    return pagina("Listas", corpo, "/listas")


def gerar_csv(con):
    """CSV com ; e BOM: abre direto no Excel em português, com acentos certos."""
    saida = io.StringIO()
    w = csv.writer(saida, delimiter=";", lineterminator="\r\n")
    w.writerow([c[1] for c in CAMPOS] + ["Dias úteis em aberto", "Tempo de resolução (dias úteis)"])
    for a in reversed(carregar(con)):
        linha = []
        for nome, _, tipo in CAMPOS:
            valor = br(a[nome]) if tipo == "data" else a[nome]
            # Texto começando com = + - @ poderia ser lido pelo Excel como fórmula.
            if tipo != "data" and valor[:1] in ("=", "+", "-", "@"):
                valor = "'" + valor
            linha.append(valor)
        linha += ["" if a["dias_aberto"] is None else a["dias_aberto"],
                  "" if a["tempo_resolucao"] is None else a["tempo_resolucao"]]
        w.writerow(linha)
    return ("﻿" + saida.getvalue()).encode("utf-8")


# ---------------------------------------------------------------------------
# 4. SERVIDOR WEB LOCAL
# ---------------------------------------------------------------------------

class Servidor(BaseHTTPRequestHandler):
    """Atende só o próprio computador. Duas proteções:

    - Host: recusa pedidos que não sejam para 127.0.0.1/localhost.
    - Token: todo formulário leva um código sorteado a cada execução; sem ele,
      um site aberto em outra aba não consegue gravar nada aqui.
    """
    server_version = "Atendimentos"

    def log_message(self, formato, *args):  # não imprime endereços (podem ter busca por CPF)
        pass

    def host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def responder(self, corpo, tipo="text/html; charset=utf-8", status=200, extras=()):
        if isinstance(corpo, str):
            corpo = corpo.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; "
                                                    "script-src 'unsafe-inline'; form-action 'self'; base-uri 'none'")
        for k, v in extras:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(corpo)

    def redirecionar(self, destino):
        self.send_response(303)
        self.send_header("Location", destino)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if not self.host_ok():
            return self.responder("Acesso negado.", status=403)
        url = urlparse(self.path)
        q = dict((k, v[0]) for k, v in parse_qs(url.query).items())
        con = conectar()
        try:
            if url.path == "/":
                self.responder(pagina_painel(con))
            elif url.path == "/registro":
                self.responder(pagina_registro(con, q))
            elif url.path == "/novo":
                self.responder(pagina_ficha(con, {"data": date.today().isoformat()}))
            elif url.path == "/editar":
                linha = con.execute("SELECT * FROM atendimentos WHERE id=?", (q.get("id", ""),)).fetchone()
                if not linha:
                    return self.responder(pagina("Não encontrado", "<h1>Atendimento não encontrado.</h1>"), status=404)
                self.responder(pagina_ficha(con, dict(linha), id_=linha["id"]))
            elif url.path == "/listas":
                self.responder(pagina_listas(con, "Listas salvas." if q.get("ok") else ""))
            elif url.path == "/exportar.csv":
                nome = "atendimentos-%s.csv" % date.today().isoformat()
                self.responder(gerar_csv(con), "text/csv; charset=utf-8",
                               extras=[("Content-Disposition", 'attachment; filename="%s"' % nome)])
            else:
                self.responder(pagina("Não encontrado", "<h1>Página não encontrada.</h1>"), status=404)
        finally:
            con.close()

    def do_POST(self):
        if not self.host_ok():
            return self.responder("Acesso negado.", status=403)
        tamanho = int(self.headers.get("Content-Length") or 0)
        if tamanho > 2000000:
            return self.responder("Formulário grande demais.", status=413)
        bruto = self.rfile.read(tamanho).decode("utf-8", "replace")
        form = dict((k, v[0]) for k, v in parse_qs(bruto, keep_blank_values=True).items())
        if not secrets.compare_digest(form.get("token", ""), TOKEN):
            return self.responder("Formulário expirado. Volte, recarregue a página e tente de novo.", status=403)
        caminho = urlparse(self.path).path
        con = conectar()
        try:
            agora = datetime.now().isoformat(timespec="seconds")
            id_ = int(form["id"]) if (form.get("id") or "").isdigit() else None
            if caminho == "/salvar":
                dados, erros = validar(form, con)
                if erros:
                    return self.responder(pagina_ficha(con, dados, erros, id_), status=400)
                if id_:
                    con.execute("UPDATE atendimentos SET %s, atualizado_em=? WHERE id=?" % ", ".join(n + "=?" for n in NOMES),
                                [dados[n] for n in NOMES] + [agora, id_])
                else:
                    con.execute("INSERT INTO atendimentos (%s, criado_em, atualizado_em) VALUES (%s)" % (
                        ", ".join(NOMES), ", ".join("?" * (len(NOMES) + 2))), [dados[n] for n in NOMES] + [agora, agora])
                con.commit()
                self.redirecionar("/registro")
            elif caminho == "/concluir" and id_:
                # Atalho: marca como concluído hoje, sem abrir a ficha.
                con.execute("UPDATE atendimentos SET status=?, data_resposta=CASE WHEN data_resposta='' THEN ? "
                            "ELSE data_resposta END, atualizado_em=? WHERE id=?",
                            (CONCLUIDO, date.today().isoformat(), agora, id_))
                con.commit()
                self.redirecionar("/registro")
            elif caminho == "/excluir" and id_:
                con.execute("DELETE FROM atendimentos WHERE id=?", (id_,))
                con.commit()
                self.redirecionar("/registro")
            elif caminho == "/listas":
                for tipo in LISTAS_PADRAO:
                    valores = []
                    for v in (form.get("lista_" + tipo) or "").splitlines():
                        v = v.strip()
                        if v and v not in valores:
                            valores.append(v)
                    if tipo == "status" and CONCLUIDO not in valores:
                        valores.append(CONCLUIDO)
                    con.execute("DELETE FROM listas WHERE tipo=?", (tipo,))
                    con.executemany("INSERT INTO listas VALUES (?,?,?)", [(tipo, v, i) for i, v in enumerate(valores)])
                con.execute("DELETE FROM feriados")
                for linha in (form.get("feriados") or "").splitlines():
                    try:
                        d = datetime.strptime(linha.strip(), "%d/%m/%Y").date()
                        con.execute("INSERT OR IGNORE INTO feriados VALUES (?)", (d.isoformat(),))
                    except ValueError:
                        pass  # linha vazia ou fora do formato: ignora
                alerta = form.get("alerta") or "3"
                con.execute("INSERT OR REPLACE INTO config VALUES ('alerta_dias', ?)",
                            (alerta if alerta.isdigit() else "3",))
                con.commit()
                self.redirecionar("/listas?" + urlencode({"ok": 1}))
            else:
                self.responder("Ação desconhecida.", status=404)
        finally:
            con.close()


def main():
    ap = argparse.ArgumentParser(description="Controle de Atendimentos (local)")
    ap.add_argument("--porta", type=int, default=8765)
    ap.add_argument("--sem-navegador", action="store_true", help="não abrir o navegador automaticamente")
    args = ap.parse_args()
    fazer_backup()
    preparar_banco()
    try:
        # 127.0.0.1 = só este computador. Nunca troque por 0.0.0.0.
        servidor = HTTPServer(("127.0.0.1", args.porta), Servidor)
    except OSError:
        sys.exit("A porta %d já está em uso. O programa já está aberto? Tente: --porta %d" % (args.porta, args.porta + 1))
    endereco = "http://127.0.0.1:%d/" % args.porta
    print("Controle de Atendimentos rodando em %s" % endereco)
    print("Dados em: %s" % CAMINHO_DB)
    print("Para encerrar: Ctrl + C nesta janela.")
    if not args.sem_navegador:
        webbrowser.open(endereco)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrado.")


if __name__ == "__main__":
    main()
