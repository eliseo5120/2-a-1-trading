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
MIN_RATIO = 2.0                   # Ratio mínimo R:R (1:2) — INAMOVIBLE
PAUSA_ENTRE_PARES_SEG = 0.04      # Pausa entre pares en segundos
PAUSA_ERROR_RED_SEG = 10          # Pausa si se cae la red

# ------------------------------------------------------------------------------
# MODO REVERSIÓN EXTREMA Y FILTRO DE SOBREEXTENSIÓN
# ------------------------------------------------------------------------------
MODO_REVERSION_EXTREMA = True     # True = Caza extremos alejados de la SMA para retroceso en muro
SMA_CORTA_PERIODO = 20            # Periodo SMA corta
SMA_LARGA_PERIODO = 200           # Periodo SMA larga
SOBREEXTENSION_MIN_PCT = 3.5      # % Mínimo de distancia a la SMA 20 para activar reversión
TIMEFRAME_SMA = '1h'              # Temporalidad para calcular las SMA ('15m', '1h', etc.)
VOLUMEN_MINIMO_24H_USDT = 5_000_000  # Volumen mínimo en USDT en 24h para operar un par

# ------------------------------------------------------------------------------
# CALCULADORA DE ENTRADAS (capital / riesgo / leverage)
# ------------------------------------------------------------------------------
CAPITAL_DISPONIBLE = 20.0        # Capital disponible en USDT
RIESGO_PCT = 5                   # % del capital que se arriesga por operación
LEVERAGE = 10                     # Apalancamiento
# ==============================================================================

# ------------------------------------------------------------------------------
# EJECUCIÓN DE ÓRDENES EN BINANCE
# ------------------------------------------------------------------------------
EJECUTAR_ORDENES_REALES = True    # True = Manda órdenes a Binance
USAR_TESTNET = False                # False = Binance real

ACTIVACION_TRAILING_R = 1.8        # Trailing se activa a 1.8R para dejar correr el TP
MAX_OPERACIONES_ABIERTAS = 4       # Operaciones simultáneas configuradas a 4
SEGUNDOS_ESPERA_CUPO_LLENO = 30    
MINUTOS_MAX_ORDEN_PENDIENTE = 720  

# ------------------------------------------------------------------------------
# RANKING DE SEÑALES
# ------------------------------------------------------------------------------
PESO_RATIO = 0.5             
PESO_VOLUMEN = 0.3           
PESO_MOVIMIENTO_SL = 0.2     

try:
    from config import BINANCE_API_KEY, BINANCE_API_SECRET
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

USUARIO_ESPERANDO_DATO = {}  
PROTECCIONES_ENVIADAS = set()  


def _ruta(nombre):
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), nombre)


ARCHIVO_AJUSTES = _ruta("ajustes_bot.json")   
ARCHIVO_ESTADO = _ruta("estado_bot.json")     
ARCHIVO_HISTORIAL = _ruta("historial_rendimiento.json")

_lock_historial = threading.Lock()


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
        raise Exception(f"Binance API error {resp.status_code}: {data}")
    return data


TIPOS_PNL = ('REALIZED_PNL', 'COMMISSION', 'FUNDING_FEE')


def obtener_income(desde_ms=0):
    registros = []
    inicio = desde_ms
    try:
        while True:
            lote = _binance_signed_request('GET', '/fapi/v1/income',
                                           {'startTime': inicio, 'limit': 1000},
                                           BINANCE_API_KEY, BINANCE_API_SECRET)
            if not lote:
                break
            registros.extend(lote)
            if len(lote) < 1000:
                break
            inicio = int(lote[-1]['time']) + 1
    except Exception:
        pass
    return registros


def registrar_operacion_historial(datos):
    with _lock_historial:
        historial = []
        if os.path.exists(ARCHIVO_HISTORIAL):
            try:
                with open(ARCHIVO_HISTORIAL, 'r', encoding='utf-8') as f:
                    historial = json.load(f)
            except Exception:
                historial = []
        
        historial.append(datos)
        try:
            with open(ARCHIVO_HISTORIAL, 'w', encoding='utf-8') as f:
                json.dump(historial, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"⚠️ No se pudo guardar en el historial: {e}")


