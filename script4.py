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
# CONFIGURACIÓN GENERAL DEL BOT (MODO REVERSIÓN EXTREMA)
# ==============================================================================
MIN_RATIO = 2.0                   # Ratio mínimo R:R (1:2) — INAMOVIBLE
PAUSA_ENTRE_PARES_SEG = 0.04      # Pausa entre pares en segundos
PAUSA_ERROR_RED_SEG = 10          # Pausa si se cae la red

# ------------------------------------------------------------------------------
# MODO REVERSIÓN EXTREMA Y VOLUMEN
# ------------------------------------------------------------------------------
MODO_REVERSION_EXTREMA = True     # True = Caza extremos alejados de la SMA para retroceso en muro
SMA_CORTA_PERIODO = 20            # Periodo SMA corta
SMA_LARGA_PERIODO = 200           # Periodo SMA larga
SOBREEXTENSION_MIN_PCT = 3.5      # % Mínimo de distancia a la SMA 20 para activar reversión
VOLUMEN_MINIMO_24H_USDT = 5_000_000  # Volumen mínimo en USDT en 24h
TIMEFRAME_SMA = '1h'              # Temporalidad SMA

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
EJECUTAR_ORDENES_REALES = True    # True = Manda órdenes reales a Binance
USAR_TESTNET = False                # False = Binance real

ACTIVACION_TRAILING_R = 1.8        # Trailing Stop se activa a 1.8R para proteger y dejar correr
MAX_OPERACIONES_ABIERTAS = 4       # Operaciones simultáneas máximas
SEGUNDOS_ESPERA_CUPO_LLENO = 30    
MINUTOS_MAX_ORDEN_PENDIENTE = 180  # Tiempo límite para órdenes en el muro

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
# VALIDACIÓN DE SOBREEXTENSIÓN PARA REVERSIÓN EXTREMA
# ==============================================================================
def validar_reversion_sma(exchange, symbol, es_long, precio_actual):
    if not MODO_REVERSION_EXTREMA:
        return True, "Modo reversión extrema desactivado"
    
    try:
        limit_velas = SMA_LARGA_PERIODO + 10
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME_SMA, limit=limit_velas)
        if len(ohlcv) < SMA_LARGA_PERIODO:
            return False, f"insuficientes velas"
        
        cierres = [x[4] for x in ohlcv]
        sma_20 = sum(cierres[-SMA_CORTA_PERIODO:]) / SMA_CORTA_PERIODO
        
        distancia_sma_pct = ((precio_actual - sma_20) / sma_20) * 100

        if es_long:
            if distancia_sma_pct <= -SOBREEXTENSION_MIN_PCT:
                return True, f"LONG Reversión | Precio abajo de SMA20 ({distancia_sma_pct:.2f}%)"
            else:
                return False, f"LONG rechazado: No sobreextendido ({distancia_sma_pct:.2f}%)"
        else:
            if distancia_sma_pct >= SOBREEXTENSION_MIN_PCT:
                return True, f"SHORT Reversión | Precio arriba de SMA20 (+{distancia_sma_pct:.2f}%)"
            else:
                return False, f"SHORT rechazado: No sobreextendido ({distancia_sma_pct:.2f}%)"

    except Exception as e:
        return False, f"error SMA ({e})"


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
        print(f"⚠️ Error fetch_positions: {e}")
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
            ocupados[sym] = (lado, 'orden pendiente')
    except Exception as e:
        print(f"⚠️ Error fetch_open_orders: {e}")
        ok = False

    longs = sum(1 for lado, _ in ocupados.values() if lado == 'long')
    shorts = sum(1 for lado, _ in ocupados.values() if lado == 'short')
    detalle = [f"{sym} {lado.upper()}" for sym, (lado, _) in ocupados.items()]
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
        return False, "lleno"
    max_por_lado = math.ceil(MAX_OPERACIONES_ABIERTAS / 2)
    ocupado_lado = long_ocupado if es_long else short_ocupado
    if ocupado_lado >= max_por_lado:
        return False, "lado lleno"
    return True, None


def tiene_posicion_u_orden_abierta(exchange, symbol):
    try:
        if exchange.fetch_positions([symbol]):
            for p in exchange.fetch_positions([symbol]):
                if float(p.get('contracts') or 0) != 0:
                    return True
        if exchange.fetch_open_orders(symbol):
            return True
    except Exception:
        return True
    return False


