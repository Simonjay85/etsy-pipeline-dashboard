import urllib.request
import json
import time

failed_folders = ["product-15", "product-38", "product-39", "product-41"]

req = urllib.request.Request("http://127.0.0.1:8090/api/products")
with urllib.request.urlopen(req) as resp:
    products = json.loads(resp.read().decode())["products"]

for p in products:
    if p["folder"] in failed_folders:
        row = p["row"]
        print(f"Fixing {p['folder']} (Row {row})...")
        payload = json.dumps({"title":"", "keywords":p["keywords"], "extra":p["extra"]}).encode()
        regen_req = urllib.request.Request(
            f"http://127.0.0.1:8090/api/products/{row}/regen-seo", 
            data=payload, method="POST",
            headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(regen_req) as r_resp:
                result = json.loads(r_resp.read().decode())
                print(f"Success {p['folder']}: {result.get('title', '')}")
        except Exception as e:
            print(f"Failed {p['folder']}: {e}")
        time.sleep(1)
