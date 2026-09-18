
import os
import requests
import json
import re
import gradio as gr
from openai import OpenAI
from fastapi import FastAPI, Request, Response
client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "homeburger_webhook_2026")
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SECRET_KEY = os.environ["SUPABASE_SECRET_KEY"]

def supabase_headers():
    return {
        "apikey": SUPABASE_SECRET_KEY,
        "Authorization": f"Bearer {SUPABASE_SECRET_KEY}",
        "Content-Type": "application/json",
    }

def es_telefono_utilizable(valor):
    digitos = re.sub(r"\D", "", str(valor or ""))
    return 9 <= len(digitos) <= 15


def normalizar_telefono(valor):
    return re.sub(r"\D", "", str(valor or ""))


def supabase_patch(identificador, datos):
    payload = {**datos, "updated_at": "now()"}
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/clientes",
        headers=supabase_headers(),
        params={"identificador_whatsapp": f"eq.{identificador}"},
        json=payload,
        timeout=20,
    )
    r.raise_for_status()


def obtener_cliente(identificador):
    # Primero por el identificador persistente nuevo.
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/clientes",
        headers=supabase_headers(),
        params={"identificador_whatsapp": f"eq.{identificador}", "select": "*", "limit": "1"},
        timeout=20,
    )
    r.raise_for_status()
    filas = r.json()
    if filas:
        return filas[0]

    # Compatibilidad con clientes creados antes de agregar identificador_whatsapp.
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/clientes",
        headers=supabase_headers(),
        params={"telefono": f"eq.{identificador}", "select": "*", "limit": "1"},
        timeout=20,
    )
    r.raise_for_status()
    filas = r.json()
    if filas:
        cliente = filas[0]
        r2 = requests.patch(
            f"{SUPABASE_URL}/rest/v1/clientes",
            headers=supabase_headers(),
            params={"id": f"eq.{cliente['id']}"},
            json={"identificador_whatsapp": identificador, "updated_at": "now()"},
            timeout=20,
        )
        r2.raise_for_status()
        cliente["identificador_whatsapp"] = identificador
        return cliente
    return None


def crear_cliente(identificador):
    payload = {"identificador_whatsapp": identificador}
    # Si Meta entrega un teléfono utilizable, también sirve para OlaClick.
    if es_telefono_utilizable(identificador):
        payload["telefono"] = normalizar_telefono(identificador)
    else:
        # Compatibilidad con el esquema actual si telefono todavía exige un valor.
        payload["telefono"] = identificador

    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/clientes",
        headers={**supabase_headers(), "Prefer": "return=representation"},
        json=payload,
        timeout=20,
    )
    r.raise_for_status()
    filas = r.json()
    return filas[0] if filas else payload


def obtener_o_crear_cliente(identificador):
    cliente = obtener_cliente(identificador)
    return cliente if cliente else crear_cliente(identificador)


def cargar_json(campo, cliente, valor_default):
    if not cliente:
        return valor_default
    bruto = cliente.get(campo)
    if not bruto:
        return valor_default
    if isinstance(bruto, (dict, list)):
        return bruto
    try:
        return json.loads(bruto)
    except (json.JSONDecodeError, TypeError):
        return valor_default


def cargar_historial(cliente):
    datos = cargar_json("historial", cliente, [])
    return datos if isinstance(datos, list) else []


def cargar_pedido_actual(cliente):
    datos = cargar_json("pedido_actual", cliente, {})
    return datos if isinstance(datos, dict) else {}


def guardar_historial(identificador, historial):
    historial = historial[-24:]
    supabase_patch(
        identificador,
        {"historial": json.dumps(historial, ensure_ascii=False)},
    )


def guardar_nombre(identificador, nombre):
    supabase_patch(identificador, {"nombre": nombre})


def guardar_telefono(identificador, telefono):
    supabase_patch(identificador, {"telefono": normalizar_telefono(telefono)})


def guardar_pedido_actual(identificador, pedido):
    supabase_patch(
        identificador,
        {"pedido_actual": json.dumps(pedido, ensure_ascii=False)},
    )


def guardar_ultimo_pedido(identificador, pedido):
    supabase_patch(
        identificador,
        {
            "ultimo_pedido": json.dumps(pedido, ensure_ascii=False),
            "pedido_actual": None,
        },
    )


