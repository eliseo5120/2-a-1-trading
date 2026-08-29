import ccxt
import time
import math
import hmac
import hashlib
import requests
from urllib.parse import urlencode

# ==============================================================================
# CONFIGURACIÓN GENERAL DEL BOT
# ==============================================================================
MIN_RATIO = 2.0                   # Ratio mínimo R:R (1:2)
MINUTOS_ESPERA_ENTRE_CICLOS = 10  # Tiempo de descanso entre ciclos (en MINUTOS)
PAUSA_ENTRE_PARES_SEG = 0.04      # Pausa entre pares en segundos
PAUSA_ERROR_RED_SEG = 10          # Pausa si se cae la red

# ------------------------------------------------------------------------------
# CALCULADORA DE ENTRADAS (capital / riesgo / leverage) — editable
# ------------------------------------------------------------------------------
CAPITAL_DISPONIBLE = 500.0        # Capital disponible en USDT
RIESGO_PCT = 10                   # % del capital que se arriesga por operación (10 = 10%)
LEVERAGE = 10                     # Apalancamiento (solo afecta el margen necesario)
# ==============================================================================

# ------------------------------------------------------------------------------
# EJECUCIÓN DE ÓRDENES EN BINANCE — editable
# ------------------------------------------------------------------------------
EJECUTAR_ORDENES_REALES = False    # ⚠️ En False = solo imprime lo que HARÍA, no manda nada.
                                    #    Ponlo en True solo cuando ya lo probaste en Testnet.
USAR_TESTNET = True                # True = fapi Testnet (dinero de prueba). False = Binance real.

ACTIVACION_TRAILING_R = 1.5        # El trailing se activa cuando el precio llega a 1.5R.
                                    # Con eso, el stop queda protegiendo exactamente el 1:1 (1R).

MAX_OPERACIONES_ABIERTAS = 1       # Cuántas operaciones simultáneas permite el bot.
                                    # Si es 2+, se reparten lo más parejo posible entre LONG y SHORT
                                    # (ej. con 2 -> máx 1 long y 1 short; con 3 -> máx 2 de un lado y 1 del otro).

try:
    from config import BINANCE_API_KEY, BINANCE_API_SECRET  # noqa: E402
except ImportError:
    BINANCE_API_KEY, BINANCE_API_SECRET = None, None
    # Crea un archivo config.py junto a este script con:
    #   BINANCE_API_KEY = "tu_api_key"
    #   BINANCE_API_SECRET = "tu_api_secret"

BINANCE_FAPI_BASE = "https://testnet.binancefuture.com" if USAR_TESTNET else "https://fapi.binance.com"
# ==============================================================================


def _binance_signed_request(method, path, params, api_key, api_secret):
    """Petición firmada HMAC-SHA256 directa a la Futures API de Binance.
    Se usa específicamente para /fapi/v1/algoOrder, que ccxt puede no soportar
    todavía (obligatorio desde el 09-dic-2025 para órdenes condicionales)."""
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
    """
    Calcula activationPrice y callbackRate para que el TRAILING_STOP_MARKET,
    en el momento en que se activa, quede protegiendo exactamente el nivel 1:1
    (no el breakeven). Se activa cuando el precio llega a `activacion_r` * R.

    Para LONG (precio_entrada > precio_stop):
        R = precio_entrada - precio_stop
        activation_price = precio_entrada + activacion_r * R
        protegido_1_1    = precio_entrada + R
        callback_rate    = 1 - (protegido_1_1 / activation_price)

    Para SHORT (precio_entrada < precio_stop):
        R = precio_stop - precio_entrada
        activation_price = precio_entrada - activacion_r * R
        protegido_1_1    = precio_entrada - R
        callback_rate    = (protegido_1_1 / activation_price) - 1
    """
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
    # Binance exige callbackRate entre 0.1% y 5%. Si el riesgo es muy chico/grande
    # respecto al precio, se recorta al límite permitido (deja de proteger el 1:1 exacto).
    callback_rate_pct_ajustado = max(0.1, min(5.0, round(callback_rate_pct, 1)))

    return {
        'activation_price': activation_price,
        'protegido_1_1': protegido_1_1,
        'callback_rate_pct': callback_rate_pct,
        'callback_rate_pct_ajustado': callback_rate_pct_ajustado,
    }