def resumen_metricas():
    with _lock_historial:
        if not os.path.exists(ARCHIVO_HISTORIAL):
            return "📊 Sin operaciones registradas en el historial aún."
        try:
            with open(ARCHIVO_HISTORIAL, 'r', encoding='utf-8') as f:
                historial = json.load(f)
        except Exception as e:
            return f"⚠️ Error leyendo historial: {e}"

    if not historial:
        return "📊 Sin operaciones registradas en el historial aún."

    total_ops = len(historial)
    ganadoras = sum(1 for op in historial if op.get('pnl', 0) > 0)
    perdedoras = sum(1 for op in historial if op.get('pnl', 0) <= 0)
    pnl_total = sum(op.get('pnl', 0) for op in historial)
    winrate = (ganadoras / total_ops) * 100 if total_ops > 0 else 0.0

    return (
        "📈 *MÉTRICAS DE RENDIMIENTO*\n"
        "───────────────────────────────\n"
        f"🔢 Total Operaciones: {total_ops}\n"
        f"✅ Ganadas: {ganadoras} | ❌ Perdidas: {perdedoras}\n"
        f"🎯 Win Rate: {winrate:.1f}%\n"
        f"💵 PnL Acumulado: {pnl_total:+,.2f} USDT\n"
        "───────────────────────────────"
    )


# ==============================================================================
# FILTRO DE REVERSIÓN EXTREMA Y SOBREEXTENSIÓN A LA SMA
# ==============================================================================
def validar_reversion_sma(exchange, symbol, es_long, precio_actual):
    if not MODO_REVERSION_EXTREMA:
        return True, "Modo Reversión Extrema desactivado"
    
    try:
        limit_velas = SMA_LARGA_PERIODO + 10
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME_SMA, limit=limit_velas)
        if len(ohlcv) < SMA_LARGA_PERIODO:
            return False, f"insuficientes velas para calcular SMA"
        
        cierres = [x[4] for x in ohlcv]
        sma_20 = sum(cierres[-SMA_CORTA_PERIODO:]) / SMA_CORTA_PERIODO
        
        distancia_sma_pct = ((precio_actual - sma_20) / sma_20) * 100

        if es_long:
            if distancia_sma_pct <= -SOBREEXTENSION_MIN_PCT:
                return True, f"LONG Reversión verificado | Precio sobreextendido abajo de SMA20 ({distancia_sma_pct:.2f}%)"
            else:
                return False, f"LONG rechazado: No suficientemente sobreextendido ({distancia_sma_pct:.2f}% vs req <= -{SOBREEXTENSION_MIN_PCT}%)"
        else:
            if distancia_sma_pct >= SOBREEXTENSION_MIN_PCT:
                return True, f"SHORT Reversión verificado | Precio sobreextendido arriba de SMA20 (+{distancia_sma_pct:.2f}%)"
            else:
                return False, f"SHORT rechazado: No suficientemente sobreextendido ({distancia_sma_pct:.2f}% vs req >= +{SOBREEXTENSION_MIN_PCT}%)"

    except Exception as e:
        return False, f"error al calcular reversión SMA ({e})"


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
        print("   ⚠️  No se pudo calcular el trailing. Se omite la operación.")
        return False

    try:
        cantidad_ok, precio_ok, error = validar_precision_y_notional(exchange, symbol, market_info, cantidad, precio_entrada)
    except Exception as e:
        cantidad_ok, precio_ok, error = None, None, f"no se pudo validar contra el exchange ({e})"

    print(f"   🤖 Plan de ejecución Reversión Muro: LIMIT {lado_entrada.upper()} {cantidad_ok} {market_info['base']} @ {precio_entrada}")
    print(f"   🤖 SL: {precio_stop}")
    print(f"   🤖 Trailing protector → activación {trailing['activation_price']:.8f} | callback {trailing['callback_rate_pct_ajustado']}%")

    if error:
        print(f"   ⏭️  Se omite {symbol}: {error}.\n")
        return False

    if not EJECUTAR_ORDENES_REALES:
        print("   🔒 EJECUTAR_ORDENES_REALES=False → no se mandó ninguna orden real.\n")
        return True

    if not BINANCE_API_KEY or not BINANCE_API_SECRET:
        print("   ❌ Faltan BINANCE_API_KEY / BINANCE_API_SECRET en config.py.\n")
        return False

    try:
        exchange.set_leverage(LEVERAGE, symbol)
        client_id = f"rev{int(time.time() * 1000)}"
        orden_entrada = exchange.create_order(symbol, 'limit', lado_entrada, cantidad_ok, precio_ok,
                                              params={'clientOrderId': client_id})
        print(f"   ✅ Orden de entrada en muro enviada. id={orden_entrada.get('id')}")
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
            'cantidad_str': exchange.amount_to_precision(symbol, cantidad_ok),
        }
    print(f"   ⏳ SL/Trailing pendientes de activación automática al llenarse la entrada.\n")
    tg_enviar(f"📥 ENTRADA REVERSIÓN EN MURO: {symbol} {'LONG' if es_long else 'SHORT'}\n"
              f"LIMIT: {precio_ok} | Cantidad: {cantidad_ok}")

    return True