def validar_precision_y_notional(exchange, symbol, market_info, cantidad, precio_entrada):
    try:
        cantidad_str = exchange.amount_to_precision(symbol, cantidad)
        precio_str = exchange.price_to_precision(symbol, precio_entrada)
        cantidad_ok = float(cantidad_str)
        precio_ok = float(precio_str)
    except Exception as e:
        return None, None, f"error precisión ({e})"

    limites = market_info.get('limits', {}) or {}
    min_qty = ((limites.get('amount') or {}).get('min'))
    min_notional = ((limites.get('cost') or {}).get('min')) or 5.0
    notional = cantidad_ok * precio_ok

    if cantidad_ok <= 0 or (min_qty and cantidad_ok < min_qty) or (min_notional and notional < min_notional):
        return None, None, "por debajo de mínimos de Binance"

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
        return False

    cantidad_ok, precio_ok, error = validar_precision_y_notional(exchange, symbol, market_info, cantidad, precio_entrada)
    if error:
        return False

    print(f"   🤖 Ejecutando Reversión Extrema: LIMIT {lado_entrada.upper()} {cantidad_ok} {market_info['base']} @ {precio_entrada}")
    print(f"   🤖 SL: {precio_stop} | Trailing activando a {ACTIVACION_TRAILING_R}R")

    if not EJECUTAR_ORDENES_REALES:
        return True

    try:
        exchange.set_leverage(LEVERAGE, symbol)
        client_id = f"rev{int(time.time() * 1000)}"
        exchange.create_order(symbol, 'limit', lado_entrada, cantidad_ok, precio_ok, params={'clientOrderId': client_id})
    except Exception as e:
        tg_enviar(f"❌ ERROR en {symbol}: {e}")
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
    tg_enviar(f"📥 ENTRADA REVERSIÓN EN MURO: {symbol} {'LONG' if es_long else 'SHORT'}\nLIMIT: {precio_ok}")
    return True


def verificar_proteccion_activa(exchange, symbol, simbolo_binance):
    if symbol in PROTECCIONES_ENVIADAS:
        return True
    try:
        resp = _binance_signed_request('GET', '/fapi/v1/openAlgoOrders', {'symbol': simbolo_binance}, BINANCE_API_KEY, BINANCE_API_SECRET)
        return len(resp.get('data', []) if isinstance(resp, dict) else resp) > 0
    except Exception:
        return symbol in PROTECCIONES_ENVIADAS


def colocar_proteccion_pendiente(exchange, symbol, simbolo_binance=None):
    if symbol in PROTECCIONES_ENVIADAS:
        return True

    with _lock_proteccion:
        info = PROTECCION_PENDIENTE.get(symbol)

    if not info and simbolo_binance:
        try:
            for p in exchange.fetch_positions([symbol]):
                contratos = float(p.get('contracts') or 0)
                if contratos != 0:
                    lado_cierre = 'SELL' if contratos > 0 else 'BUY'
                    entrada = float(p.get('entryPrice'))
                    stop_estimado = entrada * 0.98 if contratos > 0 else entrada * 1.02
                    trailing = calcular_trailing_protector(entrada, stop_estimado)
                    info = {
                        'simbolo_binance': simbolo_binance, 'lado_cierre': lado_cierre,
                        'precio_stop': stop_estimado, 'activation_price': trailing['activation_price'],
                        'callback_rate': trailing['callback_rate_pct_ajustado'],
                        'cantidad_str': exchange.amount_to_precision(symbol, abs(contratos))
                    }
                    break
        except Exception:
            pass

    if not info:
        return False

    try:
        _binance_signed_request('POST', '/fapi/v1/algoOrder', {
            'algoType': 'CONDITIONAL', 'symbol': info['simbolo_binance'], 'side': info['lado_cierre'],
            'type': 'STOP_MARKET', 'triggerPrice': exchange.price_to_precision(symbol, info['precio_stop']),
            'quantity': info['cantidad_str'], 'reduceOnly': 'true', 'workingType': 'CONTRACT_PRICE'
        }, BINANCE_API_KEY, BINANCE_API_SECRET)
    except Exception:
        pass

    try:
        _binance_signed_request('POST', '/fapi/v1/algoOrder', {
            'algoType': 'CONDITIONAL', 'symbol': info['simbolo_binance'], 'side': info['lado_cierre'],
            'type': 'TRAILING_STOP_MARKET', 'quantity': info['cantidad_str'],
            'activatePrice': exchange.price_to_precision(symbol, info['activation_price']),
            'callbackRate': info['callback_rate'], 'workingType': 'CONTRACT_PRICE', 'reduceOnly': 'true'
        }, BINANCE_API_KEY, BINANCE_API_SECRET)
    except Exception:
        pass

    PROTECCIONES_ENVIADAS.add(symbol)
    with _lock_proteccion:
        PROTECCION_PENDIENTE.pop(symbol, None)
    return True


