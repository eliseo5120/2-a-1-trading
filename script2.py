import ccxt
from config import BINANCE_API_KEY, BINANCE_API_SECRET

exchange = ccxt.binance({
    'apiKey': BINANCE_API_KEY,
    'secret': BINANCE_API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'future'}
})

# 1. Probar lectura de posiciones
pos = exchange.fetch_positions()
print("Posiciones detectadas:", len([p for p in pos if float(p.get('contracts') or 0) > 0]))

# 2. Probar lectura de órdenes abiertas
ordenes = exchange.fetch_open_orders()
print("Órdenes abiertas detectadas (sin params):", len(ordenes))

ordenes_params = exchange.fetch_open_orders(params={'subType': 'linear'})
print("Órdenes abiertas detectadas (con params linear):", len(ordenes_params))
