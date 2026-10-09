import ccxt
import time
import math
import hmac
import hashlib
import requests
import os
import sys
import json
import platform
import traceback
import threading
from urllib.parse import urlencode
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ==============================================================================
# CONFIGURACIÓN GENERAL DEL BOT
# ==============================================================================
MIN_RATIO = 2.0                   # Ratio mínimo R:R (1:2)
PAUSA_ENTRE_PARES_SEG = 0.04      # Pausa entre pares en segundos
PAUSA_ERROR_RED_SEG = 10          # Pausa si se cae la red

# ------------------------------------------------------------------------------
# CALCULADORA DE ENTRADAS (capital / riesgo / leverage) — editable
# ------------------------------------------------------------------------------
CAPITAL_DISPONIBLE = 20.0        # Capital disponible en USDT
RIESGO_PCT = 5                   # % del capital que se arriesga por operación (10 = 10%)
LEVERAGE = 10                     # Apalancamiento (solo afecta el margen necesario)
# ==============================================================================

# ------------------------------------------------------------------------------
# EJECUCIÓN DE ÓRDENES EN BINANCE — editable
# ------------------------------------------------------------------------------
EJECUTAR_ORDENES_REALES = True    # ⚠️ En False = solo imprime lo que HARÍA, no manda nada.
USAR_TESTNET = False                # True = fapi Testnet (dinero de prueba). False = Binance real.

ACTIVACION_TRAILING_R = 1.5        # El trailing se activa cuando el precio llega a 1.5R.
MAX_OPERACIONES_ABIERTAS = 4       # Operaciones simultáneas configuradas por defecto a 4.
SEGUNDOS_ESPERA_CUPO_LLENO = 30    # Con el cupo lleno, no escanea: solo revisa cada tantos segundos
MINUTOS_MAX_ORDEN_PENDIENTE = 0   # Tiempo límite por defecto en minutos (0 = nunca cancela)

# ------------------------------------------------------------------------------
# RANKING DE SEÑALES
# ------------------------------------------------------------------------------
PESO_RATIO = 0.5             
PESO_VOLUMEN = 0.3           
PESO_MOVIMIENTO_SL = 0.2     

try:
    from config import BINANCE_API_KEY, BINANCE_API_SECRET  # noqa: E402
except ImportError:
    BINANCE_API_KEY, BINANCE_API_SECRET = None, None

try:
    from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
except ImportError:
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID = None, None

BINANCE_FAPI_BASE = "https://testnet.binancefuture.com" if USAR_TESTNET else "https://fapi.binance.com"

TZ_LOCAL = ZoneInfo("America/Bogota")   
INTERVALO_MONITOR_SEG = 20              
# ==============================================================================


def _binance_signed_request(method, path, params, api_key, api_secret):
    params = dict(params)
    params['timestamp'] = int(time.time() * 1000)
    params.setdefault('recvWindow', 5000)
    query = urlencode(params, doseq=True)
    signature = hmac.new(api_secret.encode('utf-8'), query.encode('utf-8'), hashlib.sha256).hexdigest()
    query += f"&signature={signature}"
    url = f"{BINANCE_FAPI_BASE}{path}?{query}"
    headers = {'X-MBX-APIKEY': api_key}

    resp = requests.request(method, url, headers=headers, timeout=10)
    data = resp.json()
    if resp.status_code != 200:
        raise Exception(f"Binance algoOrder error {resp.status_code}: {data}")
    return data


def calcular_trailing_protector(precio_entrada, precio_stop, activacion_r=ACTIVACION_TRAILING_R):
    es_long = precio_entrada > precio_stop
    r = abs(precio_entrada - precio_stop)
    if r <= 0:
        return None

    if es_long:
        activation_price = precio_entrada + activacion_r * r
        protegido_1_1 = precio_entrada + r
        callback_rate = 1 - (protegido_1_1 / activation_price)
    else:
        activation_price = precio_entrada - activacion_r * r
        protegido_1_1 = precio_entrada - r
        callback_rate = (protegido_1_1 / activation_price) - 1

    callback_rate_pct = callback_rate * 100
    callback_rate_pct_ajustado = max(0.1, min(5.0, round(callback_rate_pct, 1)))

    return {
        'activation_price': activation_price,
        'protegido_1_1': protegido_1_1,
        'callback_rate_pct': callback_rate_pct,
        'callback_rate_pct_ajustado': callback_rate_pct_ajustado,
    }


def _es_orden_de_entrada(o):
    if o.get('reduceOnly'):
        return False
    info = o.get('info') or {}
    if str(info.get('closePosition', '')).lower() == 'true':
        return False
    return True


def contar_posiciones_por_lado(exchange):
    ocupados = {}
    ok = True

    try:
        for p in exchange.fetch_positions():
            contratos = p.get('contracts') or 0
            if not contratos or float(contratos) == 0:
                continue
            lado = p.get('side')
            if lado not in ('long', 'short'):
                lado = 'long' if float(contratos) > 0 else 'short'
            ocupados[p.get('symbol')] = (lado, 'posición abierta')
    except Exception as e:
        print(f"⚠️ No se pudo consultar fetch_positions() para el cupo ({e}). Por seguridad, se asume cupo LLENO.")
        ok = False

    try:
        ordenes = exchange.fetch_open_orders(params={'subType': 'linear'})
        
        if not ordenes:
            try:
                ordenes = exchange.fapiPrivateGetOpenOrders()
            except Exception:
                pass

        for o in ordenes:
            if not _es_orden_de_entrada(o):
                continue
            
            sym = o.get('symbol')
            if sym and '/' not in sym and 'USDT' in sym:
                base = sym.replace('USDT', '')
                sym = f"{base}/USDT:USDT"

            if sym in ocupados:
                continue
            
            side_raw = str(o.get('side')).lower()
            lado = 'long' if side_raw in ('buy', 'long') else 'short'
            ocupados[sym] = (lado, 'orden de entrada pendiente')

    except Exception as e:
        print(f"⚠️ No se pudo consultar las órdenes pendientes para el cupo ({e}). Por seguridad, se asume cupo LLENO.")
        ok = False

    longs = sum(1 for lado, _ in ocupados.values() if lado == 'long')
    shorts = sum(1 for lado, _ in ocupados.values() if lado == 'short')
    detalle = [f"{sym} {lado.upper()} ({origen})" for sym, (lado, origen) in ocupados.items()]
    return ok, longs + shorts, longs, shorts, detalle


def _normalizar(valor, minimo, maximo):
    if maximo == minimo:
        return 1.0
    return (valor - minimo) / (maximo - minimo)


def ordenar_candidatas_por_score(candidatas):
    if not candidatas:
        return []

    ratios = [c['ratio'] for c in candidatas]
    volumenes = [c['volumen_entrada'] for c in candidatas]
    movimientos = [c['pct_movimiento_sl'] for c in candidatas]

    r_min, r_max = min(ratios), max(ratios)
    v_min, v_max = min(volumenes), max(volumenes)
    m_min, m_max = min(movimientos), max(movimientos)

    for c in candidatas:
        score_ratio = _normalizar(c['ratio'], r_min, r_max)
        score_volumen = _normalizar(c['volumen_entrada'], v_min, v_max)
        score_movimiento = _normalizar(c['pct_movimiento_sl'], m_min, m_max)
        c['score'] = (score_ratio * PESO_RATIO) + (score_volumen * PESO_VOLUMEN) + (score_movimiento * PESO_MOVIMIENTO_SL)

    return sorted(candidatas, key=lambda c: c['score'], reverse=True)