PREFIJO_ORDEN = "rev"   


AJUSTES_EDITABLES = {
    'CAPITAL_DISPONIBLE': (float, 1, 1_000_000, "Capital disponible (USDT)"),
    'RIESGO_PCT': (float, 0.1, 50, "% del capital que se arriesga"),
    'LEVERAGE': (int, 1, 125, "Apalancamiento"),
    'MAX_OPERACIONES_ABIERTAS': (int, 1, 20, "Máx operaciones simultáneas"),
    'ACTIVACION_TRAILING_R': (float, 1.05, 10, "Activación Trailing R"),
    'SOBREEXTENSION_MIN_PCT': (float, 1.0, 30.0, "% Mínimo distancia SMA"),
    'MODO_REVERSION_EXTREMA': (bool, None, None, "Modo Reversión (true/false)"),
    'EJECUTAR_ORDENES_REALES': (bool, None, None, "Órdenes reales (true/false)"),
}

ALIAS_AJUSTES = {
    'capital': 'CAPITAL_DISPONIBLE', 'riesgo': 'RIESGO_PCT', 'leverage': 'LEVERAGE',
    'operaciones': 'MAX_OPERACIONES_ABIERTAS', 'trailing': 'ACTIVACION_TRAILING_R',
    'sobreextension': 'SOBREEXTENSION_MIN_PCT', 'reversion': 'MODO_REVERSION_EXTREMA',
}


def _convertir_valor(tipo, texto):
    t = str(texto).strip().replace(',', '.')
    if tipo is bool:
        return t.lower() in ('true', '1', 'si', 'sí', 'on', 'activar')
    numero = float(t)
    return int(numero) if tipo is int else numero


def resolver_nombre_ajuste(texto):
    k = texto.strip().lower()
    return ALIAS_AJUSTES.get(k) or next((n for n in AJUSTES_EDITABLES if n.lower() == k), None)


def aplicar_ajuste(nombre, valor):
    globals()[nombre] = tipo = AJUSTES_EDITABLES[nombre][0](valor)


def panel_ajustes():
    return (
        "⚙️ *MODO REVERSIÓN EXTREMA*\n"
        f"💰 Capital: {CAPITAL_DISPONIBLE:g} USDT | Riesgo: {RIESGO_PCT:g}%\n"
        f"🔄 Reversión: {'ON' if MODO_REVERSION_EXTREMA else 'OFF'} | Sobreextensión: {SOBREEXTENSION_MIN_PCT:g}%\n"
        f"🎯 Trailing: {ACTIVACION_TRAILING_R}R"
    ), None


def _tg_procesar(upd):
    if 'message' in upd:
        m = upd['message']
        if str(m['chat']['id']) == str(TELEGRAM_CHAT_ID):
            texto = m.get('text', '').strip()
            if texto in ('📡 Estado', '/estado'):
                tg_enviar(f"{panel_ajustes()[0]}\n\n{texto_posiciones()}\n\n{texto_pnl()}")


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
    totales = {'diario': 0.0, 'semanal': 0.0, 'mensual': 0.0}
    for r in obtener_income(int(hoy0.timestamp() * 1000)):
        if r.get('incomeType') in TIPOS_PNL and r.get('asset') == 'USDT':
            totales['diario'] += float(r['income'])
    return totales


def texto_pnl():
    try:
        p = calcular_pnl_periodos()
        return f"📊 *PnL Hoy*: {p['diario']:+,.2f} USDT"
    except Exception:
        return "📊 PnL no disponible"


def _pnl_operacion(id_binance, desde_ms):
    total = 0.0
    for r in obtener_income(desde_ms):
        if r.get('symbol') == id_binance and r.get('incomeType') in TIPOS_PNL and r.get('asset') == 'USDT':
            total += float(r['income'])
    return total


