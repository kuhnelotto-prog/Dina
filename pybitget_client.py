"""
pybitget_client.py
Замена для pybitget — прямые вызовы Bitget REST API v2 через requests.
"""
import hashlib
import hmac
import base64
import time
import requests
from typing import Any, Dict, Optional

def _sign(secret: str, timestamp: str, method: str, path: str, body: str = "") -> str:
    msg = timestamp + method.upper() + path + body
    return base64.b64encode(
        hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()

def _headers(api_key: str, secret: str, passphrase: str,
             method: str, path: str, body: str = "") -> Dict[str, str]:
    ts = str(int(time.time() * 1000))
    return {
        "ACCESS-KEY": api_key,
        "ACCESS-SIGN": _sign(secret, ts, method, path, body),
        "ACCESS-TIMESTAMP": ts,
        "ACCESS-PASSPHRASE": passphrase,
        "Content-Type": "application/json",
        "locale": "en-US",
    }

BASE = "https://api.bitget.com"

class OrderApi:
    def __init__(self, client):
        # client is an instance of Client
        self.k = client.api_key
        self.s = client.api_secret
        self.p = client.passphrase

    def _post(self, path: str, body: Dict[str, Any]) -> Dict:
        import json
        b = json.dumps(body)
        r = requests.post(BASE + path,
                          headers=_headers(self.k, self.s, self.p, "POST", path, b),
                          data=b, timeout=10)
        return r.json()

    def placeOrder(self, symbol, marginCoin, size, side,
                   orderType, price="", **kwargs) -> Dict:
        body = {
            "symbol": symbol,
            "marginCoin": marginCoin,
            "size": str(size),
            "side": side.lower(),
            "orderType": orderType,
            "price": str(price),
            "productType": kwargs.get("productType", "USDT-FUTURES"),
        }
        # Добавляем дополнительные параметры, если переданы
        if "marginMode" in kwargs:
            body["marginMode"] = kwargs["marginMode"]
        if "tradeSide" in kwargs:
            body["tradeSide"] = kwargs["tradeSide"]
        if "clientOid" in kwargs:
            body["clientOid"] = kwargs["clientOid"]
        return self._post("/api/v2/mix/order/place-order", body)

    def placePlanOrder(self, symbol, marginCoin, size, side,
                       triggerPrice, triggerType="mark_price",
                       executePrice="", planType="pos_profit", **kwargs) -> Dict:
        body = {
            "symbol": symbol,
            "marginCoin": marginCoin,
            "size": str(size),
            "side": side.lower(),
            "triggerPrice": str(triggerPrice),
            "triggerType": triggerType,
            "executePrice": str(executePrice),
            "planType": planType,
            "productType": kwargs.get("productType", "USDT-FUTURES"),
        }
        # Добавляем дополнительные параметры, если переданы
        if "marginMode" in kwargs:
            body["marginMode"] = kwargs["marginMode"]
        if "tradeSide" in kwargs:
            body["tradeSide"] = kwargs["tradeSide"]
        if "orderType" in kwargs:
            body["orderType"] = kwargs["orderType"]
        if "clientOid" in kwargs:
            body["clientOid"] = kwargs["clientOid"]
        return self._post("/api/v2/mix/order/place-plan-order", body)

    def cancelPlanOrder(self, symbol, orderId, **kwargs) -> Dict:
        return self._post("/api/v2/mix/order/cancel-plan-order", {
            "symbol": symbol, "orderId": orderId,
            "productType": kwargs.get("productType", "USDT-FUTURES"),
        })

    def ordersPlanPending(self, symbol, **kwargs) -> Dict:
        path = f"/api/v2/mix/order/orders-plan-pending?symbol={symbol}&productType={kwargs.get('productType', 'USDT-FUTURES')}"
        r = requests.get(BASE + path,
                         headers=_headers(self.k, self.s, self.p, "GET", path),
                         timeout=10)
        return r.json()

    def cancelAllPlanOrders(self, symbol, **kwargs) -> Dict:
        return self._post("/api/v2/mix/order/cancel-all-plan-order", {
            "symbol": symbol,
            "productType": kwargs.get("productType", "USDT-FUTURES"),
        })

    def detail(self, symbol, orderId, **kwargs) -> Dict:
        path = f"/api/v2/mix/order/detail?symbol={symbol}&orderId={orderId}&productType={kwargs.get('productType', 'USDT-FUTURES')}"
        r = requests.get(BASE + path,
                         headers=_headers(self.k, self.s, self.p, "GET", path),
                         timeout=10)
        return r.json()

class PositionApi:
    def __init__(self, client):
        self.k = client.api_key
        self.s = client.api_secret
        self.p = client.passphrase

    def _post(self, path: str, body: Dict[str, Any]) -> Dict:
        import json
        b = json.dumps(body)
        r = requests.post(BASE + path,
                          headers=_headers(self.k, self.s, self.p, "POST", path, b),
                          data=b, timeout=10)
        return r.json()

    def _get(self, path: str, params: Dict) -> Dict:
        from urllib.parse import urlencode
        qs = urlencode(params)
        full_path = path + ("?" + qs if qs else "")
        r = requests.get(BASE + full_path,
                         headers=_headers(self.k, self.s, self.p, "GET", full_path),
                         timeout=10)
        return r.json()

    def allPosition(self, productType: str, marginCoin: str = "USDT") -> Dict:
        return self._get("/api/v2/mix/position/all-position", {
            "productType": productType, "marginCoin": marginCoin,
        })

    def setMarginMode(self, symbol, marginMode, **kwargs) -> Dict:
        return self._post("/api/v2/mix/account/set-margin-mode", {
            "symbol": symbol, "marginMode": marginMode,
            "productType": kwargs.get("productType", "USDT-FUTURES"),
        })

    def setLeverage(self, symbol, leverage, **kwargs) -> Dict:
        return self._post("/api/v2/mix/account/set-leverage", {
            "symbol": symbol, "leverage": str(leverage),
            "productType": kwargs.get("productType", "USDT-FUTURES"),
        })

class AccountApi:
    def __init__(self, client):
        self.k = client.api_key
        self.s = client.api_secret
        self.p = client.passphrase

    def accounts(self, productType: str) -> Dict:
        path = f"/api/v2/mix/account/accounts?productType={productType}"
        r = requests.get(BASE + path,
                         headers=_headers(self.k, self.s, self.p, "GET", path),
                         timeout=10)
        return r.json()

class Client:
    """Совместимость с pybitget.client.Client"""
    def __init__(self, api_key: str, api_secret: str, passphrase: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.order = OrderApi(self)
        self.position = PositionApi(self)
        self.account = AccountApi(self)
