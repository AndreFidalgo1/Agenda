import datetime
import streamlit as st
from google.oauth2 import service_account
from googleapiclient.discovery import build

# Agora o robô tem permissão para a Agenda e para as Folhas de Cálculo
SCOPES = [
    'https://www.googleapis.com/auth/calendar',
    'https://www.googleapis.com/auth/spreadsheets'
]

def obter_credenciais():
    credenciais_info = st.secrets["gcp_service_account"]
    return service_account.Credentials.from_service_account_info(credenciais_info, scopes=SCOPES)

# --- FUNÇÕES DO CALENDÁRIO ---
def fazer_login_google():
    return build('calendar', 'v3', credentials=obter_credenciais())

def criar_evento(dados_evento):
    servico = fazer_login_google()
    data_string = dados_evento['data']
    
    descricao = dados_evento.get('descricao')
    if descricao == "null" or descricao is None:
        descricao = ""
        
    evento = {
        'summary': dados_evento['titulo'],
        'description': descricao,
    }
    
    recorrencia = dados_evento.get('recorrencia')
    if recorrencia and str(recorrencia) != "null" and str(recorrencia).startswith("RRULE:"):
        evento['recurrence'] = [recorrencia]
    
    hora_inicio = dados_evento.get('hora_inicio')
    if hora_inicio and hora_inicio != "null":
        inicio_datetime = f"{data_string}T{hora_inicio}:00"
        
        hora_fim = dados_evento.get('hora_fim')
        if hora_fim and hora_fim != "null":
            fim_datetime = f"{data_string}T{hora_fim}:00"
        else:
            hora_i = datetime.datetime.strptime(hora_inicio, "%H:%M")
            hora_f = hora_i + datetime.timedelta(hours=1)
            fim_datetime = f"{data_string}T{hora_f.strftime('%H:%M')}:00"
        
        evento['start'] = {'dateTime': inicio_datetime, 'timeZone': 'Europe/Lisbon'}
        evento['end'] = {'dateTime': fim_datetime, 'timeZone': 'Europe/Lisbon'}
    else:
        evento['start'] = {'date': data_string}
        evento['end'] = {'date': data_string}

    # ATENÇÃO: Substitui o email abaixo pelo teu email real (o que usaste antes)
    evento_criado = servico.events().insert(calendarId='fidalgoandre90@gmail.com', body=evento).execute()
    return evento_criado.get('htmlLink')

def listar_proximos_eventos(max_resultados=50):
    servico = fazer_login_google()
    agora = datetime.datetime.utcnow().isoformat() + 'Z' 
    
    eventos_resultado = servico.events().list(
        calendarId='fidalgoandre90@gmail.com', 
        timeMin=agora,
        maxResults=max_resultados, 
        singleEvents=True,
        orderBy='startTime'
    ).execute()
    
    return eventos_resultado.get('items', [])

def apagar_evento(evento_id):
    servico = fazer_login_google()
    servico.events().delete(calendarId='fidalgoandre90@gmail.com', eventId=evento_id).execute()
    return True


# --- FUNÇÕES DO BACKLOG (GOOGLE SHEETS) ---
def fazer_login_sheets():
    return build('sheets', 'v4', credentials=obter_credenciais())

def listar_tarefas_pendentes():
    servico = fazer_login_sheets()
    sheet_id = st.secrets["SHEET_ID"]
    resultado = servico.spreadsheets().values().get(spreadsheetId=sheet_id, range='A2:C').execute()
    linhas = resultado.get('values', [])
    
    tarefas = []
    # A linha 2 do Excel corresponde ao index 2 (a primeira linha são os títulos)
    for i, linha in enumerate(linhas):
        estado = linha[2] if len(linha) > 2 else ""
        if estado != "Concluído":
            tarefas.append({
                "linha_excel": i + 2, 
                "tarefa": linha[0] if len(linha) > 0 else "", 
                "prioridade": linha[1] if len(linha) > 1 else "Média"
            })
    return tarefas

def adicionar_tarefa(tarefa, prioridade):
    servico = fazer_login_sheets()
    sheet_id = st.secrets["SHEET_ID"]
    valores = [[tarefa, prioridade, "Pendente"]]
    corpo = {'values': valores}
    servico.spreadsheets().values().append(
        spreadsheetId=sheet_id, range='A:C',
        valueInputOption='USER_ENTERED', body=corpo).execute()

def concluir_tarefa(linha_excel):
    servico = fazer_login_sheets()
    sheet_id = st.secrets["SHEET_ID"]
    valores = [["Concluído"]]
    corpo = {'values': valores}
    servico.spreadsheets().values().update(
        spreadsheetId=sheet_id, range=f'C{linha_excel}',
        valueInputOption='USER_ENTERED', body=corpo).execute()