def texto_posiciones():
    return "📌 Monitoreando muros de reversión..."


def hilo_monitor(exchange):
    while True:
        try:
            actuales = {p['symbol']: p for p in exchange.fetch_positions() if float(p.get('contracts') or 0) != 0}
            for sym, p in actuales.items():
                if EJECUTAR_ORDENES_REALES and not verificar_proteccion_activa(exchange, sym, p['info']['symbol']):
                    colocar_proteccion_pendiente(exchange, sym, p['info']['symbol'])
        except Exception:
            pass
        time.sleep(INTERVALO_MONITOR_SEG)


def calcular_entrada(capital, riesgo_pct, precio_entrada, precio_stop, leverage):
    diff = abs(precio_entrada - precio_stop)
    if precio_stop <= 0 or diff <= 0:
        return None
    riesgo_fraccion = diff / precio_entrada
    perdida_usd = capital * (riesgo_pct / 100)
    capital_a_usar = perdida_usd / riesgo_fraccion
    cantidad_monedas = capital_a_usar / precio_entrada
    return {
        'movimiento_pct': riesgo_fraccion * 100,
        'perdida_usd': perdida_usd,
        'capital_a_usar': capital_a_usar,
        'cantidad_monedas': round(cantidad_monedas, 5),
        'margen_necesario': capital_a_usar / leverage,
    }


def agrupar_precio(precio, paso):
    return round(math.floor(precio / paso) * paso, max(0, -int(math.floor(math.log10(paso)))))


def agrupar_libro_ordenes(orders, paso):
    if paso <= 0:
        return {}
    bloques = {}
    for precio, vol in orders:
        b = agrupar_precio(precio, paso)
        if b not in bloques:
            bloques[b] = {'vol_total': 0.0, 'precio_pico': precio, 'vol_pico': vol}
        bloques[b]['vol_total'] += vol
        if vol > bloques[b]['vol_pico']:
            bloques[b]['vol_pico'] = vol
            bloques[b]['precio_pico'] = precio
    return bloques


def obtener_muro_maximo_volumen(bloques):
    if not bloques:
        return None, 0.0
    g = max(bloques.items(), key=lambda i: i[1]['vol_total'])[1]
    return g['precio_pico'], g['vol_total']


def obtener_dos_ultimos_niveles_adaptativos(market_info):
    paso_base = 0.0001
    for f in market_info.get('info', {}).get('filters', []):
        if f.get('filterType') == 'PRICE_FILTER':
            paso_base = float(f.get('tickSize', 0.0001))
            break
    niveles = sorted(list(set([round(paso_base, 8), round(paso_base * 10, 8), round(paso_base * 100, 8)])))
    return niveles[-2], niveles[-1]


def obtener_solo_perpetuos_usdt(exchange):
    while True:
        try:
            markets = exchange.load_markets()
            return [s for s, m in markets.items() if m.get('quote') == 'USDT' and m.get('swap') and m.get('active', True)]
        except Exception:
            time.sleep(PAUSA_ERROR_RED_SEG)


def evaluar_y_ejecutar_candidatas(exchange, candidatas, total_ocupado, long_ocupado, short_ocupado):
    for c in candidatas:
        if total_ocupado >= MAX_OPERACIONES_ABIERTAS:
            break
        cupo_ok, _ = hay_cupo(total_ocupado, long_ocupado, short_ocupado, c['es_long'])
        if not cupo_ok or tiene_posicion_u_orden_abierta(exchange, c['symbol']):
            continue
        if ejecutar_operacion(exchange, c['symbol'], c['market_info'], c['es_long'], c['precio_entrada'], c['precio_stop'], c['calc']):
            total_ocupado += 1
            if c['es_long']:
                long_ocupado += 1
            else:
                short_ocupado += 1


