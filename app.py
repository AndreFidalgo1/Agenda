import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from groq import Groq
import streamlit as st
from streamlit_calendar import calendar
from calendar_helper import apagar_evento, criar_evento, listar_proximos_eventos

st.set_page_config(page_title="J.A.R.V.I.S. Protocol", page_icon="🤖", layout="wide")
logger = logging.getLogger("jarvis")

# --- BARREIRA DE SEGURANÇA ---
if "autenticado" not in st.session_state:
    st.session_state.autenticado = False

if not st.session_state.autenticado:
    st.markdown("<h1 style='text-align: center; color: #00BFFF; margin-top: 100px;'>🤖 J.A.R.V.I.S. Protocol</h1>", unsafe_allow_html=True)
    st.markdown("<p style='text-align: center;'>Acesso restrito. Insira a palavra-passe de autorização.</p>", unsafe_allow_html=True)
    
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        pwd = st.text_input("Password", type="password", label_visibility="collapsed")
        if st.button("Desbloquear Sistema", use_container_width=True):
            if pwd == st.secrets["APP_PASSWORD"]:
                st.session_state.autenticado = True
                st.rerun()
            else:
                st.error("Acesso Negado.")
    st.stop() # Bloqueia a execução do resto do código se não estiver autenticado

# Inicializa o cliente do Groq
cliente_groq = Groq(api_key=st.secrets["GROQ_API_KEY"])
MODELO = "openai/gpt-oss-20b" # Equivalente ao 8B rápido no Groq

# ────────────────────────────── CONFIGURAÇÃO BASE ──────────────────────────────
FUSO = ZoneInfo("Europe/Lisbon")
LIMITE_HISTORICO = 6
CAMPOS = ("titulo", "data", "hora_inicio", "hora_fim", "descricao", "recorrencia")
OBRIGATORIOS = ("titulo", "data", "hora_inicio")
NOMES_CAMPOS = {"titulo": "o título", "data": "a data", "hora_inicio": "a hora de início",
                "hora_fim": "a hora de fim", "recorrencia": "a recorrência"}
DIAS_SEMANA = ["segunda-feira", "terça-feira", "quarta-feira",
               "quinta-feira", "sexta-feira", "sábado", "domingo"]
IGNORAR = ("aniversário", "birthday")
PALETA = ["#4a6b82", "#6b5b7b", "#5e7b5e", "#8c6b5d", "#4d6a79", "#7b615c"]
TEMAS = {
    "claro": dict(app="#ffffff", texto="#374151", linhas="#e5e7eb", botao="#f3f4f6",
                  hover="#e5e7eb", hoje="rgba(0,0,0,0.03)"),
    "escuro": dict(app="#1e1e1e", texto="#d1d5db", linhas="#2b2b2b", botao="#2d2d2d",
                   hover="#404040", hoje="rgba(255,255,255,0.015)"),
}

_campo_texto = {"type": ["string", "null"]}
SCHEMA = {
    "type": "object",
    "properties": {
        "detalhes": {"type": "object",
                     "properties": {c: _campo_texto for c in CAMPOS},
                     "required": list(CAMPOS)},
        "acao": {"type": "string",
                 "enum": ["recolher_dados", "pedir_confirmacao", "confirmar", "cancelar", "conversa"]},
        "fala_jarvis": {"type": "string"},
    },
    "required": ["detalhes", "acao", "fala_jarvis"],
}

# --- CONFIGURAÇÃO DO BRIEFING AVANÇADO ---
JANELA_INICIO, JANELA_FIM = 8, 20          
PAUSA_MIN = timedelta(minutes=45)          
SEM_PAUSA = timedelta(minutes=15)          
HORA_VESPERA = 20                          

RE_AVALIACAO = re.compile(
    r"\b(teste|mini-?teste|exame|frequ[eê]ncia|avalia[cç][aã]o|entrega|prazo|"
    r"apresenta[cç][aã]o|defesa|projeto|quiz)\b", re.I)
RE_AULA = re.compile(
    r"^\s*aula\b|\b(lab|laborat[oó]rio|te[oó]rica|pr[aá]tica|tp|pl)\b", re.I)

SCHEMA_BRIEFING = {
    "type": "object",
    "properties": {
        "resumo":   {"type": "string"},
        "analise":  {"type": "string"},
        "conselho": {"type": "string"},
    },
    "required": ["resumo", "analise", "conselho"],
}

ICONE = {"aula": "🎓", "avaliacao": "🔴", "compromisso": "📌"}


class ErroIA(RuntimeError):
    pass

@dataclass
class Resposta:
    fala: str
    estado: dict
    acao: str
    gravar: bool = False