def hay_cupo(total_ocupado, long_ocupado, short_ocupado, es_long):
    if total_ocupado >= MAX_OPERACIONES_ABIERTAS:
        return False, f"cupo total lleno ({total_ocupado}/{MAX_OPERACIONES_ABIERTAS})"

    max_por_lado = math.ceil(MAX_OPERACIONES_ABIERTAS / 2)
    ocupado_lado = long_ocupado if es_long else short_ocupado
    if ocupado_lado >= max_por_lado:
        lado_txt = 'LONG' if es_long else 'SHORT'
        return False, f"cupo de {lado_txt} lleno ({ocupado_lado}/{max_por_lado})"

    return True, None


def tiene_posicion_u_orden_abierta(exchange, symbol):
    try:
        posiciones = exchange.fetch_positions([symbol])
        for p in posiciones:
            contratos = p.get('contracts') or 0
            if contratos and float(contratos) != 0:
                return True
    except Exception as e:
        print(f"⚠️ No se pudo verificar posición en {symbol} ({e}). Por seguridad, se omite esta candidata.")
        return True

    try:
        ordenes = exchange.fetch_open_orders(symbol)
        if ordenes:
            return True
    except Exception as e:
        print(f"⚠️ No se pudo verificar órdenes abiertas en {symbol} ({e}). Por seguridad, se omite esta candidata.")
        return True

    return False


def validar_precision_y_notional(exchange, symbol, market_info, cantidad, precio_entrada):
    try:
        cantidad_str = exchange.amount_to_precision(symbol, cantidad)
        precio_str = exchange.price_to_precision(symbol, precio_entrada)
        cantidad_ok = float(cantidad_str)
        precio_ok = float(precio_str)
    except Exception as e:
        return None, None, f"no se pudo ajustar a la precisión del par ({e})"

    limites = market_info.get('limits', {}) or {}
    min_qty = ((limites.get('amount') or {}).get('min'))
    min_notional = ((limites.get('cost') or {}).get('min')) or 5.0
    notional = cantidad_ok * precio_ok

    if cantidad_ok <= 0:
        return None, None, "la cantidad calculada redondeó a 0 con la precisión del par"
    if min_qty and cantidad_ok < min_qty:
        return None, None, f"cantidad {cantidad_ok} por debajo del mínimo del par ({min_qty})"
    if min_notional and notional < min_notional:
        return None, None, (f"el valor de la orden ({notional:.2f} USDT) está por debajo del "
                             f"mínimo que exige Binance para este par ({min_notional} USDT)")

    return cantidad_ok, precio_ok, None


PROTECCION_PENDIENTE = {}   
_lock_proteccion = threading.Lock()


def ejecutar_operacion(exchange, symbol, market_info, es_long, precio_entrada, precio_stop, calc):
    lado_entrada = 'buy' if es_long else 'sell'
    lado_cierre = 'SELL' if es_long else 'BUY'
    simbolo_binance = market_info['id']  
    cantidad = calc['cantidad_monedas']

    trailing = calcular_trailing_protector(precio_entrada, precio_stop)
    if not trailing:
        print("   ⚠️  No se pudo calcular el trailing (precio de entrada = stop). Se omite la operación.")
        return False

    try:
        cantidad_ok, precio_ok, error = validar_precision_y_notional(exchange, symbol, market_info, cantidad, precio_entrada)
    except Exception as e:
        cantidad_ok, precio_ok, error = None, None, f"no se pudo validar contra el exchange ({e})"

    print(f"   🤖 Plan de ejecución: LIMIT {lado_entrada.upper()} {cantidad} {market_info['base']} @ {precio_entrada}")
    print(f"   🤖 SL (se arma cuando la posición exista): {precio_stop}")
    print(f"   🤖 Trailing protector 1:1 → activación {trailing['activation_price']:.8f} "
          f"| callback {trailing['callback_rate_pct_ajustado']}% "
          f"(protege ≈ {trailing['protegido_1_1']:.8f})")

    if error:
        print(f"   ⏭️  Se omite {symbol}: {error}.\n")
        return False

    if not EJECUTAR_ORDENES_REALES:
        print("   🔒 EJECUTAR_ORDENES_REALES=False → no se mandó ninguna orden real.\n")
        return True

    if not BINANCE_API_KEY or not BINANCE_API_SECRET:
        print("   ❌ Faltan BINANCE_API_KEY / BINANCE_API_SECRET en config.py. No se puede operar.\n")
        return False

    try:
        exchange.set_leverage(LEVERAGE, symbol)
        client_id = f"{PREFIJO_ORDEN}{int(time.time() * 1000)}"
        orden_entrada = exchange.create_order(symbol, 'limit', lado_entrada, cantidad_ok, precio_ok,
                                              params={'clientOrderId': client_id})
        print(f"   ✅ Orden de entrada enviada. id={orden_entrada.get('id')}")
    except Exception as e:
        print(f"   ❌ Error mandando la orden de entrada: {e}\n")
        tg_enviar(f"❌ ERROR mandando entrada en {symbol}: {e}")
        return False

    with _lock_proteccion:
        PROTECCION_PENDIENTE[symbol] = {
            'simbolo_binance': simbolo_binance,
            'lado_cierre': lado_cierre,
            'precio_stop': precio_stop,
            'activation_price': trailing['activation_price'],
            'callback_rate': trailing['callback_rate_pct_ajustado'],
            'protegido_1_1': trailing['protegido_1_1'],
        }
    print(f"   ⏳ SL/Trailing quedaron pendientes: se colocan automáticamente en cuanto la entrada se llene.\n")
    tg_enviar(f"📥 ENTRADA ENVIADA: {symbol} {'LONG' if es_long else 'SHORT'}\n"
              f"LIMIT: {precio_ok} | Cantidad: {cantidad_ok}\n"
              f"SL/Trailing se arman solos en cuanto se llene la entrada.")

    return True


