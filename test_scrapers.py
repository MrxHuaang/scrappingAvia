"""Quick test for all scrapers - v2."""

print("=" * 50)
print("TEST 1: FAA Scraper")
print("=" * 50)
try:
    from scraper_faa import _create_session, get_makes, search_ads
    session = _create_session()
    makes, session = get_makes(session)
    print(f"  Makes found: {len(makes)}")
    if makes:
        print(f"  First 5: {makes[:5]}")
    else:
        print("  No makes via API (likely Akamai blocked).")
        print("  Testing fallback search...")
        results = search_ads(make="Boeing", session=session)
        print(f"  Results: {len(results)}")
except Exception as e:
    print(f"  ERROR: {type(e).__name__}: {e}")

print()
print("=" * 50)
print("TEST 2: EASA Scraper")
print("=" * 50)
try:
    from scraper_easa import search_main_page, search_ads as easa_search
    results = search_main_page(max_pages=1)
    print(f"  Main page results: {len(results)}")
    for r in results[:5]:
        print(f"    AD: {r['AD_Number']} | Make: {r['Make'][:25]} | Model: {r['Model'][:20]}")

    if not results:
        print("  Trying keyword search for 'Boeing'...")
        results = easa_search(keyword="Boeing")
        print(f"  Keyword search results: {len(results)}")
        for r in results[:5]:
            print(f"    AD: {r['AD_Number']} | Make: {r['Make'][:25]} | Model: {r['Model'][:20]}")
except Exception as e:
    print(f"  ERROR: {type(e).__name__}: {e}")

print()
print("=" * 50)
print("TEST 3: Transport Canada Scraper")
print("=" * 50)
try:
    from scraper_tc import get_manufacturers, search_ads as tc_search
    mfrs, _ = get_manufacturers()
    print(f"  Manufacturers found: {len(mfrs)}")
    for m in mfrs[:5]:
        print(f"    - {m['text']}")

    # Test searching for Boeing
    boeing_mfrs = [m for m in mfrs if "boeing" in m["text"].lower()]
    if boeing_mfrs:
        print(f"\n  Boeing entries: {[m['text'] for m in boeing_mfrs]}")
except Exception as e:
    print(f"  ERROR: {type(e).__name__}: {e}")

print()
print("=" * 50)
print("TEST 4: CSV/Excel Export")
print("=" * 50)
try:
    import pandas as pd
    import os
    test_data = [
        {"Source": "TEST", "AD_Number": "TEST-001", "Subject": "Test", "Make": "Mfr", "Model": "M100"},
    ]
    os.makedirs("results", exist_ok=True)
    df = pd.DataFrame(test_data)
    df.to_csv("results/test_export.csv", index=False, encoding="utf-8-sig")
    print(f"  CSV export: OK")
    df.to_excel("results/test_export.xlsx", index=False, engine="openpyxl")
    print(f"  Excel export: OK")
    os.remove("results/test_export.csv")
    os.remove("results/test_export.xlsx")
    print("  Cleanup: OK")
except Exception as e:
    print(f"  ERROR: {type(e).__name__}: {e}")

print()
print("All tests complete!")
