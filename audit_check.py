import requests

def audit(url, method="GET", json_body=None):
    print(f"--- Denetim: {url} ({method}) ---")
    try:
        if method == "POST":
            r = requests.post(url, json=json_body, timeout=5)
        else:
            r = requests.get(url, timeout=5)
        h = r.headers
        print(f"Status: {r.status_code}")
        print(f"Server: {h.get('server', '-')}")
        print(f"CSP: {h.get('content-security-policy', 'Eksik')}")
        print(f"X-Frame-Options: {h.get('x-frame-options', 'Eksik')}")
        print(f"X-Content-Type-Options: {h.get('x-content-type-options', 'Eksik')}")
        print(f"HSTS: {h.get('strict-transport-security', 'Eksik')}")
        print(f"CORS (Allow-Origin): {h.get('access-control-allow-origin', '-')}")
        if r.cookies:
            print("Cookies:")
            for c in r.cookies:
                print(f"  {c.name}: Secure={c.secure}")
        else:
            print("Cookies: Yok")
    except Exception as e:
        print(f"Hata: {e}")
    print()

audit("https://7xwon.com")
audit("https://api.7xwon.com/user")
audit("https://api.7xwon.com/main-events", method="POST", json_body={"type": 0, "odds": 1, "code": False, "s": 1, "t": False})