def colocar_proteccion_pendiente(exchange, symbol):
    with _lock_proteccion:
        info = PROTECCION_PENDIENTE.pop(symbol, None)
    if not info:
        return  

    try:
        _binance_signed_request('POST', '/fapi/v1/algoOrder', {
            'algoType': 'CONDITIONAL',
            'symbol': info['simbolo_binance'],
            'side': info['lado_cierre'],
            'type': 'STOP_MARKET',
            'triggerPrice': exchange.price_to_precision(symbol, info['precio_stop']),
            'closePosition': 'true',
            'workingType': 'MARK_PRICE',
            'priceProtect': 'true',
        }, BINANCE_API_KEY, BINANCE_API_SECRET)

        _binance_signed_request('POST', '/fapi/v1/algoOrder', {
            'algoType': 'CONDITIONAL',
            'symbol': info['simbolo_binance'],
            'side': info['lado_cierre'],
            'type': 'TRAILING_STOP_MARKET',
            'closePosition': 'true',
            'activationPrice': exchange.price_to_precision(symbol, info['activation_price']),
            'callbackRate': info['callback_rate'],
            'workingType': 'MARK_PRICE',
        }, BINANCE_API_KEY, BINANCE_API_SECRET)

        print(f"   ✅ SL/Trailing colocados para {symbol} (posición confirmada).")
        tg_enviar(f"🎯 PROTECCIÓN ARMADA: {symbol}\n"
                  f"SL: {info['precio_stop']}\n"
                  f"Trailing → activación {info['activation_price']:.6f} "
                  f"(callback {info['callback_rate']}%, protege ≈ {info['protegido_1_1']:.6f})")
    except Exception as e:
        print(f"   ⚠️  {symbol}: posición abierta PERO falló el SL/Trailing: {e}")
        tg_enviar(f"🚨 URGENTE: {symbol} tiene una posición abierta SIN protección ({e}).")


PREFIJO_ORDEN = "esc"   


def _ruta(nombre):
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), nombre)


ARCHIVO_AJUSTES = _ruta("ajustes_bot.json")   
ARCHIVO_ESTADO = _ruta("estado_bot.json")     

AJUSTES_EDITABLES = {
    'CAPITAL_DISPONIBLE': (float, 1, 1_000_000, "Capital disponible (USDT)"),
    'RIESGO_PCT': (float, 0.1, 50, "% del capital que se arriesga por operación"),
    'LEVERAGE': (int, 1, 125, "Apalancamiento"),
    'MAX_OPERACIONES_ABIERTAS': (int, 1, 20, "Operaciones simultáneas máximas"),
    'SEGUNDOS_ESPERA_CUPO_LLENO': (float, 5, 3600, "Segundos entre revisiones cuando el cupo está lleno"),
    'MIN_RATIO': (float, 0.5, 100, "Ratio R:R mínimo (2 = 1:2)"),
    'ACTIVACION_TRAILING_R': (float, 1.05, 10, "Activación del trailing en múltiplos de R"),
    'MINUTOS_MAX_ORDEN_PENDIENTE': (float, 0, 1440, "Minutos antes de cancelar una orden límite sin llenar (0 = nunca)"),
    'PAUSA_ENTRE_PARES_SEG': (float, 0, 10, "Pausa entre pares (seg)"),
    'PAUSA_ERROR_RED_SEG': (float, 1, 300, "Pausa tras error de red (seg)"),
    'PESO_RATIO': (float, 0, 1, "Peso del ratio R:R en el ranking"),
    'PESO_VOLUMEN': (float, 0, 1, "Peso del volumen del muro en el ranking"),
    'PESO_MOVIMIENTO_SL': (float, 0, 1, "Peso del % hasta el SL en el ranking"),
    'EJECUTAR_ORDENES_REALES': (bool, None, None, "Mandar órdenes reales (true/false)"),
}

ALIAS_AJUSTES = {
    'capital': 'CAPITAL_DISPONIBLE', 'riesgo': 'RIESGO_PCT', 'leverage': 'LEVERAGE',
    'apalancamiento': 'LEVERAGE', 'operaciones': 'MAX_OPERACIONES_ABIERTAS',
    'ratio': 'MIN_RATIO', 'trailing': 'ACTIVACION_TRAILING_R',
    'pendiente': 'MINUTOS_MAX_ORDEN_PENDIENTE', 'ejecutar': 'EJECUTAR_ORDENES_REALES',
}


def _convertir_valor(tipo, texto):
    t = str(texto).strip().replace(',', '.')
    if tipo is bool:
        if t.lower() in ('true', '1', 'si', 'sí', 'on', 'activar', 'activado'):
            return True
        if t.lower() in ('false', '0', 'no', 'off', 'desactivar', 'desactivado'):
            return False
        raise ValueError("usa true o false")
    numero = float(t)
    if tipo is int:
        if numero != int(numero):
            raise ValueError("debe ser un número entero")
        return int(numero)
    return numero


def resolver_nombre_ajuste(texto):
    k = texto.strip().lower()
    if k in ALIAS_AJUSTES:
        return ALIAS_AJUSTES[k]
    for nombre in AJUSTES_EDITABLES:
        if nombre.lower() == k:
            return nombre
    return None


def aplicar_ajuste(nombre, valor, guardar=True):
    tipo, minimo, maximo, _ = AJUSTES_EDITABLES[nombre]
    valor = tipo(valor)
    if tipo is not bool:
        if minimo is not None and valor < minimo:
            raise ValueError(f"{nombre} debe estar entre {minimo:g} y {maximo:g}")
        if maximo is not None and valor > maximo:
            raise ValueError(f"{nombre} debe estar entre {minimo:g} y {maximo:g}")
    globals()[nombre] = valor
    if guardar:
        try:
            with open(ARCHIVO_AJUSTES, 'r', encoding='utf-8') as f:
                guardados = json.load(f)
        except Exception:
            guardados = {}
        guardados[nombre] = valor
        with open(ARCHIVO_AJUSTES, 'w', encoding='utf-8') as f:
            json.dump(guardados, f, indent=2, ensure_ascii=False)


def cargar_ajustes():
    if not os.path.exists(ARCHIVO_AJUSTES):
        return
    try:
        with open(ARCHIVO_AJUSTES, 'r', encoding='utf-8') as f:
            guardados = json.load(f)
    except Exception:
        return
    for nombre, valor in guardados.items():
        if nombre in AJUSTES_EDITABLES:
            try:
                aplicar_ajuste(nombre, valor, guardar=False)
            except Exception:
                pass


def _tg_api(metodo, payload=None, timeout=15):
    if not TELEGRAM_BOT_TOKEN:
        return None
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{metodo}",
                          json=payload or {}, timeout=timeout)
        return r.json()
    except Exception:
        return None


def tg_enviar(texto, botones=None):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': texto}
    if botones:
        payload['reply_markup'] = {'inline_keyboard': botones}
    _tg_api('sendMessage', payload)


def tg_editar(chat_id, message_id, texto, botones=None):
    payload = {'chat_id': chat_id, 'message_id': message_id, 'text': texto}
    if botones:
        payload['reply_markup'] = {'inline_keyboard': botones}
    _tg_api('editMessageText', payload)


def _autorizado(chat_id):
    return TELEGRAM_CHAT_ID is not None and str(chat_id) == str(TELEGRAM_CHAT_ID)


TEXTO_AYUDA = (
    "🤖 COMANDOS\n\n"
    "/ajustes — panel interactivo con botones (+/- capital, riesgo, leverage, cupos, tiempo pendiente)\n"
    "/valores — lista de todos los valores editables\n"
    "/set NOMBRE VALOR — cambia cualquiera manualmente (ej: /set MIN_RATIO 2.5)\n"
    "/capital 100 · /riesgo 5 · /leverage 5 — atajos rápidos\n"
    "/pnl — PnL real diario, semanal y mensual\n"
    "/posiciones — operaciones abiertas y órdenes pendientes\n"
    "/estado — resumen general del sistema\n\n"
)


