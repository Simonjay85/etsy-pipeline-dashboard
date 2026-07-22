import urllib.request
import json

payload = json.dumps({"title":"", "keywords":"Wedding Safety Pack", "extra":""}).encode()
req = urllib.request.Request(
    f"http://127.0.0.1:8090/api/products/18/regen-seo", 
    data=payload, method="POST",
    headers={"Content-Type": "application/json"}
)
try:
    with urllib.request.urlopen(req) as resp:
        print(resp.read().decode())
except Exception as e:
    import urllib.error
    if isinstance(e, urllib.error.HTTPError):
        print(e.read().decode())
    else:
        print(e)
