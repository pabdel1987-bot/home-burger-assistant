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

def obtener_cliente(telefono):
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/clientes",
        headers=supabase_headers(),
        params={"telefono": f"eq.{telefono}", "select": "*", "limit": "1"},
        timeout=20,
    )
    if not r.ok:
        print("SUPABASE ERROR GET:", r.status_code, r.text)
        r.raise_for_status()
    filas = r.json()
    return filas[0] if filas else None


def crear_cliente(telefono):
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/clientes",
        headers={**supabase_headers(), "Prefer": "return=representation"},
        json={"telefono": telefono},
        timeout=20,
    )
    if not r.ok:
        print("SUPABASE ERROR POST:", r.status_code, r.text)
        r.raise_for_status()
    filas = r.json()
    return filas[0] if filas else {"telefono": telefono}

def obtener_o_crear_cliente(telefono):
    cliente = obtener_cliente(telefono)
    return cliente if cliente else crear_cliente(telefono)

def cargar_historial(cliente):
    if not cliente:
        return []
    bruto = cliente.get("historial")
    if not bruto:
        return []
    try:
        datos = json.loads(bruto)
        return datos if isinstance(datos, list) else []
    except (json.JSONDecodeError, TypeError):
        return []

def guardar_historial(telefono, historial):
    historial = historial[-24:]
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/clientes",
        headers=supabase_headers(),
        params={"telefono": f"eq.{telefono}"},
        json={
            "historial": json.dumps(historial, ensure_ascii=False),
            "updated_at": "now()",
        },
        timeout=20,
    )
    r.raise_for_status()


def guardar_nombre(telefono, nombre):
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/clientes",
        headers=supabase_headers(),
        params={"telefono": f"eq.{telefono}"},
        json={"nombre": nombre, "updated_at": "now()"},
        timeout=20,
    )
    r.raise_for_status()


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


def evaluar_nombre(texto):
    prompt = f"""
El cliente de una hamburguesería acaba de responder a la pregunta por su nombre con:
{json.dumps(texto, ensure_ascii=False)}

Devuelve SOLO JSON válido con este formato:
{{"es_nombre": true, "nombre": "Nombre", "sugerencia": null}}

Reglas:
- Si parece claramente un nombre, conserva lo que escribió, corrigiendo solo mayúsculas/minúsculas.
- Si parece MUY probablemente un error de teclado de un nombre conocido, no lo corrijas silenciosamente:
  pon en "nombre" exactamente lo escrito y en "sugerencia" el nombre probable.
- Ejemplo: Jusn -> {{"es_nombre": true, "nombre": "Jusn", "sugerencia": "Juan"}}
- Si no hay alta certeza de error, sugerencia debe ser null.
- Si el texto no parece una respuesta de nombre, es_nombre debe ser false.
"""
    r = client.responses.create(
        model="gpt-5.4-mini",
        input=prompt,
    )
    try:
        datos = json.loads(r.output_text.strip())
    except Exception:
        return {"es_nombre": False, "nombre": None, "sugerencia": None}
    return datos


INSTRUCCIONES = """
Eres el asistente de atención de Home Burger. Hablas como Home Burger y nunca dices que eres una IA.
Responde breve, natural y amable, como WhatsApp. Emojis moderados.
Lee TODO el historial y usa el contexto. No repitas preguntas ya respondidas.
No inventes precios, productos, ingredientes, promociones, delivery, tiempos ni condiciones.

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


def responder(message, history, nombre_cliente=None):
    mensajes = []

    for item in history or []:
        if isinstance(item, dict):
            role = item.get("role")
            contenido = extraer_texto(item.get("content", ""))

            if role in ("user", "assistant") and contenido:
                mensajes.append({
                    "role": role,
                    "content": contenido
                })

        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            usuario = extraer_texto(item[0])
            asistente = extraer_texto(item[1])

            if usuario:
                mensajes.append({
                    "role": "user",
                    "content": usuario
                })

            if asistente:
                mensajes.append({
                    "role": "assistant",
                    "content": asistente
                })

    mensaje_actual = extraer_texto(message)

    mensajes.append({
        "role": "user",
        "content": mensaje_actual
    })

    instrucciones_actuales = INSTRUCCIONES
    if nombre_cliente:
        instrucciones_actuales += f"""