def panel_ajustes():
    riesgo_usd = CAPITAL_DISPONIBLE * RIESGO_PCT / 100
    tiempo_txt = "Desactivado (0 min)" if MINUTOS_MAX_ORDEN_PENDIENTE == 0 else f"{MINUTOS_MAX_ORDEN_PENDIENTE:g} min"
    texto = (
        "⚙️ PANEL DE AJUSTES INTERACTIVO\n\n"
        f"💰 Capital: {CAPITAL_DISPONIBLE:g} USDT\n"
        f"⚠️ Riesgo: {RIESGO_PCT:g}% (≈ {riesgo_usd:.2f} USDT por operación)\n"
        f"⚙️ Leverage: {LEVERAGE}x\n"
        f"📊 Operaciones Máximas: {MAX_OPERACIONES_ABIERTAS}\n"
        f"⏱️ Cancelar pendiente en: {tiempo_txt}\n\n"
        "Usa los botones para ajustar los valores en tiempo real:"
    )
    botones = [
        [{'text': '💰 Cap −50', 'callback_data': 'adj|CAPITAL_DISPONIBLE|-50'},
         {'text': '💰 Cap −10', 'callback_data': 'adj|CAPITAL_DISPONIBLE|-10'},
         {'text': '💰 Cap +10', 'callback_data': 'adj|CAPITAL_DISPONIBLE|10'},
         {'text': '💰 Cap +50', 'callback_data': 'adj|CAPITAL_DISPONIBLE|50'}],
        [{'text': '⚠️ Riesgo −5%', 'callback_data': 'adj|RIESGO_PCT|-5'},
         {'text': '⚠️ Riesgo −1%', 'callback_data': 'adj|RIESGO_PCT|-1'},
         {'text': '⚠️ Riesgo +1%', 'callback_data': 'adj|RIESGO_PCT|1'},
         {'text': '⚠️ Riesgo +5%', 'callback_data': 'adj|RIESGO_PCT|5'}],
        [{'text': '⚙️ Lev −5x', 'callback_data': 'adj|LEVERAGE|-5'},
         {'text': '⚙️ Lev −1x', 'callback_data': 'adj|LEVERAGE|-1'},
         {'text': '⚙️ Lev +1x', 'callback_data': 'adj|LEVERAGE|1'},
         {'text': '⚙️ Lev +5x', 'callback_data': 'adj|LEVERAGE|5'}],
        [{'text': '📊 Cupo −1', 'callback_data': 'adj|MAX_OPERACIONES_ABIERTAS|-1'},
         {'text': '📊 Cupo +1', 'callback_data': 'adj|MAX_OPERACIONES_ABIERTAS|1'}],
        [{'text': '⏱️ Tiempo −5m', 'callback_data': 'adj|MINUTOS_MAX_ORDEN_PENDIENTE|-5'},
         {'text': '⏱️ Off (0m)', 'callback_data': 'set_val|MINUTOS_MAX_ORDEN_PENDIENTE|0'},
         {'text': '⏱️ Tiempo +5m', 'callback_data': 'adj|MINUTOS_MAX_ORDEN_PENDIENTE|5'}],
    ]
    return texto, botones


def texto_valores():
    inverso = {}
    for alias, nombre in ALIAS_AJUSTES.items():
        inverso.setdefault(nombre, alias)
    lineas = ["📋 VALORES EDITABLES (usa /set NOMBRE VALOR)\n"]
    for nombre, (_, _, _, desc) in AJUSTES_EDITABLES.items():
        alias = f" (alias: {inverso[nombre]})" if nombre in inverso else ""
        lineas.append(f"• {nombre} = {globals()[nombre]}{alias}\n   {desc}")
    return "\n".join(lineas)


def _tg_set(nombre_txt, valor_txt):
    nombre = resolver_nombre_ajuste(nombre_txt)
    if not nombre:
        tg_enviar(f"❌ No existe el ajuste '{nombre_txt}'. Mira /valores.")
        return
    tipo = AJUSTES_EDITABLES[nombre][0]
    try:
        valor = _convertir_valor(tipo, valor_txt)
    except ValueError as e:
        tg_enviar(f"❌ Valor inválido para {nombre}: {e}")
        return

    if nombre == 'EJECUTAR_ORDENES_REALES' and valor is True and not EJECUTAR_ORDENES_REALES:
        modo = "TESTNET" if USAR_TESTNET else "BINANCE REAL (dinero real)"
        tg_enviar(f"⚠️ Vas a activar el envío de órdenes en {modo}. ¿Confirmas?",
                  [[{'text': '✅ Sí, activar', 'callback_data': 'conf|EJECUTAR_ORDENES_REALES|1'},
                    {'text': '✖️ Cancelar', 'callback_data': 'conf|cancelar|0'}]])
        return

    try:
        aplicar_ajuste(nombre, valor)
    except ValueError as e:
        tg_enviar(f"❌ {e}")
        return
    tg_enviar(f"✅ {nombre} = {globals()[nombre]}")


def _tg_callback(cq):
    partes = cq.get('data', '').split('|')
    msg = cq.get('message') or {}
    chat_id = msg['chat']['id']
    mid = msg['message_id']
    respuesta = ''

    if partes[0] == 'adj' and len(partes) == 3 and partes[1] in AJUSTES_EDITABLES:
        nombre = partes[1]
        tipo = AJUSTES_EDITABLES[nombre][0]
        nuevo = round(globals()[nombre] + float(partes[2]), 4)
        if tipo is int:
            nuevo = int(round(nuevo))
        try:
            aplicar_ajuste(nombre, nuevo)
            respuesta = f"{nombre} = {globals()[nombre]:g}"
        except ValueError as e:
            respuesta = f"❌ {e}"
        texto, botones = panel_ajustes()
        tg_editar(chat_id, mid, texto, botones)

    elif partes[0] == 'set_val' and len(partes) == 3 and partes[1] in AJUSTES_EDITABLES:
        nombre = partes[1]
        valor_fijo = partes[2]
        try:
            aplicar_ajuste(nombre, valor_fijo)
            respuesta = f"{nombre} = {globals()[nombre]:g}"
        except ValueError as e:
            respuesta = f"❌ {e}"
        texto, botones = panel_ajustes()
        tg_editar(chat_id, mid, texto, botones)

    elif partes[0] == 'conf':
        if partes[1] == 'EJECUTAR_ORDENES_REALES' and partes[2] == '1':
            aplicar_ajuste('EJECUTAR_ORDENES_REALES', True)
            tg_editar(chat_id, mid, "✅ EJECUTAR_ORDENES_REALES = True.")
            respuesta = "Activado"
        else:
            tg_editar(chat_id, mid, "✖️ Cancelado.")
            respuesta = "Cancelado"

    _tg_api('answerCallbackQuery', {'callback_query_id': cq['id'], 'text': respuesta[:150]})