def verificar_proteccion_activa(exchange, symbol, simbolo_binance):
    if symbol in PROTECCIONES_ENVIADAS:
        return True

    try:
        resp_algo = _binance_signed_request('GET', '/fapi/v1/openAlgoOrders', {'symbol': simbolo_binance}, BINANCE_API_KEY, BINANCE_API_SECRET)
        ordenes_algo = resp_algo.get('data', []) if isinstance(resp_algo, dict) else resp_algo
        return len(ordenes_algo) > 0
    except Exception:
        return symbol in PROTECCIONES_ENVIADAS


def colocar_proteccion_pendiente(exchange, symbol, simbolo_binance=None):
    if symbol in PROTECCIONES_ENVIADAS:
        return True

    with _lock_proteccion:
        info = PROTECCION_PENDIENTE.get(symbol)

    if not info and simbolo_binance:
        try:
            posiciones = exchange.fetch_positions([symbol])
            for p in posiciones:
                contratos = float(p.get('contracts') or 0)
                if contratos != 0:
                    lado_cierre = 'SELL' if contratos > 0 else 'BUY'
                    entrada = float(p.get('entryPrice'))
                    stop_estimado = entrada * 0.98 if contratos > 0 else entrada * 1.02
                    trailing = calcular_trailing_protector(entrada, stop_estimado)
                    info = {
                        'simbolo_binance': simbolo_binance,
                        'lado_cierre': lado_cierre,
                        'precio_stop': stop_estimado,
                        'activation_price': trailing['activation_price'],
                        'callback_rate': trailing['callback_rate_pct_ajustado'],
                        'protegido_1_1': trailing['protegido_1_1'],
                        'cantidad_str': exchange.amount_to_precision(symbol, abs(contratos)),
                    }
                    break
        except Exception as e:
            print(f"   ⚠️ Error reconstruyendo protección para {symbol}: {e}")

    if not info:
        return False

    sl_exitoso = False
    try:
        _binance_signed_request('POST', '/fapi/v1/algoOrder', {
            'algoType': 'CONDITIONAL',
            'symbol': info['simbolo_binance'],
            'side': info['lado_cierre'],
            'type': 'STOP_MARKET',
            'triggerPrice': exchange.price_to_precision(symbol, info['precio_stop']),
            'quantity': info['cantidad_str'],
            'reduceOnly': 'true',
            'timeInForce': 'GTC',
            'workingType': 'CONTRACT_PRICE',
            'priceProtect': 'true',
        }, BINANCE_API_KEY, BINANCE_API_SECRET)
        sl_exitoso = True
    except Exception:
        sl_exitoso = True

    trailing_exitoso = False
    try:
        _binance_signed_request('POST', '/fapi/v1/algoOrder', {
            'algoType': 'CONDITIONAL',
            'symbol': info['simbolo_binance'],
            'side': info['lado_cierre'],
            'type': 'TRAILING_STOP_MARKET',
            'quantity': info['cantidad_str'],
            'activatePrice': exchange.price_to_precision(symbol, info['activation_price']),
            'callbackRate': info['callback_rate'],
            'workingType': 'CONTRACT_PRICE',
            'reduceOnly': 'true',
        }, BINANCE_API_KEY, BINANCE_API_SECRET)
        trailing_exitoso = True
    except Exception:
        pass

    if sl_exitoso or trailing_exitoso:
        PROTECCIONES_ENVIADAS.add(symbol)
        with _lock_proteccion:
            PROTECCION_PENDIENTE.pop(symbol, None)
        return True
    
    return False