@dataclass
class Ev:
    titulo: str
    ini: datetime
    fim: datetime
    dia_todo: bool
    tipo: str            # "aula" | "avaliacao" | "compromisso"


# ────────────────────────────── UTILITÁRIOS BASE ──────────────────────────────
def agora() -> datetime:
    return datetime.now(FUSO)

def estado_vazio() -> dict:
    return dict.fromkeys(CAMPOS)

def inicio_evento(e: dict) -> str:
    return e["start"].get("dateTime", e["start"].get("date", ""))

@st.cache_data(ttl=60, show_spinner=False)
def obter_eventos(n: int = 50) -> list[dict]:
    return listar_proximos_eventos(n)

def eventos_visiveis() -> list[dict]:
    return [e for e in obter_eventos()
            if not any(t in e.get("summary", "").lower() for t in IGNORAR)]

def _data_valida(v: str, hoje: datetime) -> bool:
    try:
        return datetime.strptime(v, "%Y-%m-%d").date() >= hoje.date()
    except ValueError:
        return False

def _hora_valida(v: str) -> bool:
    try:
        datetime.strptime(v, "%H:%M")
        return True
    except ValueError:
        return False

def sanitizar(detalhes: dict, hoje: datetime) -> tuple[dict, list[str]]:
    limpo, rejeitados = {}, []
    for campo in CAMPOS:
        valor = detalhes.get(campo)
        if isinstance(valor, str):
            valor = valor.strip()
        if valor in (None, "", "null", "None"):
            continue
        valido = {"data": lambda v: _data_valida(v, hoje),
                  "hora_inicio": _hora_valida,
                  "hora_fim": _hora_valida}.get(campo, lambda v: True)(valor)
        if valido:
            limpo[campo] = valor
        else:
            rejeitados.append(NOMES_CAMPOS.get(campo, campo))
    return limpo, rejeitados

def resumo_evento(e: dict) -> str:
    dia = datetime.strptime(e["data"], "%Y-%m-%d")
    hora = e["hora_inicio"] + (f"–{e['hora_fim']}" if e.get("hora_fim") else "")
    return f"{e['titulo']}, {DIAS_SEMANA[dia.weekday()]} {dia:%d/%m/%Y}, às {hora}"


# ────────────────────────────── MOTOR DO LLM DE AGENDAMENTO ──────────────────────────────
def gerar_calendario(hoje: datetime, n_dias: int = 21) -> str:
    segunda = (hoje - timedelta(days=hoje.weekday())).date()
    posicao = {0: "HOJE", 1: "AMANHÃ", 2: "DEPOIS DE AMANHÃ"}
    linhas = []
    for i in range(n_dias):
        dia = hoje + timedelta(days=i)
        semanas = (dia.date() - segunda).days // 7
        if i in posicao:
            rel = posicao[i]
        else:
            rel = {0: "esta semana", 1: "próxima semana"}.get(semanas, f"daqui a {semanas} semanas")
        linhas.append(f"{dia:%Y-%m-%d} | {DIAS_SEMANA[dia.weekday()]} | {rel}")
    return "\n".join(linhas)