def mensaje_ya_procesado(cliente, mensaje_id):
    return bool(mensaje_id and cliente and cliente.get("ultimo_mensaje_id") == mensaje_id)


def marcar_mensaje_procesado(identificador, mensaje_id):
    if mensaje_id:
        supabase_patch(identificador, {"ultimo_mensaje_id": mensaje_id})


def ultimo_texto_asistente(historial):
    for item in reversed(historial or []):
        if isinstance(item, dict) and item.get("role") == "assistant":
            return extraer_texto(item.get("content", ""))
    return ""


def asistente_pidio_nombre(historial):
    ultimo = ultimo_texto_asistente(historial).lower()
    return (
        "cuál es tu nombre" in ultimo
        or "cual es tu nombre" in ultimo
        or "cómo te llamas" in ultimo
        or "como te llamas" in ultimo
    )


def candidato_nombre_pendiente(historial):
    ultimo = ultimo_texto_asistente(historial).strip()
    m = re.match(r"^([A-Za-zÁÉÍÓÚÜÑáéíóúüñ' -]{2,40}),\s*¿?cierto\??", ultimo, re.IGNORECASE)
    return m.group(1).strip() if m else None



def extraer_nombre_declarado(texto):
    """Extrae el candidato cuando el cliente declara su nombre de forma natural."""
    limpio = (texto or "").strip()
    patrones = [
        r"^(?:mi nombre es)\s+(.+?)\s*$",
        r"^(?:me llamo)\s+(.+?)\s*$",
        r"^(?:soy)\s+(.+?)\s*$",
        r"^(?:perd[oó]n[, ]+)?(?:mi nombre es|me llamo|soy)\s+(.+?)\s*$",
    ]
    for patron in patrones:
        m = re.match(patron, limpio, re.IGNORECASE)
        if m:
            candidato = m.group(1).strip(" .,!¡¿?")
            if 1 <= len(candidato.split()) <= 4 and len(candidato) <= 60:
                return candidato
    return None


def es_correccion_nombre_explicita(texto):
    t = (texto or "").lower().strip()
    return (
        t.startswith("perdón, mi nombre es")
        or t.startswith("perdon, mi nombre es")
        or t.startswith("perdón me llamo")
        or t.startswith("perdon me llamo")
        or t.startswith("mi nombre es ")
        or t.startswith("me llamo ")
    )

def evaluar_nombre(texto):
    prompt = f"""
El cliente de una hamburguesería acaba de responder a la pregunta por su nombre con:
{json.dumps(texto, ensure_ascii=False)}

Devuelve SOLO JSON válido:
{{"es_nombre": true, "nombre": "Nombre", "sugerencia": null}}

Reglas:
- Si parece un nombre, conserva lo escrito corrigiendo solo mayúsculas/minúsculas.
- Si es MUY probablemente un error de teclado de un nombre conocido, usa "sugerencia".
- Ejemplo: Jusn -> {{"es_nombre": true, "nombre": "Jusn", "sugerencia": "Juan"}}
- Si no hay alta certeza, sugerencia debe ser null.
- Si no parece una respuesta de nombre, es_nombre debe ser false.
"""
    r = client.responses.create(model="gpt-5.4-mini", input=prompt)
    try:
        return json.loads(r.output_text.strip())
    except Exception:
        return {"es_nombre": False, "nombre": None, "sugerencia": None}


def extraer_estado_pedido(texto, historial, pedido_actual):
    contexto = historial[-10:] if historial else []
    prompt = f"""
Actualiza el estado estructurado de un pedido de Home Burger usando el mensaje nuevo.
Devuelve SOLO JSON válido. No inventes datos.

ESTADO ANTERIOR:
{json.dumps(pedido_actual or {}, ensure_ascii=False)}

CONTEXTO RECIENTE:
{json.dumps(contexto, ensure_ascii=False)}

MENSAJE NUEVO:
{json.dumps(texto, ensure_ascii=False)}

Campos permitidos:
{{
  "productos": [],
  "modalidad": null,
  "salsas": null,
  "bebida": null,
  "ubicacion": null,
  "delivery_costo": null,
  "confirmado": false
}}

Reglas:
- Conserva todos los datos anteriores salvo que el cliente los cambie.
- "recojo" y expresiones equivalentes => modalidad "recojo".
- "delivery", "me lo envías", etc. => modalidad "delivery".
- Si ya indicó una salsa, consérvala y no la borres.
- "sin bebida", "no quiero bebida" => bebida "sin bebida".
- Si menciona una bebida concreta, guárdala.
- productos debe contener texto breve suficiente para recordar cantidades, producto y extras.
- No marques confirmado por tu cuenta; conserva el valor anterior.
"""
    r = client.responses.create(model="gpt-5.4-mini", input=prompt)
    try:
        datos = json.loads(r.output_text.strip())
        return datos if isinstance(datos, dict) else (pedido_actual or {})
    except Exception:
        return pedido_actual or {}