def escanear_perpetuos_binance():
    exchange = ccxt.binance({
        'apiKey': BINANCE_API_KEY, 'secret': BINANCE_API_SECRET, 'enableRateLimit': True,
        'options': {'defaultType': 'future', 'adjustForTimeDifference': True}
    })
    if USAR_TESTNET:
        exchange.set_sandbox_mode(True)

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        threading.Thread(target=hilo_telegram, daemon=True).start()
        threading.Thread(target=hilo_monitor, args=(exchange,), daemon=True).start()
        tg_enviar("🚀 Escáner Modo Reversión Extrema con Trailing (TL) Activo")

    pares = obtener_solo_perpetuos_usdt(exchange)
    print(f"🚀 Modo Reversión Extrema Activo | {len(pares)} Pares")
    print("=" * 75)
    
    while True:
        try:
            cupo_ok, total_ocupado, long_ocupado, short_ocupado, _ = contar_posiciones_por_lado(exchange)
            if not cupo_ok or total_ocupado >= MAX_OPERACIONES_ABIERTAS:
                time.sleep(SEGUNDOS_ESPERA_CUPO_LLENO)
                continue

            señales = []
            for symbol in pares:
                try:
                    market_info = exchange.market(symbol)
                    ticker = exchange.fetch_ticker(symbol)
                    if (ticker.get('quoteVolume', 0) or 0) < VOLUMEN_MINIMO_24H_USDT:
                        continue
                    precio_actual = ticker.get('last') or ticker.get('close')
                    if not precio_actual:
                        continue

                    order_book = exchange.fetch_order_book(symbol, limit=200)
                    bids, asks = order_book['bids'], order_book['asks']
                    if not bids or not asks:
                        continue

                    p_pen, p_ult = obtener_dos_ultimos_niveles_adaptativos(market_info)
                    c_1, vol_c1 = obtener_muro_maximo_volumen(agrupar_libro_ordenes(bids, p_pen))
                    v_1, vol_v1 = obtener_muro_maximo_volumen(agrupar_libro_ordenes(asks, p_pen))
                    if not c_1 or not v_1 or c_1 >= v_1:
                        continue

                    c_2, _ = obtener_muro_maximo_volumen({p: v for p, v in agrupar_libro_ordenes(bids, p_ult).items() if p < c_1})
                    v_2, _ = obtener_muro_maximo_volumen({p: v for p, v in agrupar_libro_ordenes(asks, p_ult).items() if p > v_1})

                    # SHORT Reversión (Precio arriba -> Muro Venta N1)
                    if v_1 and v_1 > precio_actual:
                        ok_rev, _ = validar_reversion_sma(exchange, symbol, es_long=False, precio_actual=precio_actual)
                        if ok_rev and v_2 and v_2 > v_1:
                            dist_tp = v_1 - c_1
                            dist_sl = v_2 - v_1
                            if dist_sl > 0 and (dist_tp / dist_sl) >= MIN_RATIO:
                                calc = calcular_entrada(CAPITAL_DISPONIBLE, RIESGO_PCT, v_1, v_2, LEVERAGE)
                                if calc:
                                    señales.append({
                                        'symbol': symbol, 'market_info': market_info, 'es_long': False,
                                        'precio_entrada': v_1, 'precio_stop': v_2, 'ratio': dist_tp / dist_sl,
                                        'volumen_entrada': vol_v1, 'pct_movimiento_sl': (dist_sl / v_1) * 100, 'calc': calc
                                    })

                    # LONG Reversión (Precio abajo -> Muro Compra N1)
                    if c_1 and c_1 < precio_actual:
                        ok_rev, _ = validar_reversion_sma(exchange, symbol, es_long=True, precio_actual=precio_actual)
                        if ok_rev and c_2 and c_2 < c_1:
                            dist_tp = v_1 - c_1
                            dist_sl = c_1 - c_2
                            if dist_sl > 0 and (dist_tp / dist_sl) >= MIN_RATIO:
                                calc = calcular_entrada(CAPITAL_DISPONIBLE, RIESGO_PCT, c_1, c_2, LEVERAGE)
                                if calc:
                                    señales.append({
                                        'symbol': symbol, 'market_info': market_info, 'es_long': True,
                                        'precio_entrada': c_1, 'precio_stop': c_2, 'ratio': dist_tp / dist_sl,
                                        'volumen_entrada': vol_c1, 'pct_movimiento_sl': (dist_sl / c_1) * 100, 'calc': calc
                                    })
                    time.sleep(PAUSA_ENTRE_PARES_SEG)
                except Exception:
                    continue

            if señales:
                evaluar_y_ejecutar_candidatas(exchange, ordenar_candidatas_por_score(señales), total_ocupado, long_ocupado, short_ocupado)
            time.sleep(10)
        except Exception as e:
            print(f"⚠️ Error: {e}")
            time.sleep(PAUSA_ERROR_RED_SEG)

if __name__ == "__main__":
    escanear_perpetuos_binance()