def contar_posiciones_por_lado(exchange):
    """
    Recorre TODAS las posiciones abiertas de la cuenta (todos los símbolos) y devuelve
    (total_abiertas, longs_abiertas, shorts_abiertas).
    """
    longs = 0
    shorts = 0
    try:
        posiciones = exchange.fetch_positions()
        for p in posiciones:
            contratos = p.get('contracts') or 0
            if not contratos or float(contratos) == 0:
                continue
            lado = p.get('side')  # ccxt unificado: 'long' o 'short'
            if lado == 'long':
                longs += 1
            elif lado == 'short':
                shorts += 1
            else:
                # Respaldo si el exchange no manda 'side': usar el signo de 'contracts'
                if float(contratos) > 0:
                    longs += 1
                else:
                    shorts += 1
    except Exception:
        pass

    return longs + shorts, longs, shorts


def hay_cupo_para_nueva_operacion(exchange, es_long):
    """
    Revisa si se puede abrir una operación más, respetando:
      - el máximo total (MAX_OPERACIONES_ABIERTAS)
      - el máximo por lado (repartido lo más parejo posible entre long y short)
    """
    total, longs, shorts = contar_posiciones_por_lado(exchange)

    if total >= MAX_OPERACIONES_ABIERTAS:
        return False, f"ya hay {total}/{MAX_OPERACIONES_ABIERTAS} operaciones abiertas en total"

    max_por_lado = math.ceil(MAX_OPERACIONES_ABIERTAS / 2)
    if es_long and longs >= max_por_lado:
        return False, f"ya hay {longs}/{max_por_lado} operaciones LONG abiertas (cupo por lado)"
    if not es_long and shorts >= max_por_lado:
        return False, f"ya hay {shorts}/{max_por_lado} operaciones SHORT abiertas (cupo por lado)"

    return True, None


def tiene_posicion_u_orden_abierta(exchange, symbol):
    """True si ya hay posición abierta o alguna orden viva en ese símbolo (evita duplicar)."""
    try:
        posiciones = exchange.fetch_positions([symbol])
        for p in posiciones:
            contratos = p.get('contracts') or 0
            if contratos and float(contratos) != 0:
                return True
    except Exception:
        pass

    try:
        ordenes = exchange.fetch_open_orders(symbol)
        if ordenes:
            return True
    except Exception:
        pass

    return False