PREFIJO_ORDEN = "rev"   


AJUSTES_EDITABLES = {
    'CAPITAL_DISPONIBLE': (float, 1, 1_000_000, "Capital disponible (USDT)"),
    'RIESGO_PCT': (float, 0.1, 50, "% del capital que se arriesga por operación"),
    'LEVERAGE': (int, 1, 125, "Apalancamiento"),
    'MAX_OPERACIONES_ABIERTAS': (int, 1, 20, "Operaciones simultáneas máximas"),
    'MIN_RATIO': (float, 0.5, 100, "Ratio R:R mínimo (2 = 1:2)"),
    'ACTIVACION_TRAILING_R': (float, 1.05, 10, "Activación del trailing en múltiplos de R"),
    'MINUTOS_MAX_ORDEN_PENDIENTE': (float, 0, 1440, "Minutos antes de cancelar orden límite (0 = nunca)"),
    'SOBREEXTENSION_MIN_PCT': (float, 1.0, 30.0, "% Distancia mínima sobreextensión SMA"),
    'VOLUMEN_MINIMO_24H_USDT': (float, 0, 1_000_000_000, "Volumen mínimo en 24h (USDT)"),
    'MODO_REVERSION_EXTREMA': (bool, None, None, "Modo reversión extrema (true/false)"),
    'EJECUTAR_ORDENES_REALES': (bool, None, None, "Mandar órdenes reales (true/false)"),
}

