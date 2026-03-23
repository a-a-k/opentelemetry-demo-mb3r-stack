#!/usr/bin/env python3

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


BASE_URL = os.environ.get("MB3R_PROBE_BASE_URL", "http://frontend-proxy:8080").rstrip("/")
INTERVAL = float(os.environ.get("MB3R_PROBE_INTERVAL", "15"))
TIMEOUT = float(os.environ.get("MB3R_PROBE_TIMEOUT", "5"))

CHECKOUT_PERSON = {
    "email": "larry_sergei@example.com",
    "address": {
        "streetAddress": "1600 Amphitheatre Parkway",
        "zipCode": "94043",
        "city": "Mountain View",
        "state": "CA",
        "country": "United States",
    },
    "userCurrency": "USD",
    "creditCard": {
        "creditCardNumber": "4432-8015-6152-0454",
        "creditCardExpirationMonth": 1,
        "creditCardExpirationYear": 2039,
        "creditCardCvv": 672,
    },
}


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def request_json(path, method="GET", payload=None, params=None):
    url = f"{BASE_URL}{path}"
    if params:
        query = urllib.parse.urlencode(params, doseq=True)
        url = f"{url}?{query}"

    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        body = response.read().decode("utf-8")
        return json.loads(body) if body else None


def probe_once():
    step = "GET /api/products"
    try:
        products = request_json("/api/products", params={"currencyCode": "USD"})
        if not isinstance(products, list) or not products:
            raise RuntimeError("probe did not receive products")

        product_id = products[0].get("id")
        if not product_id:
            raise RuntimeError("probe could not resolve a product id")

        user_id = str(uuid.uuid4())

        step = "GET /api/recommendations"
        request_json(
            "/api/recommendations",
            params={
                "productIds": [product_id],
                "sessionId": user_id,
                "currencyCode": "USD",
            },
        )

        step = "POST /api/cart"
        request_json(
            "/api/cart",
            method="POST",
            payload={
                "userId": user_id,
                "item": {
                    "productId": product_id,
                    "quantity": 1,
                },
            },
        )

        step = "GET /api/cart"
        request_json(
            "/api/cart",
            params={
                "sessionId": user_id,
                "currencyCode": "USD",
            },
        )

        checkout_payload = dict(CHECKOUT_PERSON)
        checkout_payload["userId"] = user_id
        step = "POST /api/checkout"
        request_json(
            "/api/checkout",
            method="POST",
            payload=checkout_payload,
            params={"currencyCode": "USD"},
        )
        return step
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, RuntimeError, json.JSONDecodeError) as err:
        raise RuntimeError(f"{step} failed: {err}") from err


def main():
    while True:
        started_at = time.time()
        try:
            last_step = probe_once()
            logging.info("probe completed successfully through %s", last_step)
        except RuntimeError as err:
            logging.warning("probe iteration failed: %s", err)

        elapsed = time.time() - started_at
        time.sleep(max(1.0, INTERVAL - elapsed))


if __name__ == "__main__":
    main()