def telefono_visible_para_ticket(cliente, identificador):
    tel = (cliente or {}).get("telefono")
    if tel and es_telefono_utilizable(tel):
        return normalizar_telefono(tel)
    if es_telefono_utilizable(identificador):
        return normalizar_telefono(identificador)
    return None


def detectar_telefono_en_texto(texto):
    candidatos = re.findall(r"(?:\+?\d[\d\s-]{7,}\d)", texto or "")
    for candidato in candidatos:
        digitos = normalizar_telefono(candidato)
        if 9 <= len(digitos) <= 15:
            return digitos
    return None


def pedido_parece_activo(pedido):
    return bool(pedido and (pedido.get("productos") or pedido.get("modalidad")))


def faltante_bloqueante(pedido, nombre_cliente, telefono_ticket):
    if not pedido_parece_activo(pedido):
        return None
    if not pedido.get("productos"):
        return "producto"
    if not pedido.get("modalidad"):
        return "modalidad"
    if pedido.get("modalidad") == "delivery":
        if not pedido.get("ubicacion"):
            return "ubicacion"
        if pedido.get("delivery_costo") is None:
            return "delivery_costo"
    if not nombre_cliente:
        return "nombre"
    if not telefono_ticket:
        return "telefono"
    return None


def respuesta_para_faltante(faltante):
    respuestas = {
        "modalidad": "¿Será delivery o recojo? 😊",
        "ubicacion": "Compárteme tu ubicación o dirección para calcular el delivery 📍",
        "delivery_costo": "Déjame confirmar el costo de delivery para darte el total definitivo 😊",
        "nombre": "Genial 😊 ¿Cuál es tu nombre?",
        "telefono": "¿Me brindas tu número para registrar tu ticket? 😊",
    }
    return respuestas.get(faltante)