def construir_prompt(hoje: datetime, estado: dict) -> str:
    quinta = (hoje - timedelta(days=hoje.weekday()) + timedelta(days=10)).date().isoformat()

    def ex(**kw) -> str:
        det = {**estado_vazio(), "titulo": "Teste de MC", "data": quinta, **kw["det"]}
        return json.dumps({"detalhes": det, "acao": kw["acao"], "fala_jarvis": kw["fala"]},
                          ensure_ascii=False)

    exemplos = "\n\n".join([
        'Senhor: "adiciona um evento na próxima quinta-feira, teste de mc"\n' + ex(
            det={}, acao="recolher_dados",
            fala="Com certeza, Senhor. A que horas devo agendar o Teste de MC?"),
        'Senhor: "às 14h"\n' + ex(
            det={"hora_inicio": "14:00"}, acao="pedir_confirmacao",
            fala="Perfeito. Teste de MC às 14:00. Devo gravar, Senhor?"),
        'Senhor: "sim, grava"\n' + ex(
            det={"hora_inicio": "14:00"}, acao="confirmar",
            fala="Imediatamente, Senhor. A gravar."),
    ])

    return f"""És o J.A.R.V.I.S., assistente pessoal de agenda. Tratas o utilizador por "Senhor".
Tom: britânico, formal, elegante e conciso (máximo 2 frases). Português de Portugal.

# CONTEXTO TEMPORAL
Agora: {DIAS_SEMANA[hoje.weekday()]}, {hoje:%Y-%m-%d}, {hoje:%H:%M}.
Calendário de referência (data | dia | posição):
{gerar_calendario(hoje)}

# ESTADO ATUAL DO EVENTO EM CONSTRUÇÃO
{json.dumps(estado, ensure_ascii=False)}

# TAREFA
Extrai do último pedido do Senhor os dados de um evento de calendário.
Campos obrigatórios: titulo, data, hora_inicio. Os restantes são opcionais.

# REGRAS
1. Parte do ESTADO ATUAL e atualiza-o apenas com o que o Senhor disser agora. Nunca apagues campos já preenchidos, exceto se ele os corrigir.
2. Se o Senhor pedir um evento claramente NOVO, ignora o estado anterior e começa de raiz.
3. Datas: usa SEMPRE o calendário acima. Nunca calcules datas de cabeça.
4. Horas: formato 24h "HH:MM". "às 14h" → "14:00". "às 8 da noite" → "20:00". Se disser só "às 8" sem indicar período, pergunta se é manhã ou noite.
5. Nunca inventes hora, data ou título. Se faltar algo, deixa null e pergunta APENAS esse campo.
6. "titulo": curto, com maiúscula inicial, sem a data nem a hora.
7. "recorrencia": só se o Senhor pedir repetição (formato RRULE).
8. Escolhe "acao":
   - "recolher_dados": falta titulo, data ou hora_inicio → pergunta o que falta.
   - "pedir_confirmacao": os 3 campos obrigatórios estão preenchidos → resume e pergunta se deve gravar.
   - "confirmar": o Senhor acabou de aceitar e o estado está completo.
   - "cancelar": o Senhor desistiu → limpa os detalhes (tudo null).
   - "conversa": o pedido não tem relação com agendamentos → responde brevemente em personagem.

# EXEMPLOS
{exemplos}

Responde apenas com o JSON.
"""

def conversar(texto: str, historico: list[dict], estado: dict | None,
              pendente: dict | None) -> Resposta:
    hoje = agora()
    estado = estado or estado_vazio()
    mensagens = [{"role": "system", "content": construir_prompt(hoje, estado)},
                 *historico[-LIMITE_HISTORICO:],
                 {"role": "user", "content": texto}]
    try:
        r = cliente_groq.chat.completions.create(
            model=MODELO,
            messages=mensagens,
            response_format={"type": "json_object"},
            temperature=0.1
        )
        dados = json.loads(r.choices[0].message.content)
        acao, fala, detalhes = dados["acao"], dados["fala_jarvis"], dados["detalhes"]
    except Exception as exc:
        logger.exception("Falha no modelo")
        raise ErroIA("Não consegui contactar os servidores da Groq.") from exc
    if acao == "cancelar":
        return Resposta(fala, estado_vazio(), acao)
    if acao == "conversa":
        return Resposta(fala, estado, acao)

    limpo, rejeitados = sanitizar(detalhes, hoje)
    novo = {**estado, **limpo}
    if novo["hora_fim"] and novo["hora_inicio"] and novo["hora_fim"] <= novo["hora_inicio"]:
        novo["hora_fim"] = None
        rejeitados.append(NOMES_CAMPOS["hora_fim"])
    em_falta = [c for c in OBRIGATORIOS if not novo.get(c)]

    if rejeitados:
        fala = (f"Peço desculpa, Senhor, mas não consegui validar {', '.join(rejeitados)}. "
                "Poderia indicá-lo novamente?")
        return Resposta(fala, novo, "recolher_dados")
    if em_falta:
        if acao != "recolher_dados":
            fala = f"Falta-me apenas {NOMES_CAMPOS[em_falta[0]]}, Senhor."
        return Resposta(fala, novo, "recolher_dados")

    if acao == "confirmar" and pendente is not None and novo == pendente:
        return Resposta(fala, novo, "confirmar", gravar=True)
    return Resposta(f"Perfeito, Senhor. {resumo_evento(novo)}. Devo gravar?",
                    novo, "pedir_confirmacao")


# ────────────────────────────── SISTEMA DE BRIEFING AVANÇADO ──────────────────────────────

def classificar(titulo: str, e: dict) -> str:
    if re.match(r"^\s*aula\b", titulo, re.I):     
        return "aula"
    if RE_AVALIACAO.search(titulo):
        return "avaliacao"
    if RE_AULA.search(titulo):
        return "aula"
    return "compromisso"

def _dt(s: str) -> datetime:
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d.astimezone().replace(tzinfo=None) if d.tzinfo else d

def _fim_evento(e: dict) -> str | None:
    f = e.get("end", {})
    return f.get("dateTime") or f.get("date")