DATOS PERSISTENTES DEL CLIENTE:
- Nombre confirmado: {nombre_cliente}
- No vuelvas a preguntarle su nombre salvo que el propio cliente indique que quiere corregirlo.
"""

    response = client.responses.create(
        model="gpt-5.4-mini",
        instructions=instrucciones_actuales,
        input=mensajes
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

        numero_cliente = mensaje["from"]
        texto_cliente = mensaje["text"]["body"]

        cliente = obtener_o_crear_cliente(numero_cliente)
        historial = cargar_historial(cliente)
        nombre_cliente = cliente.get("nombre") if cliente else None

        candidato = candidato_nombre_pendiente(historial)
        texto_normalizado_nombre = texto_cliente.lower().strip()

        if candidato and texto_normalizado_nombre in ("si", "sí", "s", "correcto", "exacto", "asi es", "así es"):
            guardar_nombre(numero_cliente, candidato)
            nombre_cliente = candidato
            respuesta = responder(texto_cliente, historial, nombre_cliente)

        elif candidato and texto_normalizado_nombre in ("no", "nop", "nope"):
            respuesta = "Entendido 😊 ¿Cuál es tu nombre?"

        elif not nombre_cliente and asistente_pidio_nombre(historial):
            evaluacion = evaluar_nombre(texto_cliente)

            if evaluacion.get("es_nombre"):
                nombre_recibido = (evaluacion.get("nombre") or texto_cliente).strip()
                sugerencia = evaluacion.get("sugerencia")

                if sugerencia and sugerencia.strip().lower() != nombre_recibido.lower():
                    respuesta = f"{sugerencia.strip()}, ¿cierto? 😊"
                else:
                    guardar_nombre(numero_cliente, nombre_recibido)
                    nombre_cliente = nombre_recibido
                    respuesta = responder(texto_cliente, historial, nombre_cliente)
            else:
                respuesta = "¿Cuál es tu nombre? 😊"
        else:
            respuesta = responder(texto_cliente, historial, nombre_cliente)

        historial_actualizado = historial + [
            {"role": "user", "content": texto_cliente},
            {"role": "assistant", "content": respuesta},
        ]
        guardar_historial(numero_cliente, historial_actualizado)

        access_token = os.environ["WHATSAPP_ACCESS_TOKEN"]
        phone_number_id = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
        url = f"https://graph.facebook.com/v26.0/{phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
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
            "qué burgers tienen", "que burgers tienen"
        ]
        productos_concretos = [
            "consentida", "doradita", "indomable", "soberana",
            "doble con queso", "pollo clásico", "pollo clasico",
            "filete con cheddar", "filete royal", "despeinado",
            "salchi clásica", "salchi clasica", "salchipollo",
            "alitas bbq", "papas clásicas", "papas clasicas",
            "papas familiares"
        ]
        enviar_carta = (
            any(frase in texto_normalizado for frase in pedidos_carta)
            and not any(producto in texto_normalizado for producto in productos_concretos)
        )

        if enviar_carta:
            payload_texto_carta = {
                "messaging_product": "whatsapp",
                "to": numero_cliente,
                "type": "text",
                "text": {"body": "Claro, te envío la carta 🍔"}
            }
            r_texto_carta = requests.post(
                url, headers=headers, json=payload_texto_carta, timeout=20
            )
            print("WhatsApp texto carta status:", r_texto_carta.status_code)
            print("WhatsApp texto carta response:", r_texto_carta.text)
            r_texto_carta.raise_for_status()

            payload_imagen = {
                "messaging_product": "whatsapp",
                "to": numero_cliente,
                "type": "image",
                "image": {"link": "https://home-burger-assistant.onrender.com/carta"}
            }
            r_imagen = requests.post(url, headers=headers, json=payload_imagen, timeout=20)
            print("WhatsApp imagen status:", r_imagen.status_code)
            print("WhatsApp imagen response:", r_imagen.text)
            r_imagen.raise_for_status()
            return {"status": "ok"}

        payload = {
            "messaging_product": "whatsapp",
            "to": numero_cliente,
            "type": "text",
            "text": {"body": respuesta}
        }
        r = requests.post(url, headers=headers, json=payload, timeout=20)
        print("WhatsApp status:", r.status_code)
        print("WhatsApp response:", r.text)
        r.raise_for_status()
        return {"status": "ok"}

    except Exception as e:
        print("Error WhatsApp:", e)
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