INSTRUCCIONES = """
Eres el asistente de atención de Home Burger. Hablas como Home Burger y nunca dices que eres una IA.
Responde breve, natural y amable, como WhatsApp. Emojis moderados.
Lee TODO el historial y usa el contexto. No repitas preguntas ya respondidas.
No inventes precios, productos, ingredientes, promociones, delivery, tiempos ni condiciones.
El sistema puede darte un ESTADO ESTRUCTURADO DEL PEDIDO. Trátalo como fuente de verdad.
No vuelvas a preguntar un dato que ya esté resuelto en ese estado.
Si el cliente ya indicó salsa o que no quiere bebida desde su primer mensaje, no lo preguntes otra vez.
Si el sistema indica que el teléfono para ticket ya está disponible, no lo pidas.
Nunca confirmes si el sistema indica que falta un dato obligatorio.

SALUDO:
Si el primer mensaje es solo un saludo:
"¡Hola! 👋 Bienvenido a Home Burger 🍔 ¿Buscando una burger para hoy? 👀"
Si el primer mensaje ya contiene una intención o pedido, saluda brevemente y atiende directamente.
No vuelvas a saludar durante una conversación ya iniciada.
Si dice "hola" durante una conversación, continúa el contexto.

CARTA:
BURGERS:
Consentida S/15.50
Doradita S/16.90
Indomable S/18.90
Soberana S/18.90
Doble con queso S/21.90

Todas las burgers incluyen papas fritas aparte, pero NO lo menciones salvo que pregunten qué incluye o por las papas.

Si alguien pide "una clásica" sin contexto previo, oriéntalo hacia la burger:
"Claro 😊 La clásica sería nuestra Consentida 🍔 ¿Te preparo una?"
Si el contexto es de salchipapas, "clásica" significa Salchi Clásica.

FILETES:
Pollo Clásico S/12.90
Filete con Cheddar S/13.90
Filete Royal S/14.90
Despeinado S/12.90

SALCHIPAPAS:
Salchi Clásica S/11.90 = papas fritas + salchicha.
Salchipollo S/15.90 = papas fritas + salchicha + pollo.
No incluyen otra porción de papas aparte.

ALITAS:
Alitas BBQ S/17.90 = 6 alitas BBQ + papas fritas.

PAPAS:
Clásicas S/3.00
Familiares S/7.90

BEBIDAS:
Coca-Cola S/4
Inca Kola S/4
Fanta S/4
Agua S/4 (opción de upsell).

Si preguntan genéricamente cuánto cuesta la bebida:
"Las bebidas cuestan S/4 🥤 Coca-Cola, Inca Kola, Fanta y agua."

COMBO INDIVIDUAL:
Consentida S/19.90
Doradita S/20.90
Indomable S/22.90
Soberana S/22.90
Doble con queso S/25.90
Incluye 1 burger + papas + bebida.

COMBO DÚO:
Consentida S/36.90
Doradita S/38.90
Indomable S/42.90
Soberana S/42.90
Doble con queso S/48.90
Incluye 2 burgers + papas + 2 bebidas.

EXTRAS:
Cheddar S/2.50
Tocino S/2.50
Huevo S/2.50
Carne adicional S/7

SALSAS:
Mayonesa, ketchup, mostaza, tártara y ají.

FLUJO:
Producto → delivery/recojo → salsas → bebida → datos necesarios → resumen/monto → pago → confirmación.

No preguntes automáticamente si desea agregar algo más.
No preguntes si desea ver el resumen: muéstralo directamente cuando corresponda.
Nunca menciones una burger, salchipapa u otra categoría que no exista en el pedido actual.

Interpreta lenguaje natural:
"me envías", "mándamelo", "me lo traes" y similares = delivery.
"voy", "paso", "lo recojo" y similares = recojo.
Entiende porfis, porfa, xfa, xfis, pls como por favor, pero no los repitas mecánicamente.

SALSAS:
Burger/filete:
"¿Deseas alguna salsa aparte? Mayonesa, ketchup, mostaza, tártara o ají."

Salchi/Salchipollo:
Pregunta qué salsas desea dentro. No ofrezcas aparte salvo que lo solicite.

Si hay salchipapa + burger:
"¿Qué salsas deseas para la salchipapa? Mayonesa, ketchup, mostaza, tártara o ají. ¿Deseas alguna aparte para la burger?"

Construye siempre la pregunta según los productos REALES del pedido.

BEBIDA:
Después de salsas:
"¿Deseas agregar una bebida por S/4? 🥤 Coca-Cola, Inca Kola, Fanta o agua."
Si dice no, no insistas.
Si el combo ya incluye bebida, pregunta cuál desea.

CLIENTES:
Si es cliente nuevo, después de que el pedido haya avanzado pregunta en un momento natural:
"Genial 😊 ¿Cuál es tu nombre?"
Cuando responda, usa su nombre ocasionalmente, no en cada mensaje.
Si acabas de preguntar el nombre, espera esa respuesta antes de continuar al resumen, pago o confirmación.
Si el sistema ya dispone de un nombre confirmado, no vuelvas a preguntarlo.

Si el sistema no dispone de su número, preguntar cuando corresponda:
"¿Me brindas tu número para registrar tu ticket? 😊"
Si el número ya está disponible, NO preguntarlo.

UBICACIÓN:
Si preguntan dónde están ubicados:
"Estamos a media cuadra de Metro de Aramburú 📍"
No menciones Surquillo en esa primera respuesta.
No preguntes si desea la ubicación salvo que haya indicado que será recojo.

HORARIO:
Martes a sábado de 6:00 pm a 11:00 pm.
Un pedido puede ingresar a las 11:00 pm.
A partir de las 11:01 pm está fuera del horario habitual.

CARTA Y PRECIOS GENERALES:
Si el cliente pide la carta, menú o precios de forma general (por ejemplo: "¿tienes carta?", "¿dónde están tus precios?", "¿cuánto cuestan?", "¿qué precios tienen?", "pásame la carta", "quiero ver el menú"), responde únicamente:
"Claro, te envío la carta 🍔"
No escribas ni enumeres los precios en ese caso. La imagen de la carta se enviará automáticamente.
Si pregunta por el precio de un producto específico, responde solo ese precio y NO envíes la carta.

PREGUNTAS PUNTUALES:
Si el cliente pregunta únicamente el precio de un producto específico, ingrediente, horario, ubicación u otro dato puntual, responde SOLO lo necesario.
No agregues ofertas, combos, llamadas a comprar ni "si deseas..." innecesarios.
Ejemplo:
"¿Cuánto cuesta la Consentida?"
"La Consentida cuesta S/15.50 🍔"

PAGO:
Calcula cuidadosamente el subtotal.
Solo incluye delivery cuando el costo sea conocido. Nunca lo inventes.

Formato:
🍔 Pedido: S/[subtotal]
🛵 Delivery: S/[delivery]
💰 Total: S/[total]

📲 Yape o Plin
993 427 906
Pablo Delgado

También se acepta efectivo.

CONFIRMACIÓN:
Cuando ya estén completos los datos necesarios, muestra resumen, total y datos de pago y confirma el pedido sin esperar un "gracias".

"¡Pedido confirmado! ✅
Ya estamos en preparación 🍔🔥"

No condicionar la confirmación a que ya haya pagado por Yape o Plin.

IMPORTANTE:
"ok", "sí", "dale", "listo" y similares se interpretan según la pregunta inmediatamente anterior.
Nunca asumir automáticamente que significan confirmar el pedido.

Después de confirmar no hagas preguntas ni ofertas.
ACTUALIZACIONES IMPORTANTES:

ESTADO DEL PEDIDO:
- Si un pedido ya fue confirmado, conserva ese estado durante toda la conversación.
- Si después de confirmar el cliente simplemente dice "hola", no inicies una nueva atención.
- Responde indicando que su pedido ya está en preparación.
- Solo inicia otro pedido si el cliente expresa claramente que quiere hacer un pedido nuevo o adicional.

FILETES:
- Nunca inventes ingredientes de los filetes.
- Si no tienes definidos los ingredientes necesarios para responder una pregunta, no los deduzcas ni los completes por tu cuenta.

SALCHIPAPAS Y SALCHIPOLLO:
- Siempre se dice "el Salchipollo", nunca "la Salchipollo".
- Para Salchi Clásica o Salchipollo pregunta:
"¿Qué salsas deseas? Mayonesa, ketchup, mostaza, tártara o ají."
- No uses la palabra "dentro" al preguntar por las salsas.
- Si el mismo pedido contiene Salchi Clásica o Salchipollo y una burger, pregunta las salsas de la salchipapa o Salchipollo y además:
"¿Deseas alguna salsa aparte para la burger?"
- No preguntes por salsas aparte para Salchi Clásica o Salchipollo salvo que el cliente las solicite expresamente.

ALITAS:
- No preguntes automáticamente si desea salsas aparte para las alitas.

TIEMPO:
- Si el cliente pregunta cuánto demora el pedido, responde:
"El tiempo aproximado es de 20 minutos 😊"
- No añadas tiempo de traslado ni un rango diferente.

CAMBIOS EN PEDIDOS:
- Antes de confirmar el pedido, acepta las modificaciones solicitadas y actualiza el pedido y el total cuando corresponda.
- Si el pedido ya fue confirmado y el cliente solicita una modificación, no prometas que puede realizarse.
- Responde:
"Claro 😊 Déjame confirmar si todavía podemos hacer ese cambio."
- Ese caso requiere confirmación de Home Burger.

DELIVERY Y TOTAL:
- Nunca confirmes un pedido si el costo de delivery está pendiente o el total definitivo todavía no puede calcularse.
- Nunca muestres "S/[por confirmar]" como si fuera un importe.
- Si falta conocer el costo de delivery, indica que está pendiente de confirmación.
- Solo después de conocer el costo de delivery calcula y muestra el total definitivo y continúa con la confirmación.

MENSAJE DE CONFIRMACIÓN:
- Si es DELIVERY:
"¡Pedido confirmado! ✅
Ya estamos en preparación 🍔🔥
Te avisamos cuando salga 🛵"

- Si es RECOJO:
"¡Pedido confirmado! ✅
Ya estamos en preparación 🍔🔥
Te avisamos cuando esté listo."

- Después de confirmar no hagas preguntas ni ofertas.
"""

