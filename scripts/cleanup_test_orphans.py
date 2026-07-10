"""Borra los archivos huérfanos de pruebas de la raíz de Movistar.

Uso:  python3 scripts/cleanup_test_orphans.py <session.json>

session.json = export de sesión del panel admin (validationKey + cookies + userAgent).
Solo borra (softdelete) los nombres listados en VICTIM_NAMES; el resto se conserva.
Requiere: pip install httpx
"""
from __future__ import annotations

import json
import sys
from urllib.parse import urlencode

import httpx

# Huérfanos detectados el 2026-07-09: placeholders de 0 bytes que Finder creó y
# cuyo contenido nunca llegó a Movistar, más sidecars AppleDouble de pruebas.
VICTIM_NAMES = {
    "._ (1).DS_Store",
    "._ (2).DS_Store",
    "._Pegado 08-07-2026 a las 21.30.56.textClipping",
    "._docker-compose.txt",
    "._telegram.yaml",
    "Pegado 08-07-2026 a las 21.30.56.textClipping",
    "gio-test-1738.txt",
    "gio-test-1755 (1).txt",
    "gio-test-1905 (1).txt",
    "gio-test-1907.txt",
    "telegram.yaml",
}

API = "https://micloud.movistar.es/sapi/"


def main() -> None:
    session = json.load(open(sys.argv[1]))
    vkey = session["validationKey"]
    jar = {c["name"]: c["value"] for c in session["cookies"]}

    def headers() -> dict:
        return {
            "User-Agent": session["userAgent"],
            "Origin": "https://micloud.movistar.es",
            "Referer": "https://micloud.movistar.es/",
            "X-deviceid": "O2CloudGatewayCleanup",
            "Accept": "application/json",
            "Cookie": "; ".join(f"{k}={v}" for k, v in jar.items()),
        }

    client = httpx.Client(timeout=30)

    def api(method: str, resource: str, query: dict, body=None):
        nonlocal vkey
        url = API + resource + "?" + urlencode({**query, "validationkey": vkey})
        response = client.request(method, url, headers=headers(), json=body)
        for cookie in response.cookies.jar:
            jar[cookie.name] = cookie.value
            if cookie.name.lower() == "validationkey":
                vkey = cookie.value
        try:
            return response.status_code, response.json()
        except Exception:
            return response.status_code, response.text[:200]

    status, payload = api("POST", "media/folder/root", {"action": "get"})
    if status >= 400:
        print(f"Sesión rechazada: HTTP {status} {str(payload)[:150]}")
        sys.exit(1)
    root = str(payload["data"]["folders"][0]["id"])
    status, payload = api(
        "POST", "media", {"action": "get", "folderid": root, "limit": "200"},
        {"data": {"fields": ["name", "size"]}},
    )
    media = (payload.get("data") or {}).get("media") or []
    for item in media:
        name = item.get("name") or ""
        if name in VICTIM_NAMES:
            status, result = api(
                "POST", "media/file", {"action": "delete", "softdelete": "true"},
                {"data": {"files": [int(item["id"])]}},
            )
            error = (result.get("error") or {}).get("code") if isinstance(result, dict) else "?"
            print(f"BORRADO {name}: {'OK' if not error else error}")
        else:
            print(f"conservado: {name} ({item.get('size')} bytes)")


if __name__ == "__main__":
    main()
