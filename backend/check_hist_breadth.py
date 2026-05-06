import sys
sys.path.insert(0, ".")
from futu import *
from config import FUTU_HOST, FUTU_PORT

ctx = OpenQuoteContext(host=FUTU_HOST, port=FUTU_PORT)

# Method 1: Try daily kline - check if it has raise/fall fields
print("=== Method 1: Daily kline fields ===")
ret, data, _ = ctx.request_history_kline("HK.800000", start="2026-04-29", end="2026-04-29", ktype="K_DAY", max_count=5)
if ret == RET_OK:
    print("Columns:", data.columns.tolist())
    row = data.iloc[-1]
    for col in data.columns:
        val = row[col]
        if str(val) != "0" and str(val) != "0.0" and str(val) != "" and str(val) != "nan":
            print("  %s = %s" % (col, val))

# Method 2: Try get_plate_stock to count raise/fall manually
print("\n=== Method 2: Get HSI constituent stocks ===")
ret2, plate = ctx.get_plate_stock("HK.HSI_Constituent")
if ret2 == RET_OK:
    codes = plate["code"].tolist()
    print("HSI constituents: %d stocks" % len(codes))
    print("First 5:", codes[:5])

    # Get yesterday's daily kline for all constituents
    print("\nChecking yesterday's raise/fall for constituents...")
    raise_count = 0
    fall_count = 0
    equal_count = 0
    for code in codes:
        r, d, _ = ctx.request_history_kline(code, start="2026-04-29", end="2026-04-29", ktype="K_DAY", max_count=1)
        if r == RET_OK and len(d) > 0:
            row = d.iloc[0]
            chg = row["close"] - row.get("last_close", row["open"])
            if chg > 0:
                raise_count += 1
            elif chg < 0:
                fall_count += 1
            else:
                equal_count += 1
    print("Yesterday (2026-04-29): raise=%d fall=%d equal=%d" % (raise_count, fall_count, equal_count))
    ratio = raise_count / max(fall_count, 1)
    print("Ratio: %.2f" % ratio)
else:
    print("Failed to get plate stocks: %s" % str(plate))

# Method 3: Check if snapshot has historical data via other means
print("\n=== Method 3: Try get_stock_quote ===")
ctx.subscribe(["HK.800000"], [SubType.QUOTE])
ret3, quote = ctx.get_stock_quote(["HK.800000"])
if ret3 == RET_OK:
    print("Quote columns:", quote.columns.tolist())
    row = quote.iloc[0]
    for col in quote.columns:
        val = row[col]
        if str(val) != "0" and str(val) != "0.0" and str(val) != "" and str(val) != "nan" and str(val) != "N/A":
            print("  %s = %s" % (col, val))

ctx.close()