def extraer_texto(valor):
    if valor is None:
        return ""

    if isinstance(valor, str):
        return valor

    if isinstance(valor, dict):
        if "text" in valor:
            return extraer_texto(valor["text"])

        if "content" in valor:
            return extraer_texto(valor["content"])

        return str(valor)

    if isinstance(valor, (list, tuple)):
        partes = []

        for elemento in valor:
            texto = extraer_texto(elemento)

            if texto:
                partes.append(texto)

        return "".join(partes)

    return str(valor)


def responder(message, history, nombre_cliente=None, pedido_actual=None, telefono_ticket=None):
    mensajes = []

    for item in history or []:
        if isinstance(item, dict):
            role = item.get("role")
            contenido = extraer_texto(item.get("content", ""))
            if role in ("user", "assistant") and contenido:
                mensajes.append({"role": role, "content": contenido})
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            usuario = extraer_texto(item[0])
            asistente = extraer_texto(item[1])
            if usuario:
                mensajes.append({"role": "user", "content": usuario})
            if asistente:
                mensajes.append({"role": "assistant", "content": asistente})

    mensaje_actual = extraer_texto(message)
    mensajes.append({"role": "user", "content": mensaje_actual})

    instrucciones_actuales = INSTRUCCIONES + f"""

ESTADO ESTRUCTURADO DEL PEDIDO ACTUAL:
{json.dumps(pedido_actual or {}, ensure_ascii=False)}

DATOS PERSISTENTES:
- Nombre confirmado: {nombre_cliente or "NO DISPONIBLE"}
- Teléfono utilizable para ticket: {telefono_ticket or "NO DISPONIBLE"}

REGLAS DE ESTADO:
- No preguntes nuevamente ningún dato que ya figure en el estado.
- Si bebida dice "sin bebida", no ofrezcas bebida.
- Si salsas ya tiene contenido, no vuelvas a preguntar las salsas correspondientes.
- Si nombre está confirmado, no vuelvas a pedirlo.
- Si teléfono utilizable está disponible, no vuelvas a pedirlo.
"""

    response = client.responses.create(
        model="gpt-5.4-mini",
        instructions=instrucciones_actuales,
        input=mensajes,
    )
    return response.output_text