def ejecutar_operacion(exchange, symbol, market_info, es_long, precio_entrada, precio_stop, calc):
    """
    Secuencia completa cuando salta una alerta:
      1. Fija el leverage.
      2. Manda la orden LIMIT de entrada al precio del muro detectado.
      3. Coloca el STOP_MARKET inicial (SL) vía /fapi/v1/algoOrder.
      4. Coloca el TRAILING_STOP_MARKET que protege el 1:1, vía /fapi/v1/algoOrder.
    Con EJECUTAR_ORDENES_REALES=False solo imprime lo que haría, sin mandar nada.
    """
    lado_entrada = 'buy' if es_long else 'sell'
    lado_cierre = 'SELL' if es_long else 'BUY'
    simbolo_binance = market_info['id']  # ej. 'BTCUSDT' (formato crudo de la API)
    cantidad = calc['cantidad_monedas']

    trailing = calcular_trailing_protector(precio_entrada, precio_stop)
    if not trailing:
        print("   ⚠️  No se pudo calcular el trailing (precio de entrada = stop). Se omite la operación.")
        return

    print(f"   🤖 Plan de ejecución: LIMIT {lado_entrada.upper()} {cantidad} {market_info['base']} @ {precio_entrada}")
    print(f"   🤖 SL inicial (STOP_MARKET closePosition): {precio_stop}")
    print(f"   🤖 Trailing protector 1:1 → activación {trailing['activation_price']:.8f} "
          f"| callback {trailing['callback_rate_pct_ajustado']}% "
          f"(protege ≈ {trailing['protegido_1_1']:.8f})")

    if not EJECUTAR_ORDENES_REALES:
        print("   🔒 EJECUTAR_ORDENES_REALES=False → no se mandó ninguna orden real.\n")
        return

    if not BINANCE_API_KEY or not BINANCE_API_SECRET:
        print("   ❌ Faltan BINANCE_API_KEY / BINANCE_API_SECRET en config.py. No se puede operar.\n")
        return

    try:
        exchange.set_leverage(LEVERAGE, symbol)

        orden_entrada = exchange.create_order(symbol, 'limit', lado_entrada, cantidad, precio_entrada)
        print(f"   ✅ Orden de entrada enviada. id={orden_entrada.get('id')}")

        _binance_signed_request('POST', '/fapi/v1/algoOrder', {
            'symbol': simbolo_binance,
            'side': lado_cierre,
            'type': 'STOP_MARKET',
            'stopPrice': f"{precio_stop:.8f}",
            'closePosition': 'true',
            'workingType': 'MARK_PRICE',
            'priceProtect': 'true',
        }, BINANCE_API_KEY, BINANCE_API_SECRET)
        print("   ✅ Stop Loss inicial colocado (algoOrder).")

        _binance_signed_request('POST', '/fapi/v1/algoOrder', {
            'symbol': simbolo_binance,
            'side': lado_cierre,
            'type': 'TRAILING_STOP_MARKET',
            'closePosition': 'true',
            'activationPrice': f"{trailing['activation_price']:.8f}",
            'callbackRate': trailing['callback_rate_pct_ajustado'],
            'workingType': 'MARK_PRICE',
        }, BINANCE_API_KEY, BINANCE_API_SECRET)
        print("   ✅ Trailing Stop protector del 1:1 colocado (algoOrder).\n")

    except Exception as e:
        print(f"   ❌ Error ejecutando la operación en Binance: {e}\n")


def calcular_entrada(capital, riesgo_pct, precio_entrada, precio_stop, leverage):
    """
    Traduce la calculadora de Excel (hojas LONG/SHORT) a código:
    - riesgo_pct se pasa como número entero (10 = 10%), aquí se convierte a fracción.
    - % de movimiento = distancia entre entrada y stop, relativa al precio MENOR de los dos.
    - Pérdida en USD  = capital * (riesgo_pct / 100).
    - Capital a usar   = pérdida USD / % de movimiento  (valor NOCIONAL de la posición).
    - Cantidad monedas = capital a usar / precio de entrada (el leverage NO multiplica aquí).
    - Margen necesario = capital a usar / leverage (lo que realmente se bloquea en la cuenta).
    """
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
    """ Redondea el precio al escalón exacto de la agrupación """
    precision_decimales = max(0, -int(math.floor(math.log10(paso))))
    return round(math.floor(precio / paso) * paso, precision_decimales)


def agrupar_libro_ordenes(orders, paso):
    """
    Agrupa las órdenes en bloques (igual que el selector de 'Agrupación' de Binance),
    pero además guarda, por cada bloque, el precio REAL (sin redondear) y el volumen
    de la orden individual más grande que cae dentro de ese bloque.
    """
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
    """
    Encuentra el bloque con mayor volumen TOTAL (la 'zona' más fuerte del libro),
    pero devuelve el precio REAL (sin redondear) de la orden con más cantidad
    dentro de ese bloque, en vez del precio del borde del bloque.
    """
    if not bloques_agrupados:
        return None, 0.0

    bloque_ganador = max(bloques_agrupados.items(), key=lambda item: item[1]['vol_total'])
    datos = bloque_ganador[1]
    return datos['precio_pico'], datos['vol_total']