def _tg_comando(texto):
    partes = texto.strip().split()
    cmd = partes[0].split('@')[0].lower()
    args = partes[1:]

    if cmd in ('/start', '/ayuda', '/help'):
        tg_enviar(TEXTO_AYUDA)
    elif cmd == '/ajustes':
        t, b = panel_ajustes()
        tg_enviar(t, b)
    elif cmd == '/valores':
        tg_enviar(texto_valores())
    elif cmd == '/set':
        if len(args) != 2:
            tg_enviar("Uso: /set NOMBRE VALOR")
        else:
            _tg_set(args[0], args[1])
    elif cmd in ('/capital', '/riesgo', '/leverage'):
        if len(args) != 1:
            tg_enviar(f"Uso: {cmd} VALOR")
        else:
            _tg_set(cmd[1:], args[0])
    elif cmd == '/pnl':
        tg_enviar(texto_pnl())
    elif cmd == '/posiciones':
        tg_enviar(texto_posiciones())
    elif cmd == '/estado':
        modo = "TESTNET" if USAR_TESTNET else "BINANCE REAL"
        tg_enviar(f"📡 ESTADO\nModo: {modo}\nEnvío de órdenes: {'ACTIVADO' if EJECUTAR_ORDENES_REALES else 'DESACTIVADO'}\n"
                  f"Capital {CAPITAL_DISPONIBLE:g} USDT | Riesgo {RIESGO_PCT:g}% | Leverage {LEVERAGE}x\n\n"
                  f"{texto_posiciones()}\n\n{texto_pnl()}")
    else:
        tg_enviar("Comando no reconocido.")


def _tg_procesar(upd):
    if 'callback_query' in upd:
        cq = upd['callback_query']
        chat_id = (cq.get('message') or {}).get('chat', {}).get('id')
        if _autorizado(chat_id):
            _tg_callback(cq)
        else:
            _tg_api('answerCallbackQuery', {'callback_query_id': cq['id']})
    elif 'message' in upd:
        m = upd['message']
        if not _autorizado(m['chat']['id']):
            return  
        texto = m.get('text', '')
        if texto.startswith('/'):
            _tg_comando(texto)


def hilo_telegram():
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    offset = None
    r = _tg_api('getUpdates', {'offset': -1, 'timeout': 0})
    if r and r.get('result'):
        offset = r['result'][-1]['update_id'] + 1

    aviso_409_mostrado = False

    while True:
        try:
            payload = {'timeout': 20, 'allowed_updates': ['message', 'callback_query']}
            if offset:
                payload['offset'] = offset
            resp = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                                 json=payload, timeout=30).json()
            if not resp.get('ok'):
                if resp.get('error_code') == 409:
                    if not aviso_409_mostrado:
                        print("⚠️ Telegram: liberando la conexión de la corrida anterior...")
                        aviso_409_mostrado = True
                time.sleep(3)
                continue
            if aviso_409_mostrado:
                aviso_409_mostrado = False
            for upd in resp.get('result', []):
                offset = upd['update_id'] + 1
                try:
                    _tg_procesar(upd)
                except Exception as e:
                    print(f"⚠️ Error procesando update: {e}")
        except Exception:
            time.sleep(5)


TIPOS_PNL = ('REALIZED_PNL', 'COMMISSION', 'FUNDING_FEE')


def obtener_income(desde_ms):
    registros = []
    inicio = desde_ms
    while True:
        lote = _binance_signed_request('GET', '/fapi/v1/income',
                                       {'startTime': inicio, 'limit': 1000},
                                       BINANCE_API_KEY, BINANCE_API_SECRET)
        registros.extend(lote)
        if len(lote) < 1000:
            break
        inicio = int(lote[-1]['time']) + 1
    return registros


def calcular_pnl_periodos():
    ahora = datetime.now(TZ_LOCAL)
    hoy0 = ahora.replace(hour=0, minute=0, second=0, microsecond=0)
    semana0 = hoy0 - timedelta(days=hoy0.weekday())
    mes0 = hoy0.replace(day=1)
    desde = min(semana0, mes0)

    totales = {'diario': 0.0, 'semanal': 0.0, 'mensual': 0.0}
    for r in obtener_income(int(desde.timestamp() * 1000)):
        if r.get('incomeType') not in TIPOS_PNL or r.get('asset') != 'USDT':
            continue
        t = datetime.fromtimestamp(int(r['time']) / 1000, TZ_LOCAL)
        v = float(r['income'])
        if t >= hoy0:
            totales['diario'] += v
        if t >= semana0:
            totales['semanal'] += v
        if t >= mes0:
            totales['mensual'] += v
    return totales


def texto_pnl():
    if not (BINANCE_API_KEY and BINANCE_API_SECRET):
        return "📊 PnL no disponible: faltan API keys."
    try:
        p = calcular_pnl_periodos()
    except Exception as e:
        return f"📊 No pude leer el historial: {e}"
    return ("📊 PnL REAL\n"
            f"📅 Hoy:    {p['diario']:+,.2f} USDT\n"
            f"🗓️ Semana: {p['semanal']:+,.2f} USDT\n"
            f"📆 Mes:    {p['mensual']:+,.2f} USDT")


def _pnl_operacion(id_binance, desde_ms):
    total = 0.0
    for r in obtener_income(desde_ms):
        if (r.get('symbol') == id_binance and r.get('incomeType') in TIPOS_PNL
                and r.get('asset') == 'USDT'):
            total += float(r['income'])
    return total


_lock_estado = threading.Lock()
POSICIONES_ACTUALES = {}
ORDENES_PENDIENTES = []