app = FastAPI()
@app.get("/carta")
async def carta_home_burger():
    return Response(
        content=open("carta_home_burger.jpeg", "rb").read(),
        media_type="image/jpeg"
    )

@app.get("/privacy")
async def privacy_policy():
    return Response(
        content="""
        <html>
        <head><title>Política de Privacidad - Home Burger</title></head>
        <body style="font-family:Arial;max-width:800px;margin:40px auto;padding:20px;line-height:1.6">
        <h1>Política de Privacidad de Home Burger</h1>
        <p>Home Burger utiliza WhatsApp para atender consultas y gestionar pedidos de sus clientes.</p>
        <p>Podemos recibir información proporcionada voluntariamente por el cliente, como nombre, número de teléfono, dirección de entrega y datos relacionados con su pedido.</p>
        <p>Esta información se utiliza únicamente para brindar atención, procesar pedidos, coordinar entregas y mejorar nuestro servicio.</p>
        <p>No vendemos ni comercializamos la información personal de nuestros clientes.</p>
        <p>Los datos pueden ser procesados mediante proveedores tecnológicos necesarios para prestar el servicio, incluyendo servicios de mensajería y procesamiento automatizado.</p>
        <p>Los clientes pueden solicitar información, corrección o eliminación de sus datos contactando directamente a Home Burger por WhatsApp.</p>
        <p>Última actualización: septiembre de 2026.</p>
        </body>
        </html>
        """,
        media_type="text/html"
    )