def obtener_dos_ultimos_niveles_adaptativos(market_info, precio_referencia):
    """
    Calcula los pasos de agrupación válidos que generan suficiente densidad
    de datos sin vaciar el libro devuelto por la API.
    """
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

    # Generamos la lista de niveles posibles
    niveles_posibles = [
        round(paso_base, 8),
        round(paso_base * 10, 8),
        round(paso_base * 100, 8),
        round(paso_base * 1000, 8)
    ]

    # Tomamos siempre los dos escalones más grandes de la lista (igual que las dos
    # últimas opciones del desplegable de "Agrupación" del libro de órdenes de Binance).
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


def escanear_perpetuos_binance():
    exchange = ccxt.binance({
        'apiKey': BINANCE_API_KEY,
        'secret': BINANCE_API_SECRET,
        'enableRateLimit': True,
        'options': {'defaultType': 'future'}
    })
    if USAR_TESTNET:
        exchange.set_sandbox_mode(True)
    
    pares = obtener_solo_perpetuos_usdt(exchange)
    print(f"🚀 Escáner iniciado | {len(pares)} Perpetuos USDT | Detección Adaptativa de Volumen")
    print("=" * 75)
    
    while True:
        try:
            alerta_encontrada = False
            
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

                    # 1. PENÚLTIMO NIVEL (Entrada / TP)
                    bids_penultimo = agrupar_libro_ordenes(bids, paso_penultimo)
                    asks_penultimo = agrupar_libro_ordenes(asks, paso_penultimo)

                    compra_1, vol_c1 = obtener_muro_maximo_volumen(bids_penultimo)
                    venta_1, vol_v1 = obtener_muro_maximo_volumen(asks_penultimo)

                    if not compra_1 or not venta_1 or compra_1 >= venta_1:
                        continue

                    # 2. ÚLTIMO NIVEL (Stop Loss)
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

                                    if tiene_posicion_u_orden_abierta(exchange, symbol):
                                        print(f"   ⏭️  Ya hay posición/orden abierta en {symbol}, se omite esta señal.\n")
                                    else:
                                        cupo_ok, motivo = hay_cupo_para_nueva_operacion(exchange, es_long=True)
                                        if not cupo_ok:
                                            print(f"   ⏭️  Sin cupo para LONG: {motivo}.\n")
                                        else:
                                            ejecutar_operacion(exchange, symbol, market_info, True, compra_1, compra_2, calc)
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

                                    if tiene_posicion_u_orden_abierta(exchange, symbol):
                                        print(f"   ⏭️  Ya hay posición/orden abierta en {symbol}, se omite esta señal.\n")
                                    else:
                                        cupo_ok, motivo = hay_cupo_para_nueva_operacion(exchange, es_long=False)
                                        if not cupo_ok:
                                            print(f"   ⏭️  Sin cupo para SHORT: {motivo}.\n")
                                        else:
                                            ejecutar_operacion(exchange, symbol, market_info, False, venta_1, venta_2, calc)
                                else:
                                    print()

                                alerta_encontrada = True

                    time.sleep(PAUSA_ENTRE_PARES_SEG)

                except Exception:
                    continue

            hora_fin = time.strftime('%H:%M:%S')
            if not alerta_encontrada:
                print(f"⏳ [{hora_fin}] Ciclo finalizado sin señales que superen el criterio 1:{MIN_RATIO}.")
            
            print(f"😴 Descansando {MINUTOS_ESPERA_ENTRE_CICLOS} minuto(s)...\n")
            time.sleep(MINUTOS_ESPERA_ENTRE_CICLOS * 60)

        except Exception as e:
            print(f"\n📡 [{time.strftime('%H:%M:%S')}] Conexión de red interrumpida. Reintentando en {PAUSA_ERROR_RED_SEG}s...")
            time.sleep(PAUSA_ERROR_RED_SEG)

if __name__ == "__main__":
    escanear_perpetuos_binance()