def _cargar_estado():
    try:
        with open(ARCHIVO_ESTADO, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _guardar_estado(estado):
    try:
        with open(ARCHIVO_ESTADO, 'w', encoding='utf-8') as f:
            json.dump(estado, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def _leer_posiciones(exchange):
    res = {}
    for p in exchange.fetch_positions():
        try:
            contratos = float(p.get('contracts') or 0)
        except (TypeError, ValueError):
            contratos = 0.0
        if contratos == 0:
            continue
        sym = p.get('symbol')
        info = p.get('info') or {}
        res[sym] = {
            'symbol': sym,
            'id': info.get('symbol') or exchange.market(sym)['id'],
            'lado': p.get('side') or ('long' if contratos > 0 else 'short'),
            'contratos': abs(contratos),
            'entrada': p.get('entryPrice'),
            'pnl_no_realizado': p.get('unrealizedPnl'),
        }
    return res


def _pendientes_del_bot(exchange):
    pendientes = []
    for o in exchange.fetch_open_orders(params={'subType': 'linear'}):
        if str(o.get('clientOrderId') or '').startswith(PREFIJO_ORDEN) and not o.get('reduceOnly'):
            pendientes.append(o)
    return pendientes


def limpiar_ordenes_simbolo(exchange, symbol, id_binance):
    try:
        exchange.cancel_all_orders(symbol)
    except Exception:
        pass
    try:
        _binance_signed_request('DELETE', '/fapi/v1/algoOpenOrders', {'symbol': id_binance},
                                BINANCE_API_KEY, BINANCE_API_SECRET)
    except Exception:
        pass


def texto_posiciones():
    with _lock_estado:
        pos = dict(POSICIONES_ACTUALES)
        pend = list(ORDENES_PENDIENTES)
    if not pos and not pend:
        return "📌 Sin operaciones abiertas ni órdenes pendientes."
    lineas = []
    if pos:
        lineas.append(f"📌 OPERACIONES ABIERTAS ({len(pos)})")
        for p in pos.values():
            pnl = p.get('pnl_no_realizado')
            pnl_txt = f" | PnL no realizado {float(pnl):+,.2f} USDT" if pnl is not None else ""
            lineas.append(f"• {p['symbol']} {str(p['lado']).upper()} | {p['contratos']:g} contratos | entrada {p['entrada']}{pnl_txt}")
    if pend:
        lineas.append(f"\n⏳ ÓRDENES DE ENTRADA PENDIENTES ({len(pend)})")
        for o in pend:
            lineas.append(f"• {o['symbol']} {str(o['side']).upper()} {o['amount']:g} @ {o['price']}")
    return "\n".join(lineas)


def hilo_monitor(exchange):
    global POSICIONES_ACTUALES, ORDENES_PENDIENTES
    conocidas = _cargar_estado().get('posiciones', {})
    primera = True

    while True:
        try:
            actuales = _leer_posiciones(exchange)   
            ahora_ms = int(time.time() * 1000)

            for sym, p in actuales.items():
                if sym not in conocidas:
                    apertura = ahora_ms if primera else ahora_ms - (INTERVALO_MONITOR_SEG * 2 + 5) * 1000
                    conocidas[sym] = {'id': p['id'], 'lado': p['lado'], 'contratos': p['contratos'],
                                      'entrada': p['entrada'], 'apertura_ms': apertura, 'aprox': primera}
                    cabecera = "📌 POSICIÓN DETECTADA AL INICIAR" if primera else "🟢 OPERACIÓN ABIERTA"
                    tg_enviar(f"{cabecera}\n{sym} {str(p['lado']).upper()}\nContratos: {p['contratos']:g} | Entrada: {p['entrada']}")
                    if EJECUTAR_ORDENES_REALES:
                        colocar_proteccion_pendiente(exchange, sym)

            for sym in list(conocidas):
                if sym not in actuales:
                    info = conocidas.pop(sym)
                    time.sleep(4)  
                    try:
                        pnl_op = _pnl_operacion(info['id'], info['apertura_ms'])
                        pnl_txt = f"{pnl_op:+,.2f} USDT"
                    except Exception as e:
                        pnl_txt = f"no disponible ({e})"
                        pnl_op = 0.0
                    limpiar_ordenes_simbolo(exchange, sym, info['id'])
                    icono = "✅" if pnl_op >= 0 else "❌"
                    tg_enviar(f"{icono} OPERACIÓN CERRADA\n{sym} {str(info['lado']).upper()}\nEntrada: {info['entrada']}\nPnL: {pnl_txt}\n\n{texto_pnl()}")

            _guardar_estado({'posiciones': conocidas})

            pendientes = []
            try:
                pendientes = _pendientes_del_bot(exchange)
                if MINUTOS_MAX_ORDEN_PENDIENTE > 0:
                    for o in list(pendientes):
                        edad_min = (ahora_ms - (o.get('timestamp') or ahora_ms)) / 60000
                        if edad_min >= MINUTOS_MAX_ORDEN_PENDIENTE and o['symbol'] not in actuales:
                            exchange.cancel_order(o['id'], o['symbol'])
                            limpiar_ordenes_simbolo(exchange, o['symbol'], exchange.market(o['symbol'])['id'])
                            with _lock_proteccion:
                                PROTECCION_PENDIENTE.pop(o['symbol'], None)
                            pendientes.remove(o)
                            tg_enviar(f"⌛ ORDEN CANCELADA: {o['symbol']}\nSuperó el límite de {MINUTOS_MAX_ORDEN_PENDIENTE:g} min sin llenarse.")
            except Exception:
                pass

            with _lock_estado:
                POSICIONES_ACTUALES = actuales
                ORDENES_PENDIENTES = pendientes
            primera = False

        except Exception as e:
            print(f"⚠️ Monitor de posiciones: {e}")

        time.sleep(INTERVALO_MONITOR_SEG)


def calcular_entrada(capital, riesgo_pct, precio_entrada, precio_stop, leverage):
    numero_mayor = max(precio_entrada, precio_stop)
    numero_menor = min(precio_entrada, precio_stop)
    diff = numero_mayor - numero_menor

    if numero_menor <= 0:
        return None

    riesgo_fraccion = diff / numero_menor
    if riesgo_fraccion <= 0:
        return None

    perdida_usd = capital * (riesgo_pct / 100)
    capital_a_usar = perdida_usd / riesgo_fraccion
    cantidad_monedas = capital_a_usar / precio_entrada
    margen_necesario = capital_a_usar / leverage

    return {
        'movimiento_pct': riesgo_fraccion * 100,
        'perdida_usd': perdida_usd,
        'capital_a_usar': capital_a_usar,
        'cantidad_monedas': round(cantidad_monedas, 5),
        'margen_necesario': margen_necesario,
    }


def agrupar_precio(precio, paso):
    precision_decimales = max(0, -int(math.floor(math.log10(paso))))
    return round(math.floor(precio / paso) * paso, precision_decimales)


def agrupar_libro_ordenes(orders, paso):
    if paso <= 0:
        return {}

    bloques_agrupados = {}
    for precio, vol in orders:
        bloque = agrupar_precio(precio, paso)
        if bloque not in bloques_agrupados:
            bloques_agrupados[bloque] = {'vol_total': 0.0, 'precio_pico': precio, 'vol_pico': vol}

        bloques_agrupados[bloque]['vol_total'] += vol

        if vol > bloques_agrupados[bloque]['vol_pico']:
            bloques_agrupados[bloque]['vol_pico'] = vol
            bloques_agrupados[bloque]['precio_pico'] = precio

    return bloques_agrupados


def obtener_muro_maximo_volumen(bloques_agrupados):
    if not bloques_agrupados:
        return None, 0.0

    bloque_ganador = max(bloques_agrupados.items(), key=lambda item: item[1]['vol_total'])
    datos = bloque_ganador[1]
    return datos['precio_pico'], datos['vol_total']


def obtener_dos_ultimos_niveles_adaptativos(market_info, precio_referencia):
    paso_base = None

    filters = market_info.get('info', {}).get('filters', [])
    for f in filters:
        if f.get('filterType') == 'PRICE_FILTER':
            paso_base = float(f.get('tickSize', 0))
            break

    if not paso_base or paso_base <= 0:
        tick_size = market_info['precision']['price']
        if isinstance(tick_size, int):
            paso_base = 10 ** (-tick_size)
        else:
            paso_base = float(tick_size)

    niveles_posibles = [
        round(paso_base, 8),
        round(paso_base * 10, 8),
        round(paso_base * 100, 8),
        round(paso_base * 1000, 8)
    ]

    niveles_unicos = sorted(list(set(niveles_posibles)))

    return niveles_unicos[-2], niveles_unicos[-1]


def obtener_solo_perpetuos_usdt(exchange):
    while True:
        try:
            print("🔄 Cargando lista de mercados de Binance...")
            markets = exchange.load_markets()
            perpetuos = []
            for symbol, market in markets.items():
                if (market.get('quote') == 'USDT' and 
                    (market.get('type') == 'swap' or market.get('swap') is True) and 
                    market.get('linear') is True and 
                    market.get('active', True) and 
                    market.get('expiry') is None):
                    perpetuos.append(symbol)
            
            print(f"✅ Se encontraron {len(perpetuos)} contratos PERPETUOS activos en USDT-M.\n")
            return perpetuos
        except Exception:
            print(f"⚠️ Error de conexión al cargar mercados. Reintentando en {PAUSA_ERROR_RED_SEG}s...")
            time.sleep(PAUSA_ERROR_RED_SEG)


def evaluar_y_ejecutar_candidatas(exchange, candidatas_ordenadas, total_ocupado, long_ocupado, short_ocupado):
    ejecutadas = 0
    for i, c in enumerate(candidatas_ordenadas, 1):
        lado = 'LONG' if c['es_long'] else 'SHORT'
        print(f"   #{i} {c['symbol']} {lado} | score={c['score']:.3f} | ratio=1:{c['ratio']:.2f} "
              f"| vol={c['volumen_entrada']:,.0f} | mov_sl={c['pct_movimiento_sl']:.2f}%")

        if total_ocupado >= MAX_OPERACIONES_ABIERTAS:
            print(f"      ⏹️  Cupo total lleno ({total_ocupado}/{MAX_OPERACIONES_ABIERTAS}): no se manda nada más.")
            break

        cupo_ok, motivo = hay_cupo(total_ocupado, long_ocupado, short_ocupado, c['es_long'])
        if not cupo_ok:
            print(f"      ⏭️  Se omite: {motivo}. Se sigue buscando una candidata del otro lado.")
            continue

        if tiene_posicion_u_orden_abierta(exchange, c['symbol']):
            print(f"      ⏭️  Ya hay posición/orden abierta en {c['symbol']}, se omite.")
            continue

        if ejecutar_operacion(exchange, c['symbol'], c['market_info'], c['es_long'],
                              c['precio_entrada'], c['precio_stop'], c['calc']):
            total_ocupado += 1
            ejecutadas += 1
            if c['es_long']:
                long_ocupado += 1
            else:
                short_ocupado += 1

    return ejecutadas, total_ocupado, long_ocupado, short_ocupado


def escanear_perpetuos_binance():
    cargar_ajustes()  

    exchange = ccxt.binance({
        'apiKey': BINANCE_API_KEY,
        'secret': BINANCE_API_SECRET,
        'enableRateLimit': True,
        'options': {
            'defaultType': 'future',
            'warnOnFetchOpenOrdersWithoutSymbol': False,
            'fetchOpenOrders': {
                'warnWithoutSymbol': False,
            },
            'adjustForTimeDifference': True,
            'recvWindow': 10000,
        }
    })
    if USAR_TESTNET:
        exchange.set_sandbox_mode(True)

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        threading.Thread(target=hilo_telegram, daemon=True).start()
        threading.Thread(target=hilo_monitor, args=(exchange,), daemon=True).start()
        modo = "TESTNET" if USAR_TESTNET else "BINANCE REAL"
        tg_enviar(f"🚀 Escáner iniciado ({modo})\n"
                  f"Envío de órdenes: {'ACTIVADO' if EJECUTAR_ORDENES_REALES else 'DESACTIVADO (solo simula)'}\n"
                  "Usa /ayuda para ver los comandos.")
    else:
        print("ℹ️  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID no configurados en config.py.")

    pares = obtener_solo_perpetuos_usdt(exchange)
    print(f"🚀 Escáner iniciado | {len(pares)} Perpetuos USDT | Detección Adaptativa de Volumen")
    print(f"ℹ️  {platform.system()} | Python {sys.version.split()[0]} | ccxt {ccxt.__version__} | "
          f"{'TESTNET' if USAR_TESTNET else 'BINANCE REAL'} | órdenes reales: {'SÍ' if EJECUTAR_ORDENES_REALES else 'NO'} | "
          f"cupo máx: {MAX_OPERACIONES_ABIERTAS}")
    print("=" * 75)
    
    while True:
        try:
            cupo_verificado, total_ocupado, long_ocupado, short_ocupado, detalle = contar_posiciones_por_lado(exchange)
            if not cupo_verificado:
                print(f"🟡 [{time.strftime('%H:%M:%S')}] No se pudo verificar el cupo con seguridad. "
                      f"Se espera {SEGUNDOS_ESPERA_CUPO_LLENO:g}s y se reintenta.")
                time.sleep(SEGUNDOS_ESPERA_CUPO_LLENO)
                continue
            if total_ocupado >= MAX_OPERACIONES_ABIERTAS:
                print(f"🟡 [{time.strftime('%H:%M:%S')}] Cupo lleno ({total_ocupado}/{MAX_OPERACIONES_ABIERTAS}): "
                      f"{', '.join(detalle) if detalle else 'sin detalle'}. "
                      f"En espera, gestionando operaciones abiertas... (revisa en {SEGUNDOS_ESPERA_CUPO_LLENO:g}s)")
                time.sleep(SEGUNDOS_ESPERA_CUPO_LLENO)
                continue
            print(f"🟢 [{time.strftime('%H:%M:%S')}] Cupo {total_ocupado}/{MAX_OPERACIONES_ABIERTAS} "
                  f"(LONG {long_ocupado}, SHORT {short_ocupado}): "
                  f"{', '.join(detalle) if detalle else 'nada abierto'}. "
                  f"Escaneando para llenar {MAX_OPERACIONES_ABIERTAS - total_ocupado} cupo(s)...")

            alerta_encontrada = False
            señales_candidatas = []
            
            for symbol in pares:
                try:
                    market_info = exchange.market(symbol)

                    order_book = exchange.fetch_order_book(symbol, limit=500)
                    bids = order_book['bids']
                    asks = order_book['asks']
                    
                    if not bids or not asks:
                        continue

                    precio_ref = (bids[0][0] + asks[0][0]) / 2.0

                    paso_penultimo, paso_ultimo = obtener_dos_ultimos_niveles_adaptativos(market_info, precio_ref)

                    bids_penultimo = agrupar_libro_ordenes(bids, paso_penultimo)
                    asks_penultimo = agrupar_libro_ordenes(asks, paso_penultimo)

                    compra_1, vol_c1 = obtener_muro_maximo_volumen(bids_penultimo)
                    venta_1, vol_v1 = obtener_muro_maximo_volumen(asks_penultimo)

                    if not compra_1 or not venta_1 or compra_1 >= venta_1:
                        continue

                    bids_ultimo = agrupar_libro_ordenes(bids, paso_ultimo)
                    asks_ultimo = agrupar_libro_ordenes(asks, paso_ultimo)

                    bids_ult_filtrados = {p: v for p, v in bids_ultimo.items() if p < compra_1}
                    asks_ult_filtrados = {p: v for p, v in asks_ultimo.items() if p > venta_1}

                    compra_2, vol_c2 = obtener_muro_maximo_volumen(bids_ult_filtrados)
                    venta_2, vol_v2 = obtener_muro_maximo_volumen(asks_ult_filtrados)

                    hora_actual = time.strftime('%H:%M:%S')

                    # ==========================================
                    # EVALUACIÓN LONG
                    # ==========================================
                    if compra_2 and compra_2 < compra_1:
                        distancia_tp = venta_1 - compra_1
                        distancia_sl = compra_1 - compra_2
                        
                        if distancia_sl > 0:
                            ratio_long = distancia_tp / distancia_sl
                            if ratio_long >= MIN_RATIO:
                                pct_tp = (distancia_tp / compra_1) * 100
                                pct_sl = (distancia_sl / compra_1) * 100
                                base_currency = symbol.split('/')[0]
                                
                                print(f"\n🟢 [{hora_actual}] ¡ALERTA LONG: {symbol}!")
                                print(f"   ▸ Pasos: Penúltimo ({paso_penultimo}) | Último ({paso_ultimo})")
                                print(f"   ▸ Entrada (Pico Bid N1): {compra_1} USDT | Vol: {vol_c1:,.0f} {base_currency}")
                                print(f"   ▸ TP (Pico Ask N1):      {venta_1} USDT (+{round(pct_tp, 2)}%) | Vol: {vol_v1:,.0f} {base_currency}")
                                print(f"   ▸ SL (Pico Bid N2):      {compra_2} USDT (-{round(pct_sl, 2)}%) | Vol: {vol_c2:,.0f} {base_currency}")
                                print(f"   🎯 Ratio R:R: 1:{round(ratio_long, 2)}")

                                calc = calcular_entrada(CAPITAL_DISPONIBLE, RIESGO_PCT, compra_1, compra_2, LEVERAGE)
                                if calc:
                                    print(f"   💰 Capital a usar:   {calc['capital_a_usar']:,.2f} USDT (riesgo: {calc['perdida_usd']:,.2f} USDT, movimiento SL: {calc['movimiento_pct']:.2f}%)")
                                    print(f"   💰 Cantidad {base_currency}: {calc['cantidad_monedas']}")
                                    print(f"   💰 Margen necesario ({LEVERAGE}x): {calc['margen_necesario']:,.2f} USDT")
                                    print(f"   📝 Registrada como candidata, se evalúa junto a las demás al final del ciclo.\n")

                                    señales_candidatas.append({
                                        'symbol': symbol,
                                        'market_info': market_info,
                                        'es_long': True,
                                        'precio_entrada': compra_1,
                                        'precio_stop': compra_2,
                                        'ratio': ratio_long,
                                        'volumen_entrada': vol_c1,
                                        'pct_movimiento_sl': pct_sl,
                                        'calc': calc,
                                    })
                                else:
                                    print()

                                alerta_encontrada = True

                    # ==========================================
                    # EVALUACIÓN SHORT
                    # ==========================================
                    if venta_2 and venta_2 > venta_1:
                        distancia_tp_s = venta_1 - compra_1
                        distancia_sl_s = venta_2 - venta_1
                        
                        if distancia_sl_s > 0:
                            ratio_short = distancia_tp_s / distancia_sl_s
                            if ratio_short >= MIN_RATIO:
                                pct_tp_s = (distancia_tp_s / venta_1) * 100
                                pct_sl_s = (distancia_sl_s / venta_1) * 100
                                base_currency = symbol.split('/')[0]
                                
                                print(f"\n🔴 [{hora_actual}] ¡ALERTA SHORT: {symbol}!")
                                print(f"   ▸ Pasos: Penúltimo ({paso_penultimo}) | Último ({paso_ultimo})")
                                print(f"   ▸ Entrada (Pico Ask N1): {venta_1} USDT | Vol: {vol_v1:,.0f} {base_currency}")
                                print(f"   ▸ TP (Pico Bid N1):      {compra_1} USDT (-{round(pct_tp_s, 2)}%) | Vol: {vol_c1:,.0f} {base_currency}")
                                print(f"   ▸ SL (Pico Ask N2):      {venta_2} USDT (+{round(pct_sl_s, 2)}%) | Vol: {vol_v2:,.0f} {base_currency}")
                                print(f"   🎯 Ratio R:R: 1:{round(ratio_short, 2)}")

                                calc = calcular_entrada(CAPITAL_DISPONIBLE, RIESGO_PCT, venta_1, venta_2, LEVERAGE)
                                if calc:
                                    print(f"   💰 Capital a usar:   {calc['capital_a_usar']:,.2f} USDT (riesgo: {calc['perdida_usd']:,.2f} USDT, movimiento SL: {calc['movimiento_pct']:.2f}%)")
                                    print(f"   💰 Cantidad {base_currency}: {calc['cantidad_monedas']}")
                                    print(f"   💰 Margen necesario ({LEVERAGE}x): {calc['margen_necesario']:,.2f} USDT")
                                    print(f"   📝 Registrada como candidata, se evalúa junto a las demás al final del ciclo.\n")

                                    señales_candidatas.append({
                                        'symbol': symbol,
                                        'market_info': market_info,
                                        'es_long': False,
                                        'precio_entrada': venta_1,
                                        'precio_stop': venta_2,
                                        'ratio': ratio_short,
                                        'volumen_entrada': vol_v1,
                                        'pct_movimiento_sl': pct_sl_s,
                                        'calc': calc,
                                    })
                                else:
                                    print()

                                alerta_encontrada = True

                    time.sleep(PAUSA_ENTRE_PARES_SEG)

                except Exception:
                    continue

            hora_fin = time.strftime('%H:%M:%S')
            if not alerta_encontrada:
                print(f"⏳ [{hora_fin}] Ciclo finalizado sin señales que superen el criterio 1:{MIN_RATIO}.")
            else:
                candidatas_ordenadas = ordenar_candidatas_por_score(señales_candidatas)
                print(f"\n📊 [{hora_fin}] Ciclo finalizado con {len(candidatas_ordenadas)} candidata(s), evaluando por score...")

                cupo_verificado, total_ocupado, long_ocupado, short_ocupado, detalle = contar_posiciones_por_lado(exchange)
                if not cupo_verificado:
                    print("   ⚠️  No se pudo reverificar el cupo antes de ejecutar; cancelando...")
                else:
                    print(f"   🔎 Cupo antes de ejecutar: {total_ocupado}/{MAX_OPERACIONES_ABIERTAS} "
                          f"(LONG {long_ocupado}, SHORT {short_ocupado}): "
                          f"{', '.join(detalle) if detalle else 'nada abierto'}")
                    ejecutadas, total_ocupado, long_ocupado, short_ocupado = evaluar_y_ejecutar_candidatas(
                        exchange, candidatas_ordenadas, total_ocupado, long_ocupado, short_ocupado)
                    print(f"   ✅ {ejecutadas} operación(es) ejecutada(s) este ciclo (cupo: {total_ocupado}/{MAX_OPERACIONES_ABIERTAS}).")

            print()

        except Exception as e:
            hora_err = time.strftime('%H:%M:%S')
            if isinstance(e, (ccxt.NetworkError, requests.exceptions.RequestException)):
                print(f"\n📡 [{hora_err}] Error de red ({type(e).__name__}: {e}). Reintentando en {PAUSA_ERROR_RED_SEG}s...")
            else:
                print(f"\n❌ [{hora_err}] ERROR INESPERADO ({type(e).__name__}: {e}). Reintentando...")
                traceback.print_exc()
            time.sleep(PAUSA_ERROR_RED_SEG)

if __name__ == "__main__":
    escanear_perpetuos_binance()