@app.get("/webhook")
async def verificar_webhook(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return int(challenge)

    return {"error": "Verification failed"}

@app.post("/webhook")
async def recibir_whatsapp(request: Request):
    data = await request.json()

    try:
        value = data["entry"][0]["changes"][0]["value"]
        if "messages" not in value:
            return {"status": "ok"}

        mensaje = value["messages"][0]
        if mensaje.get("type") != "text":
            return {"status": "ok"}

        identificador = mensaje["from"]
        mensaje_id = mensaje.get("id")
        texto_cliente = mensaje["text"]["body"]

        cliente = obtener_o_crear_cliente(identificador)

        # Evita contestar dos veces al mismo evento de WhatsApp.
        if mensaje_ya_procesado(cliente, mensaje_id):
            return {"status": "ok"}

        historial = cargar_historial(cliente)
        pedido_actual = cargar_pedido_actual(cliente)
        nombre_cliente = cliente.get("nombre") if cliente else None
        telefono_ticket = telefono_visible_para_ticket(cliente, identificador)

        # Si el cliente escribe un teléfono cuando se lo estamos pidiendo, guárdalo.
        telefono_enviado = detectar_telefono_en_texto(texto_cliente)
        if telefono_enviado and not telefono_ticket:
            guardar_telefono(identificador, telefono_enviado)
            telefono_ticket = telefono_enviado

        # Actualiza el pedido estructurado antes de responder.
        pedido_actual = extraer_estado_pedido(texto_cliente, historial, pedido_actual)
        guardar_pedido_actual(identificador, pedido_actual)

        candidato = candidato_nombre_pendiente(historial)
        texto_nombre = texto_cliente.lower().strip()
        nombre_declarado = extraer_nombre_declarado(texto_cliente)

        # Una declaración explícita de nombre se entiende aunque la pregunta anterior
        # haya sido otra. También permite corregir un nombre guardado.
        if nombre_declarado:
            evaluacion = evaluar_nombre(nombre_declarado)
            if evaluacion.get("es_nombre"):
                nombre_recibido = (evaluacion.get("nombre") or nombre_declarado).strip()
                sugerencia = evaluacion.get("sugerencia")
                if sugerencia and sugerencia.strip().lower() != nombre_recibido.lower():
                    respuesta = f"{sugerencia.strip()}, ¿cierto? 😊"
                else:
                    guardar_nombre(identificador, nombre_recibido)
                    nombre_cliente = nombre_recibido
                    respuesta = responder(
                        texto_cliente, historial, nombre_cliente, pedido_actual, telefono_ticket
                    )
            else:
                respuesta = "¿Cuál es tu nombre? 😊"

        elif candidato and texto_nombre in ("si", "sí", "s", "correcto", "exacto", "asi es", "así es"):
            guardar_nombre(identificador, candidato)
            nombre_cliente = candidato
            respuesta = responder(
                texto_cliente, historial, nombre_cliente, pedido_actual, telefono_ticket
            )

        elif candidato and texto_nombre in ("no", "nop", "nope"):
            respuesta = "Entendido 😊 ¿Cuál es tu nombre?"

        elif not nombre_cliente and asistente_pidio_nombre(historial):
            evaluacion = evaluar_nombre(texto_cliente)
            if evaluacion.get("es_nombre"):
                nombre_recibido = (evaluacion.get("nombre") or texto_cliente).strip()
                sugerencia = evaluacion.get("sugerencia")
                if sugerencia and sugerencia.strip().lower() != nombre_recibido.lower():
                    respuesta = f"{sugerencia.strip()}, ¿cierto? 😊"
                else:
                    guardar_nombre(identificador, nombre_recibido)
                    nombre_cliente = nombre_recibido
                    respuesta = responder(
                        texto_cliente, historial, nombre_cliente, pedido_actual, telefono_ticket
                    )
            else:
                respuesta = "¿Cuál es tu nombre? 😊"
        else:
            # Los datos obligatorios no quedan a criterio del modelo.
            # Si el pedido ya está activo y falta el nombre, se pide antes de continuar.
            if pedido_parece_activo(pedido_actual) and not nombre_cliente:
                respuesta = "Genial 😊 ¿Cuál es tu nombre?"
            else:
                respuesta = responder(
                    texto_cliente, historial, nombre_cliente, pedido_actual, telefono_ticket
                )

        # Barrera adicional: si el teléfono ya está disponible, nunca permitimos
        # que el modelo vuelva a pedirlo.
        if telefono_ticket and (
            "me brindas tu número" in respuesta.lower()
            or "me brindas tu numero" in respuesta.lower()
            or "compartes tu número" in respuesta.lower()
            or "compartes tu numero" in respuesta.lower()
            or "tu número para registrar" in respuesta.lower()
            or "tu numero para registrar" in respuesta.lower()
        ):
            if not nombre_cliente:
                respuesta = "Genial 😊 ¿Cuál es tu nombre?"
            else:
                respuesta = responder(
                    texto_cliente, historial, nombre_cliente, pedido_actual, telefono_ticket
                )

        # Barrera de seguridad: el modelo no puede confirmar si falta un dato obligatorio.
        if "pedido confirmado" in respuesta.lower():
            faltante = faltante_bloqueante(pedido_actual, nombre_cliente, telefono_ticket)
            if faltante:
                reemplazo = respuesta_para_faltante(faltante)
                if reemplazo:
                    respuesta = reemplazo
            else:
                pedido_actual["confirmado"] = True
                guardar_ultimo_pedido(identificador, pedido_actual)

        historial_actualizado = historial + [
            {"role": "user", "content": texto_cliente},
            {"role": "assistant", "content": respuesta},
        ]
        guardar_historial(identificador, historial_actualizado)

        access_token = os.environ["WHATSAPP_ACCESS_TOKEN"]
        phone_number_id = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
        url = f"https://graph.facebook.com/v26.0/{phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        texto_normalizado = texto_cliente.lower().strip()
        pedidos_carta = [
            "carta", "menú", "menu", "precios", "precio", "precios xfa",
            "precios porfa", "precios porfis", "me pasas los precios",
            "me pasas la carta", "me pasa la carta", "pásame los precios",
            "pasame los precios", "pásame la carta", "pasame la carta",
            "envíame la carta", "enviame la carta", "mándame la carta",
            "mandame la carta", "quiero ver los precios", "quiero ver la carta",
            "quiero ver el menú", "quiero ver el menu", "tienes carta",
            "tienen carta", "tienes menú", "tienes menu", "tienen menú",
            "tienen menu", "dónde están tus precios", "donde estan tus precios",
            "dónde están los precios", "donde estan los precios",
            "cuánto cuestan", "cuanto cuestan", "qué precios tienen",
            "que precios tienen", "qué tienen", "que tienen", "qué venden",
            "que venden", "qué opciones tienen", "que opciones tienen",
            "qué hamburguesas tienen", "que hamburguesas tienen",
            "qué burgers tienen", "que burgers tienen",
        ]
        productos_concretos = [
            "consentida", "doradita", "indomable", "soberana",
            "doble con queso", "pollo clásico", "pollo clasico",
            "filete con cheddar", "filete royal", "despeinado",
            "salchi clásica", "salchi clasica", "salchipollo",
            "alitas bbq", "papas clásicas", "papas clasicas",
            "papas familiares",
        ]
        enviar_carta = (
            any(frase in texto_normalizado for frase in pedidos_carta)
            and not any(producto in texto_normalizado for producto in productos_concretos)
        )

        if enviar_carta:
            payload_texto = {
                "messaging_product": "whatsapp",
                "to": identificador,
                "type": "text",
                "text": {"body": "Claro, te envío la carta 🍔"},
            }
            r1 = requests.post(url, headers=headers, json=payload_texto, timeout=20)
            r1.raise_for_status()

            payload_imagen = {
                "messaging_product": "whatsapp",
                "to": identificador,
                "type": "image",
                "image": {"link": "https://home-burger-assistant.onrender.com/carta"},
            }
            r2 = requests.post(url, headers=headers, json=payload_imagen, timeout=20)
            r2.raise_for_status()
            marcar_mensaje_procesado(identificador, mensaje_id)
            return {"status": "ok"}

        payload = {
            "messaging_product": "whatsapp",
            "to": identificador,
            "type": "text",
            "text": {"body": respuesta},
        }
        r = requests.post(url, headers=headers, json=payload, timeout=20)
        r.raise_for_status()

        # Se marca al final: si el envío falla, Meta puede reintentar.
        marcar_mensaje_procesado(identificador, mensaje_id)
        return {"status": "ok"}

    except Exception as e:
        print("Error WhatsApp:", repr(e))
        return {"status": "error"}

demo = gr.ChatInterface(
    fn=responder,
    title="Home Burger Assistant 🍔",
    description="Prueba el asistente como si fueras un cliente."
)

app = gr.mount_gradio_app(app, demo, path="/")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
