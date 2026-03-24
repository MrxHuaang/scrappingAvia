"""Debug EASA keyword search."""
import requests
from bs4 import BeautifulSoup

BASE = "https://ad.easa.europa.eu"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

session = requests.Session()

# Test 1: Main page (this works)
print("=== Test 1: Main page ===")
resp = session.get(BASE, headers=HEADERS, timeout=30)
print(f"Status: {resp.status_code}")
soup = BeautifulSoup(resp.text, "lxml")
table = soup.find("table", class_="ad-list")
print(f"Found ad-list table: {table is not None}")
if table:
    rows = table.find_all("tr")
    print(f"Rows: {len(rows)}")

# Test 2: Advanced search with keyword
print("\n=== Test 2: Advanced search GET ===")
url = f"{BASE}/search/advanced/result/"
resp = session.get(url, params={"fi_keyword": "A320"}, headers=HEADERS, timeout=30)
print(f"Status: {resp.status_code}")
print(f"URL: {resp.url}")
soup = BeautifulSoup(resp.text, "lxml")
table = soup.find("table", class_="ad-list")
print(f"Found ad-list table: {table is not None}")
if table:
    rows = table.find_all("tr")
    print(f"Rows: {len(rows)}")
else:
    # What tables exist?
    tables = soup.find_all("table")
    print(f"Other tables found: {len(tables)}")
    for t in tables:
        print(f"  class={t.get('class')}, id={t.get('id')}, rows={len(t.find_all('tr'))}")

    # Check for any result indicator
    for text in ["record", "result", "publication", "displaying", "found"]:
        found = soup.find(string=lambda s: s and text.lower() in s.lower())
        if found:
            print(f"  Found text '{text}': {found.strip()[:100]}")

# Test 3: POST to advanced search
print("\n=== Test 3: Advanced search POST ===")
resp = session.post(url, data={"fi_keyword": "A320"}, headers=HEADERS, timeout=30)
print(f"Status: {resp.status_code}")
print(f"URL: {resp.url}")
soup = BeautifulSoup(resp.text, "lxml")
table = soup.find("table", class_="ad-list")
print(f"Found ad-list table: {table is not None}")
if table:
    rows = table.find_all("tr")
    print(f"Rows: {len(rows)}")
else:
    tables = soup.find_all("table")
    print(f"Other tables found: {len(tables)}")

# Test 4: Direct search with keyword in URL path
print("\n=== Test 4: Keyword in path ===")
resp = session.get(f"{BASE}/search/advanced", params={"fi_keyword": "A320"}, headers=HEADERS, timeout=30)
print(f"Status: {resp.status_code}")
soup = BeautifulSoup(resp.text, "lxml")
table = soup.find("table", class_="ad-list")
print(f"Found ad-list table: {table is not None}")

# Test 5: Try the non-advanced search
print("\n=== Test 5: Simple search ===")
resp = session.get(f"{BASE}/search", params={"q": "A320"}, headers=HEADERS, timeout=30)
print(f"Status: {resp.status_code}")
print(f"URL: {resp.url}")
soup = BeautifulSoup(resp.text, "lxml")
table = soup.find("table", class_="ad-list")
print(f"Found ad-list table: {table is not None}")
if table:
    rows = table.find_all("tr")
    print(f"Rows: {len(rows)}")

# Test 6: Check what body content we actually get from search
print("\n=== Test 6: Body snippet from search ===")
resp = session.get(url, params={"fi_keyword": "Boeing"}, headers=HEADERS, timeout=30)
print(f"Status: {resp.status_code}")
# Print first 2000 chars of body
body = resp.text[:3000]
print(body[:2000])