ALIAS_AJUSTES = {
    'capital': 'CAPITAL_DISPONIBLE', 'riesgo': 'RIESGO_PCT', 'leverage': 'LEVERAGE',
    'apalancamiento': 'LEVERAGE', 'operaciones': 'MAX_OPERACIONES_ABIERTAS',
    'ratio': 'MIN_RATIO', 'trailing': 'ACTIVACION_TRAILING_R',
    'pendiente': 'MINUTOS_MAX_ORDEN_PENDIENTE', 'sobreextension': 'SOBREEXTENSION_MIN_PCT',
    'volumen': 'VOLUMEN_MINIMO_24H_USDT', 'reversion': 'MODO_REVERSION_EXTREMA',
    'ejecutar': 'EJECUTAR_ORDENES_REALES',
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


MENU_TECLADO_FIJO = {
    'keyboard': [
        [{'text': '⚙️ Ajustes'}, {'text': '📌 Posiciones'}],
        [{'text': '📊 PnL'}, {'text': '📈 Métricas'}],
        [{'text': '📡 Estado'}]
    ],
    'resize_keyboard': True,
    'persistent': True
}


def tg_enviar(texto, botones=None):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': texto, 'parse_mode': 'Markdown'}
    if botones:
        payload['reply_markup'] = {'inline_keyboard': botones}
    else:
        payload['reply_markup'] = MENU_TECLADO_FIJO
    _tg_api('sendMessage', payload)


def _autorizado(chat_id):
    return TELEGRAM_CHAT_ID is not None and str(chat_id) == str(TELEGRAM_CHAT_ID)


def panel_ajustes():
    riesgo_usd = CAPITAL_DISPONIBLE * RIESGO_PCT / 100
    
    texto = (
        "⚙️ *VALORES CONFIGURADOS (MODO REVERSIÓN)*\n"
        "───────────────────────────────\n"
        f"💰 Capital Disponible: {CAPITAL_DISPONIBLE:g} USDT\n"
        f"⚠️ Riesgo por Operación: {RIESGO_PCT:g}% (≈ {riesgo_usd:.2f} USDT)\n"
        f"⚙️ Apalancamiento: {LEVERAGE}x\n"
        f"📊 Operaciones Simultáneas Máx: {MAX_OPERACIONES_ABIERTAS}\n"
        f"📈 Volumen Mínimo 24h: {VOLUMEN_MINIMO_24H_USDT:,.0f} USDT\n"
        f"🔄 Modo Reversión Extrema: {'ACTIVADO' if MODO_REVERSION_EXTREMA else 'DESACTIVADO'}\n"
        f"📐 Sobreextensión Mínima: {SOBREEXTENSION_MIN_PCT:g}%\n"
        f"🎯 Activación Trailing: {ACTIVACION_TRAILING_R}R\n"
        "───────────────────────────────\n"
        "Toca un botón para cambiar el parámetro:"
    )
    
    botones = [
        [{'text': '💰 Cambiar Capital', 'callback_data': 'pedir|CAPITAL_DISPONIBLE'},
         {'text': '⚠️ Cambiar Riesgo %', 'callback_data': 'pedir|RIESGO_PCT'}],
        [{'text': '📐 Sobreextensión %', 'callback_data': 'pedir|SOBREEXTENSION_MIN_PCT'},
         {'text': '🔄 Reversión True/False', 'callback_data': 'pedir|MODO_REVERSION_EXTREMA'}],
    ]
    return texto, botones


def _tg_callback(cq):
    partes = cq.get('data', '').split('|')
    msg = cq.get('message') or {}
    chat_id = msg['chat']['id']
    respuesta = ''

    if partes[0] == 'pedir' and len(partes) == 2 and partes[1] in AJUSTES_EDITABLES:
        var_nombre = partes[1]
        USUARIO_ESPERANDO_DATO[chat_id] = var_nombre
        _, minimo, maximo, desc = AJUSTES_EDITABLES[var_nombre]
        tg_enviar(f"✍️ Escribe el nuevo número para *{var_nombre}* ({desc}):")
        respuesta = f"Esperando valor"

    _tg_api('answerCallbackQuery', {'callback_query_id': cq['id'], 'text': respuesta[:150]})


def _tg_set_directo(nombre_txt, valor_txt):
    nombre = resolver_nombre_ajuste(nombre_txt)
    if not nombre:
        tg_enviar(f"❌ Parámetro desconocido.")
        return
    tipo = AJUSTES_EDITABLES[nombre][0]
    try:
        valor = _convertir_valor(tipo, valor_txt)
        aplicar_ajuste(nombre, valor)
        tg_enviar(f"✅ Guardado: {nombre} = {globals()[nombre]}")
    except ValueError as e:
        tg_enviar(f"❌ Error: {e}")


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
        chat_id = m['chat']['id']
        if not _autorizado(chat_id):
            return  
        texto = m.get('text', '').strip()

        if chat_id in USUARIO_ESPERANDO_DATO:
            var_nombre = USUARIO_ESPERANDO_DATO.pop(chat_id)
            _tg_set_directo(var_nombre, texto)
            t, b = panel_ajustes()
            tg_enviar(t, b)
            return

        if texto in ('⚙️ Ajustes', '/ajustes'):
            t, b = panel_ajustes()
            tg_enviar(t, b)
        elif texto in ('📌 Posiciones', '/posiciones'):
            tg_enviar(texto_posiciones())
        elif texto in ('📊 PnL', '/pnl'):
            tg_enviar(texto_pnl())
        elif texto in ('📈 Métricas', '/metricas'):
            tg_enviar(resumen_metricas())
        elif texto in ('📡 Estado', '/estado'):
            tg_enviar(f"📡 *ESTADO DEL BOT*\nCapital {CAPITAL_DISPONIBLE:g} USDT | Riesgo {RIESGO_PCT:g}%\n"
                      f"Reversión Extrema: {'ON' if MODO_REVERSION_EXTREMA else 'OFF'} ({SOBREEXTENSION_MIN_PCT:g}%)\n\n"
                      f"{texto_posiciones()}\n\n{texto_pnl()}")


def hilo_telegram():
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    offset = None
    while True:
        try:
            resp = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                                 json={'timeout': 20, 'offset': offset}, timeout=30).json()
            for upd in resp.get('result', []):
                offset = upd['update_id'] + 1
                _tg_procesar(upd)
        except Exception:
            time.sleep(5)


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
        return "📊 PnL no disponible."
    try:
        p = calcular_pnl_periodos()
    except Exception as e:
        return f"📊 Error: {e}"
    return ("📊 *PnL REAL*\n"
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


def _leer_posiciones(exchange):
    res = {}
    for p in exchange.fetch_positions():
        try:
            contratos = float(p.get('contracts') or 0)
        except Exception:
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
        return "📌 Sin operaciones abiertas ni pendientes."
    lineas = []
    if pos:
        lineas.append(f"📌 *ABIERTAS ({len(pos)})*")
        for p in pos.values():
            lineas.append(f"• {p['symbol']} {p['lado'].upper()} | {p['contratos']:g} @ {p['entrada']}")
    if pend:
        lineas.append(f"\n⏳ *PENDIENTES MODO REVERSIÓN ({len(pend)})*")
        for o in pend:
            lineas.append(f"• {o['symbol']} {o['side'].upper()} {o['amount']:g} @ {o['price']}")
    return "\n".join(lineas)


def hilo_monitor(exchange):
    global POSICIONES_ACTUALES, ORDENES_PENDIENTES
    conocidas = {}
    primera = True

    while True:
        try:
            actuales = _leer_posiciones(exchange)
            ahora_ms = int(time.time() * 1000)

            for sym, p in actuales.items():
                if sym not in conocidas:
                    apertura = ahora_ms if primera else ahora_ms - 60000
                    conocidas[sym] = {'id': p['id'], 'lado': p['lado'], 'contratos': p['contratos'],
                                      'entrada': p['entrada'], 'apertura_ms': apertura}
                    tg_enviar(f"🟢 OPERACIÓN REVERSIÓN ABIERTA\n{sym} {p['lado'].upper()} @ {p['entrada']}")
                
                if EJECUTAR_ORDENES_REALES:
                    if not verificar_proteccion_activa(exchange, sym, p['id']):
                        colocar_proteccion_pendiente(exchange, sym, p['id'])

            for sym in list(conocidas):
                if sym not in actuales:
                    info = conocidas.pop(sym)
                    PROTECCIONES_ENVIADAS.discard(sym)
                    time.sleep(4)
                    try:
                        pnl_op = _pnl_operacion(info['id'], info['apertura_ms'])
                    except Exception:
                        pnl_op = 0.0
                    
                    limpiar_ordenes_simbolo(exchange, sym, info['id'])
                    icono = "✅" if pnl_op >= 0 else "❌"
                    
                    registrar_operacion_historial({
                        'timestamp': datetime.now(TZ_LOCAL).isoformat(),
                        'symbol': sym,
                        'lado': info['lado'],
                        'pnl': pnl_op
                    })
                    tg_enviar(f"{icono} OPERACIÓN CERRADA\n{sym} PnL: {pnl_op:+,.2f} USDT\n\n{texto_pnl()}")

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
                            tg_enviar(f"⌛ ORDEN CANCELADA: {o['symbol']}\nSuperó el tiempo límite en el muro.")
            except Exception:
                pass

            with _lock_estado:
                POSICIONES_ACTUALES = actuales
                ORDENES_PENDIENTES = pendientes
            primera = False
        except Exception as e:
            print(f"⚠️ Monitor: {e}")
        time.sleep(INTERVALO_MONITOR_SEG)


def calcular_entrada(capital, riesgo_pct, precio_entrada, precio_stop, leverage):
    diff = abs(precio_entrada - precio_stop)
    if precio_stop <= 0 or diff <= 0:
        return None
    riesgo_fraccion = diff / precio_entrada
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


def obtener_dos_ultimos_niveles_adaptativos(market_info):
    paso_base = 0.0001
    for f in market_info.get('info', {}).get('filters', []):
        if f.get('filterType') == 'PRICE_FILTER':
            paso_base = float(f.get('tickSize', 0.0001))
            break
    niveles = [round(paso_base, 8), round(paso_base * 10, 8), round(paso_base * 100, 8)]
    niveles = sorted(list(set(niveles)))
    return niveles[-2], niveles[-1]


def obtener_solo_perpetuos_usdt(exchange):
    while True:
        try:
            markets = exchange.load_markets()
            perpetuos = [s for s, m in markets.items() if m.get('quote') == 'USDT' and m.get('swap') and m.get('active', True)]
            return perpetuos
        except Exception:
            time.sleep(PAUSA_ERROR_RED_SEG)


def evaluar_y_ejecutar_candidatas(exchange, candidatas_ordenadas, total_ocupado, long_ocupado, short_ocupado):
    ejecutadas = 0
    for i, c in enumerate(candidatas_ordenadas, 1):
        if total_ocupado >= MAX_OPERACIONES_ABIERTAS:
            break
        cupo_ok, _ = hay_cupo(total_ocupado, long_ocupado, short_ocupado, c['es_long'])
        if not cupo_ok:
            continue
        if tiene_posicion_u_orden_abierta(exchange, c['symbol']):
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


def hay_cupo(total_ocupado, long_ocupado, short_ocupado, es_long):
    if total_ocupado >= MAX_OPERACIONES_ABIERTAS:
        return False, "lleno"
    max_por_lado = math.ceil(MAX_OPERACIONES_ABIERTAS / 2)
    ocupado_lado = long_ocupado if es_long else short_ocupado
    if ocupado_lado >= max_por_lado:
        return False, "lado lleno"
    return True, None


def escanear_perpetuos_binance():
    cargar_ajustes()  
    exchange = ccxt.binance({
        'apiKey': BINANCE_API_KEY, 'secret': BINANCE_API_SECRET,
        'enableRateLimit': True,
        'options': {
            'defaultType': 'future',
            'warnOnFetchOpenOrdersWithoutSymbol': False,
            'fetchOpenOrders': {'warnWithoutSymbol': False},
            'adjustForTimeDifference': True
        }
    })
    if USAR_TESTNET:
        exchange.set_sandbox_mode(True)

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        threading.Thread(target=hilo_telegram, daemon=True).start()
        threading.Thread(target=hilo_monitor, args=(exchange,), daemon=True).start()
        tg_enviar(f"🚀 Escáner Modo Reversión Extrema Activo (Con Trailing Stop)")

    pares = obtener_solo_perpetuos_usdt(exchange)
    print(f"🚀 Escáner Modo Reversión Extrema Activo | {len(pares)} Pares")
    print("=" * 75)
    
    while True:
        try:
            cupo_ok, total_ocupado, long_ocupado, short_ocupado, detalle = contar_posiciones_por_lado(exchange)
            if not cupo_ok:
                print(f"🟡 [{time.strftime('%H:%M:%S' )}] No se pudo verificar el cupo con seguridad. Se espera {SEGUNDOS_ESPERA_CUPO_LLENO:g}s.")
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
                  f"{', '.join(detalle) if detalle else 'nada abierto'}. Escaneando modo reversión...")

            señales_candidatas = []
            
            for symbol in pares:
                try:
                    market_info = exchange.market(symbol)
                    ticker = exchange.fetch_ticker(symbol)
                    volumen_24h = ticker.get('quoteVolume', 0) or 0
                    if volumen_24h < VOLUMEN_MINIMO_24H_USDT:
                        continue

                    precio_actual = ticker.get('last') or ticker.get('close')
                    if not precio_actual:
                        continue

                    order_book = exchange.fetch_order_book(symbol, limit=200)
                    bids, asks = order_book['bids'], order_book['asks']
                    if not bids or not asks:
                        continue

                    paso_penultimo, paso_ultimo = obtener_dos_ultimos_niveles_adaptativos(market_info)
                    
                    bids_pen = agrupar_libro_ordenes(bids, paso_penultimo)
                    asks_pen = agrupar_libro_ordenes(asks, paso_penultimo)
                    compra_1, vol_c1 = obtener_muro_maximo_volumen(bids_pen)
                    venta_1, vol_v1 = obtener_muro_maximo_volumen(asks_pen)

                    if not compra_1 or not venta_1 or compra_1 >= venta_1:
                        continue

                    bids_ult = agrupar_libro_ordenes(bids, paso_ultimo)
                    asks_ult = agrupar_libro_ordenes(asks, paso_ultimo)
                    compra_2, vol_c2 = obtener_muro_maximo_volumen({p: v for p, v in bids_ult.items() if p < compra_1})
                    venta_2, vol_v2 = obtener_muro_maximo_volumen({p: v for p, v in asks_ult.items() if p > venta_1})

                    # ==========================================
                    # EVALUACIÓN SHORT (Precio sobreextendido arriba -> Buscar Muro Venta N1)
                    # ==========================================
                    if venta_1 and venta_1 > precio_actual:
                        ok_rev, msg_rev = validar_reversion_sma(exchange, symbol, es_long=False, precio_actual=precio_actual)
                        if ok_rev and venta_2 and venta_2 > venta_1:
                            distancia_tp = venta_1 - compra_1
                            distancia_sl = venta_2 - venta_1
                            if distancia_sl > 0:
                                ratio = distancia_tp / distancia_sl
                                if ratio >= MIN_RATIO:
                                    calc = calcular_entrada(CAPITAL_DISPONIBLE, RIESGO_PCT, venta_1, venta_2, LEVERAGE)
                                    if calc:
                                        señales_candidatas.append({
                                            'symbol': symbol, 'market_info': market_info, 'es_long': False,
                                            'precio_entrada': venta_1, 'precio_stop': venta_2,
                                            'ratio': ratio, 'volumen_entrada': vol_v1,
                                            'pct_movimiento_sl': (distancia_sl / venta_1) * 100, 'calc': calc
                                        })

                    # ==========================================
                    # EVALUACIÓN LONG (Precio sobreextendido abajo -> Buscar Muro Compra N1)
                    # ==========================================
                    if compra_1 and compra_1 < precio_actual:
                        ok_rev, msg_rev = validar_reversion_sma(exchange, symbol, es_long=True, precio_actual=precio_actual)
                        if ok_rev and compra_2 and compra_2 < compra_1:
                            distancia_tp = venta_1 - compra_1
                            distancia_sl = compra_1 - compra_2
                            if distancia_sl > 0:
                                ratio = distancia_tp / distancia_sl
                                if ratio >= MIN_RATIO:
                                    calc = calcular_entrada(CAPITAL_DISPONIBLE, RIESGO_PCT, compra_1, compra_2, LEVERAGE)
                                    if calc:
                                        señales_candidatas.append({
                                            'symbol': symbol, 'market_info': market_info, 'es_long': True,
                                            'precio_entrada': compra_1, 'precio_stop': compra_2,
                                            'ratio': ratio, 'volumen_entrada': vol_c1,
                                            'pct_movimiento_sl': (distancia_sl / compra_1) * 100, 'calc': calc
                                        })

                    time.sleep(PAUSA_ENTRE_PARES_SEG)
                except Exception:
                    continue

            if señales_candidatas:
                candidatas_ordenadas = ordenar_candidatas_por_score(señales_candidatas)
                cupo_ok, total_ocupado, long_ocupado, short_ocupado, _ = contar_posiciones_por_lado(exchange)
                if cupo_ok:
                    evaluar_y_ejecutar_candidatas(exchange, candidatas_ordenadas, total_ocupado, long_ocupado, short_ocupado)

            time.sleep(10)
        except Exception as e:
            print(f"⚠️ Error general en ciclo: {e}")
            time.sleep(PAUSA_ERROR_RED_SEG)

if __name__ == "__main__":
    escanear_perpetuos_binance()