def normalizar(e: dict) -> Ev:
    ini_s = inicio_evento(e)
    ini = _dt(ini_s)
    dia_todo = "T" not in ini_s
    fim_s = _fim_evento(e)
    fim = _dt(fim_s) if fim_s else None
    if dia_todo:
        fim = ini + timedelta(days=1)
    elif not fim or fim <= ini:
        fim = ini + timedelta(hours=1)

    titulo = e.get("summary", "Evento")
    tipo = classificar(titulo, e)
    return Ev(titulo, ini, fim, dia_todo, tipo)

def _hm(d: datetime) -> str:
    return d.strftime("%H:%M")

def _dur(td: timedelta) -> str:
    h, m = divmod(int(td.total_seconds() // 60), 60)
    return f"{h}h{m:02d}" if h and m else (f"{h}h" if h else f"{m}min")

def _dia_curto(d) -> str:
    return f"{DIAS_SEMANA[d.weekday()][:3]} {d:%d/%m}"

def analisar_dia(evs: list[Ev], dia, agora_: datetime) -> dict:
    do_dia = sorted((e for e in evs if not e.dia_todo and e.ini.date() == dia),
                    key=lambda e: e.ini)
    cursor = datetime.combine(dia, time(JANELA_INICIO))
    if dia == agora_.date():
        cursor = max(cursor, agora_)
    fim_util = datetime.combine(dia, time(JANELA_FIM))

    janelas = []
    for e in do_dia:
        if e.ini - cursor >= PAUSA_MIN:
            janelas.append((cursor, e.ini))
        cursor = max(cursor, e.fim)
    if fim_util - cursor >= PAUSA_MIN:
        janelas.append((cursor, fim_util))

    ocupado = sum((e.fim - e.ini for e in do_dia), timedelta())
    maior_seq, atual, prev_fim = timedelta(), timedelta(), None
    for e in do_dia:
        d = e.fim - e.ini
        atual = atual + d if prev_fim and e.ini - prev_fim < SEM_PAUSA else d
        maior_seq = max(maior_seq, atual)
        prev_fim = e.fim

    return {
        "eventos": do_dia,
        "dia_todo": [e for e in evs if e.dia_todo and e.ini.date() <= dia < e.fim.date()],
        "janelas": janelas,
        "ocupado": ocupado,
        "maior_seq": maior_seq,
    }

def selecionar_radar(evs, foco, max_itens=5):
    limite = foco + timedelta(days=7)
    futuros = sorted((e for e in evs
                      if foco < e.ini.date() <= limite and e.tipo != "aula"),
                     key=lambda e: e.ini)
    aval = [e for e in futuros if e.tipo == "avaliacao"]
    resto = [e for e in futuros if e.tipo != "avaliacao"]
    return sorted((aval + resto)[:max_itens], key=lambda e: e.ini)

def carga_semana(evs, foco, agora_):
    linhas = []
    for i in range(1, 8):
        d = foco + timedelta(days=i)
        a = analisar_dia(evs, d, agora_)
        n = {t: sum(1 for e in a["eventos"] if e.tipo == t)
             for t in ("aula", "avaliacao", "compromisso")}
        linhas.append((d, n, a["ocupado"]))
    return linhas

def _contagem(n: dict) -> str:
    partes = []
    if n["aula"]:
        partes.append(f"{n['aula']} aula{'s' if n['aula'] > 1 else ''}")
    if n["avaliacao"]:
        partes.append(f"{n['avaliacao']} avaliação" if n["avaliacao"] == 1
                      else f"{n['avaliacao']} avaliações")
    if n["compromisso"]:
        partes.append(f"{n['compromisso']} compromisso{'s' if n['compromisso'] > 1 else ''}")
    return ", ".join(partes)

def render_carga(carga):
    max_oc = max((c[2] for c in carga), default=timedelta())
    out = []
    for d, n, oc in carga:
        if not any(n.values()):
            out.append(f"- {_dia_curto(d)}: livre")
        else:
            peso = " ⚠️" if oc == max_oc and oc >= timedelta(hours=5) else ""
            out.append(f"- {_dia_curto(d)}: {_contagem(n)} ({_dur(oc)}){peso}")
    return "\n".join(out)

def perfil_dia(a) -> str:
    if any(e.tipo == "avaliacao" for e in a["eventos"]):
        return "dia de avaliação"
    h = a["ocupado"].total_seconds() / 3600
    return ("vazio" if h == 0 else "leve" if h < 3
            else "moderado" if h < 6 else "pesado")

def construir_factos(hoje, foco, modo_vespera, a, radar, carga) -> str:
    evs = a["eventos"]
    aulas = [e for e in evs if e.tipo == "aula"]
    aval = [e for e in evs if e.tipo == "avaliacao"]
    comp = [e for e in evs if e.tipo == "compromisso"]
    quando = "amanhã" if modo_vespera else "hoje"
    periodo = "manhã" if hoje.hour < 12 else "tarde" if hoje.hour < HORA_VESPERA else "noite"

    def lst(lista):
        return "; ".join(f"{e.titulo} {_hm(e.ini)}-{_hm(e.fim)}" for e in lista) or "nenhum"

    L = [
        f"Momento da conversa: {periodo}, {_hm(hoje)}.",
        f"DIA DE FOCO: {DIAS_SEMANA[foco.weekday()]} {foco:%d/%m} ({quando}).",
        f"Perfil do dia de foco: {perfil_dia(a)} ({_dur(a['ocupado'])} ocupados).",
        f"AULAS no dia de foco ({len(aulas)}): {lst(aulas)}.",
        f"AVALIAÇÕES no dia de foco ({len(aval)}): {lst(aval)}.",
        f"COMPROMISSOS NÃO-ACADÉMICOS no dia de foco ({len(comp)}): {lst(comp)}.",
    ]
    if a["maior_seq"] >= timedelta(hours=3):
        L.append(f"Maior bloco seguido sem pausa: {_dur(a['maior_seq'])}.")
    if a["janelas"]:
        mj = max(a["janelas"], key=lambda j: j[1] - j[0])
        L.append("Janelas livres no dia de foco: " + "; ".join(
            f"{_hm(i)}-{_hm(f)} ({_dur(f - i)})" for i, f in a["janelas"]) + ".")
        L.append(f"Maior janela livre: {_hm(mj[0])}-{_hm(mj[1])} ({_dur(mj[1] - mj[0])}).")
    else:
        L.append("Janelas livres no dia de foco: nenhuma.")

    if radar:
        L.append("RADAR (acontece DEPOIS do dia de foco, nunca no dia de foco):")
        for e in radar:
            dias = (e.ini.date() - foco).days
            kind = "avaliação" if e.tipo == "avaliacao" else "compromisso"
            L.append(f"  - {kind}: {e.titulo}, {_dia_curto(e.ini)} às {_hm(e.ini)} "
                     f"(daqui a {dias} dias).")
    else:
        L.append("RADAR: nada de relevante nos próximos 7 dias.")
    return "\n".join(L)

def render_agenda(a, hoje, modo_vespera):
    def linha(e):
        estado = "" if modo_vespera else (
            "✅ " if e.fim <= hoje else "▶️ " if e.ini <= hoje else "")
        nome = f"**{e.titulo}**" if e.tipo == "avaliacao" else e.titulo
        return f"- {ICONE[e.tipo]} {estado}`{_hm(e.ini)}–{_hm(e.fim)}` {nome}"

    linhas = [f"- 📌 {e.titulo} (dia todo)" for e in a["dia_todo"]]
    linhas += [linha(e) for e in a["eventos"]]
    if not linhas:
        return "_Sem aulas nem compromissos registados._"
    if a["janelas"]:
        linhas.append("\n**Janelas livres:** " + " · ".join(
            f"{_hm(i)}–{_hm(f)} ({_dur(f - i)})" for i, f in a["janelas"]))
    return "\n".join(linhas)

def render_radar(radar, hoje):
    if not radar:
        return "_Horizonte limpo nos próximos 7 dias._"
    out = []
    for e in radar:
        dias = (e.ini.date() - hoje.date()).days
        quando = "amanhã" if dias == 1 else f"daqui a {dias} dias"
        rotulo = "Avaliação" if e.tipo == "avaliacao" else "Compromisso"
        out.append(f"- {ICONE[e.tipo]} **{e.titulo}** ({rotulo}): "
                   f"{_dia_curto(e.ini)} às {_hm(e.ini)} ({quando})")
    return "\n".join(out)


def pedir_texto_ao_modelo(factos: str) -> dict:
    prompt = f"""És o J.A.R.V.I.S., Chief of Staff do Senhor. Português de Portugal.
Tom: britânico, formal, perspicaz e conciso. Trata-o por "Senhor".

Escreves APENAS três textos curtos. As listas são inseridas por outro sistema: não as repitas.

# FACTOS (única fonte de verdade)
{factos}

# VOCABULÁRIO OBRIGATÓRIO
- "aula": eventos da secção AULAS. Nunca lhes chames "compromisso".
- "avaliação" (teste, exame, entrega): eventos da secção AVALIAÇÕES ou do RADAR.
- "compromisso": APENAS eventos da secção COMPROMISSOS NÃO-ACADÉMICOS. Se for "nenhum", não uses esta palavra.

# REGRAS
1. Usa só os FACTOS. Nunca inventes eventos, horas, disciplinas ou matérias.
2. Horas em HH:MM, copiadas dos factos.
3. Os textos "resumo" e "analise" falam EXCLUSIVAMENTE do DIA DE FOCO. Usa "amanhã" ou "hoje" conforme indicado, não o dia da semana atual.
4. Eventos do RADAR nunca acontecem no dia de foco. Só os podes mencionar no "conselho", sempre com a sua data.
5. Usa o "Perfil do dia" para caracterizar o dia. Não o contradigas. Só falas de desgaste se houver bloco seguido ≥3h nos factos.
6. Sem listas, titles, emojis ou Markdown.

# TEXTOS
- "resumo": 1-2 frases. Cumprimento ao momento + o carácter do dia de foco (aulas, avaliações, compromissos, ou nada).
- "analise": 2-3 frases sobre o dia de foco: o que o ocupa, e como aproveitar a maior janela livre.
- "conselho": 1 frase, UMA ação concreta ligada a uma janela livre e, se fizer sentido, a uma avaliação do RADAR (com data).

# EXEMPLO (dados fictícios)
{{"resumo": "Boa noite, Senhor. Amanhã será um dia moderado, com duas aulas e sem compromissos extra.",
 "analise": "As aulas ocupam a manhã e o início da tarde, deixando uma janela de três horas entre as 11:00 e as 14:00. É o espaço ideal para estudo concentrado.",
 "conselho": "Sugiro usar a janela das 11:00 para começar a rever a matéria do teste de Cálculo 1 de quinta-feira, dia 15/10."}}

Responde apenas com o JSON."""
    r = cliente_groq.chat.completions.create(
        model=MODELO,
        messages=[{"role": "system", "content": prompt},
                  {"role": "user", "content": "Gera o briefing."}],
        response_format={"type": "json_object"},
        temperature=0.2,
    )
    return json.loads(r.choices[0].message.content)


def _horas_ok(textos: list[str], factos: str) -> bool:
    permitidas = set(re.findall(r"\d{1,2}:\d{2}", factos))
    return all(h in permitidas for t in textos for h in re.findall(r"\d{1,2}:\d{2}", t))

def _coerente(txt: dict, a, radar, modo_vespera) -> bool:
    resumo, analise, conselho = (txt.get(k, "") for k in ("resumo", "analise", "conselho"))
    corpo = f"{resumo} {analise}".lower()

    if "compromiss" in corpo and not any(e.tipo == "compromisso" for e in a["eventos"]):
        return False
    for e in radar:
        if e.titulo.lower() in corpo:
            return False
    if modo_vespera and re.search(r"\bhoje\b", corpo):
        return False
    return True

def texto_reserva(a, radar) -> dict:
    aulas = sum(1 for e in a["eventos"] if e.tipo == "aula")
    comp = sum(1 for e in a["eventos"] if e.tipo == "compromisso")
    if not a["eventos"]:
        resumo = "Agenda limpa, Senhor."
    else:
        partes = []
        if aulas: partes.append(f"{aulas} aula{'s' if aulas > 1 else ''}")
        if comp: partes.append(f"{comp} compromisso{'s' if comp > 1 else ''}")
        resumo = f"O dia de foco inclui {' e '.join(partes)}, Senhor."
    analise = (f"Carga total de {_dur(a['ocupado'])}." if a["eventos"]
               else "Um bom momento para trabalho profundo.")
    aval = [e for e in radar if e.tipo == "avaliacao"]
    conselho = (f"Sugiro preparar com antecedência: {aval[0].titulo} ({_dia_curto(aval[0].ini)})."
                if aval else "Sem avaliações à vista.")
    return {"resumo": resumo, "analise": analise, "conselho": conselho}

def briefing_jarvis(eventos: list[dict]) -> str:
    hoje = agora().replace(tzinfo=None)
    modo_vespera = hoje.hour >= HORA_VESPERA
    foco = hoje.date() + timedelta(days=1 if modo_vespera else 0)

    evs = [normalizar(e) for e in eventos]
    a = analisar_dia(evs, foco, hoje)
    radar = selecionar_radar(evs, foco)
    carga = carga_semana(evs, foco, hoje)
    factos = construir_factos(hoje, foco, modo_vespera, a, radar, carga)

    try:
        txt = pedir_texto_ao_modelo(factos)
        textos = [txt.get("resumo", ""), txt.get("analise", ""), txt.get("conselho", "")]
        if not all(textos) or not _horas_ok(textos, factos) or not _coerente(txt, a, radar, modo_vespera):
            txt = texto_reserva(a, radar)
    except Exception as exc:
        raise ErroIA("Não consegui gerar o briefing estratégico.") from exc
    except (json.JSONDecodeError, KeyError):
        txt = texto_reserva(a, radar)

    titulo_foco = (f"Foco: Amanhã ({_dia_curto(foco)})" if modo_vespera
                   else f"Foco Imediato (Hoje, {_dia_curto(foco)})")
    return f"""## 📊 Resumo Executivo
{txt['resumo']}

## 🎯 {titulo_foco}
{render_agenda(a, hoje, modo_vespera)}

{txt['analise']}

## 📡 Radar Estratégico
{render_radar(radar, hoje)}

**Carga dos próximos 7 dias**
{render_carga(carga)}

## 💡 Conselho J.A.R.V.I.S.
{txt['conselho']}
"""


# ────────────────────────────── ESTADO DA SESSÃO E INTERFACE ──────────────────────────────
def iniciar_sessao() -> None:
    st.session_state.setdefault("mensagens", [])      
    st.session_state.setdefault("ctx_inicio", 0)       
    st.session_state.setdefault("estado_evento", None)
    st.session_state.setdefault("evento_pendente", None)

def contexto_llm() -> list[dict]:
    s = st.session_state
    return [{"role": m["role"], "content": m["content"]}
            for m in s.mensagens[s.ctx_inicio:] if not m.get("briefing")]

def limpar_evento() -> None:
    st.session_state.estado_evento = None
    st.session_state.evento_pendente = None
    st.session_state.ctx_inicio = len(st.session_state.mensagens)

def gravar_evento(evento: dict) -> bool:
    try:
        criar_evento(evento)
    except Exception: 
        logger.exception("Falha ao gravar evento")
        st.error("Não foi possível gravar o evento no Google Calendar.")
        return False
    obter_eventos.clear()
    st.toast("Evento gravado com sucesso.", icon="✅")
    limpar_evento()
    return True

def tratar_mensagem(frase: str) -> bool:
    s = st.session_state
    with st.chat_message("user", avatar="👤"):
        st.write(frase)
    with st.chat_message("assistant", avatar="🤖"):
        try:
            with st.spinner("A processar..."):
                r = conversar(frase, contexto_llm(), s.estado_evento, s.evento_pendente)
        except ErroIA as exc:
            st.error(str(exc))
            return False
        st.write(r.fala)

    s.mensagens += [{"role": "user", "content": frase},
                    {"role": "assistant", "content": r.fala}]
    s.estado_evento = r.estado
    if r.acao == "pedir_confirmacao":
        s.evento_pendente = dict(r.estado)
    elif r.acao == "cancelar":
        limpar_evento()
    elif r.acao == "recolher_dados":
        s.evento_pendente = None
    return r.gravar and gravar_evento(r.estado)

def renderizar_sidebar() -> None:
    with st.sidebar:
        st.markdown("### 🎙️ Comandos")
        if st.button("🗣️ Solicitar Briefing Diário", type="primary", use_container_width=True):
            try:
                with st.spinner("A compilar o relatório estratégico..."):
                    resumo = briefing_jarvis(obter_eventos(15))
                st.session_state.mensagens += [
                    {"role": "user", "content": "J.A.R.V.I.S., dá-me o relatório da agenda.",
                     "briefing": True},
                    {"role": "assistant", "content": resumo, "briefing": True}]
            except Exception as exc: 
                logger.exception("Briefing falhou")
                st.error(str(exc) if isinstance(exc, ErroIA) else "Falha ao recolher dados.")
        st.toggle("Tema claro do calendário", key="modo_claro")

def renderizar_revisao() -> None:
    p = st.session_state.evento_pendente
    if not p:
        return
    st.markdown("### 📋 Revisão do Evento")
    with st.container(border=True):
        c1, c2, c3 = st.columns(3)
        c1.markdown(f"**📌 Título**\n\n{p['titulo']}")
        c2.markdown(f"**📅 Data**\n\n{p['data']}")
        c3.markdown(f"**🕒 Hora**\n\n{p['hora_inicio']}" + (f" – {p['hora_fim']}" if p.get("hora_fim") else ""))
        b1, b2 = st.columns(2)
        if b1.button("✅ Gravar", type="primary"):
            with st.spinner("A gravar..."):
                if gravar_evento(p):
                    st.rerun()
        if b2.button("❌ Cancelar"):
            limpar_evento()
            st.toast("Operação cancelada.", icon="🛑")
            st.rerun()

def css_calendario(t: dict) -> str:
    return f"""
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&display=swap');
    .fc {{ font-family:'Inter',sans-serif !important; background:{t['app']} !important; color:{t['texto']} !important; padding:15px; border-radius:12px; }}
    .fc-theme-standard td, .fc-theme-standard th {{ border-color:{t['linhas']} !important; }}
    .fc-timegrid-event, .fc-daygrid-event {{ border-radius:6px !important; padding:4px 6px !important; margin:2px !important; border:none !important; box-shadow:0 1px 3px rgba(0,0,0,.3) !important; }}
    .fc-event-main-frame {{ display:flex !important; justify-content:space-between !important; align-items:flex-start !important; }}
    .fc-event-title {{ font-size:.85em !important; font-weight:500 !important; }}
    .fc-event-time {{ order:2; font-size:.8em !important; opacity:.8; padding-left:8px; }}
    .fc-col-header-cell-cushion, .fc-timegrid-slot-label-cushion {{ color:{t['texto']} !important; opacity:.8; }}
    .fc-timegrid-now-indicator-arrow {{ display:none !important; }}
    .fc-timegrid-now-indicator-line {{ border-color:rgba(0,191,255,.5) !important; }}
    .fc-day-today {{ background:{t['hoje']} !important; }}
    .fc-button-primary {{ background:{t['botao']} !important; border:1px solid {t['linhas']} !important; color:{t['texto']} !important; text-transform:capitalize; }}
    .fc-button-active {{ background:{t['hover']} !important; }}
    """

def renderizar_calendario() -> None:
    st.divider()
    st.markdown("### 🗓️ Agenda")
    try:
        with st.spinner("A carregar calendário..."):
            eventos = eventos_visiveis()
    except Exception:
        logger.exception("Falha ao listar eventos")
        st.error("Sem ligação ao Google Calendar.")
        return
    if not eventos:
        st.info("Agenda livre.")
        return

    formatados = [{
        "title": e.get("summary", ""),
        "start": inicio_evento(e),
        "end": e["end"].get("dateTime", e["end"].get("date")),
        "backgroundColor": PALETA[sum(map(ord, e.get("summary", ""))) % len(PALETA)],
        "borderColor": "transparent", "textColor": "#F3F4F6", "display": "block",
    } for e in eventos]
    opcoes = {
        "headerToolbar": {"left": "today prev,next", "center": "title",
                          "right": "dayGridMonth,timeGridWeek,timeGridDay"},
        "initialView": "timeGridWeek", "slotMinTime": "07:00:00", "slotMaxTime": "23:00:00",
        "height": 800, "nowIndicator": True, "slotDuration": "01:00:00", "allDaySlot": False,
        "eventTimeFormat": {"hour": "2-digit", "minute": "2-digit", "hour12": False},
    }
    tema = TEMAS["claro" if st.session_state.get("modo_claro") else "escuro"]
    calendar(events=formatados, options=opcoes, custom_css=css_calendario(tema))

    with st.expander("🛠️ Gestão manual (apagar eventos)"):
        # Filtra eventos para gerir manualmente: esconde aulas, mas mantém outros
        eventos_gerir = [e for e in eventos if classificar(e.get("summary", ""), e) != "aula"]
        
        if not eventos_gerir:
            st.write("_Nenhum evento manual ou avaliação a apagar._")
        else:
            for e in eventos_gerir:
                info, apagar = st.columns([5, 1])
                info.write(f"**{e.get('summary', '')}** ({inicio_evento(e)[:16].replace('T', ' ')})")
                if apagar.button("🗑️", key=f"del_{e['id']}"):
                    try:
                        apagar_evento(e["id"])
                        obter_eventos.clear()
                    except Exception:
                        logger.exception("Falha ao apagar evento")
                        st.error("Não foi possível apagar o evento.")
                    else:
                        st.rerun()

def main() -> None:
    st.markdown("""
    <style>
      .jarvis-title { font-family:'Courier New',monospace; color:#00BFFF; text-align:center;
                      text-shadow:0 0 10px rgba(0,191,255,.5); }
      .stButton>button { width:100%; border-radius:8px; }
    </style>
    <h1 class='jarvis-title'>🤖 J.A.R.V.I.S.</h1>
    """, unsafe_allow_html=True)
    st.divider()

    iniciar_sessao()
    renderizar_sidebar()

    for m in st.session_state.mensagens:
        with st.chat_message(m["role"], avatar="👤" if m["role"] == "user" else "🤖"):
            st.write(m["content"])

    recarregar = False
    if frase := st.chat_input("Comunique com o J.A.R.V.I.S., Senhor..."):
        recarregar = tratar_mensagem(frase)
    if recarregar: 
        st.rerun()

    renderizar_revisao()
    renderizar_calendario()

main()