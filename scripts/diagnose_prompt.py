from pathlib import Path
import re
from playwright.sync_api import sync_playwright
prompt=Path('scripts/bubble_redesign_prompt.txt').read_text(encoding='utf-8')
def norm(s): return re.sub(r'\n{3,}','\n\n',s).strip()
with sync_playwright() as p:
 b=p.chromium.connect_over_cdp('http://127.0.0.1:9333');c=b.contexts[0];pg=c.new_page();pg.goto('https://chatgpt.com/',wait_until='domcontentloaded',timeout=120000);pg.wait_for_selector('#prompt-textarea',state='visible',timeout=120000);e=pg.locator('#prompt-textarea');e.fill(prompt);actual=e.evaluate("node => 'value' in node ? node.value : (node.innerText || node.textContent || '')");print('raw',len(prompt),len(actual),'norm',len(norm(prompt)),len(norm(actual)),'equal',norm(prompt)==norm(actual));
 a=norm(prompt);z=norm(actual)
 for i,(x,y) in enumerate(zip(a,z)):
  if x!=y: print('mismatch',i,repr(x),repr(y),'expected_context',repr(a[max(0,i-40):i+80]),'actual_context',repr(z[max(0,i-40):i+80]));break
 if len(a)!=len(z):print('remaining',repr(a[min(len(a),len(z)-20):min(len(a),len(z)+80)]),repr(z[min(len(a),len(z)-20):min(len(a),len(z)+80)]))
 pg.close();b.close()
