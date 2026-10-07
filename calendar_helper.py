import datetime
import streamlit as st
from google.oauth2 import service_account
from googleapiclient.discovery import build

SCOPES = ['https://www.googleapis.com/auth/calendar']

def fazer_login_google():
    # Lê as credenciais secretas do cofre da nuvem
    credenciais_info = st.secrets["gcp_service_account"]
    creds = service_account.Credentials.from_service_account_info(credenciais_info, scopes=SCOPES)
    return build('calendar', 'v3', credentials=creds)

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

    evento_criado = servico.events().insert(calendarId='primary', body=evento).execute()
    return evento_criado.get('htmlLink')

def listar_proximos_eventos(max_resultados=50):
    servico = fazer_login_google()
    agora = datetime.datetime.utcnow().isoformat() + 'Z' 
    
    eventos_resultado = servico.events().list(
        calendarId='primary', 
        timeMin=agora,
        maxResults=max_resultados, 
        singleEvents=True,
        orderBy='startTime'
    ).execute()
    
    return eventos_resultado.get('items', [])

def apagar_evento(evento_id):
    servico = fazer_login_google()
    servico.events().delete(calendarId='primary', eventId=evento_id).execute()
    return True