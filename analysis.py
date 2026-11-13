"""Evidence-based 13F trends. A filer is not an outside SMA manager roster."""
from collections import defaultdict

ETF_TERMS=(" ETF","ISHARES","SPDR","VANGUARD INDEX","INVESCO QQQ","SCHWAB STRATEGIC","DIMENSIONAL ETF")
GROWTH_TERMS=("GROWTH","NASDAQ","QQQ","RUSSELL 1000 GR")
VALUE_TERMS=("VALUE","RUSSELL 1000 VAL")
def style(issuer,title):
    text=(issuer+" "+title).upper()
    if any(x in text for x in ETF_TERMS): kind="ETF / fund exposure"
    else:kind="Individual security"
    if any(x in text for x in GROWTH_TERMS):bucket="Growth-labelled"
    elif any(x in text for x in VALUE_TERMS):bucket="Value-labelled"
    else:bucket="Unclassified"
    return kind,bucket

def analyze(period_rows):
    """Rows: period, issuer, cusip, title, reported_value_usd.
    Values are SEC 13F thousands of USD. Deduplicated by CUSIP/period.
    """
    periods=sorted(set(str(r[0]) for r in period_rows))
    by_period=defaultdict(dict)
    for period,issuer,cusip,title,value in period_rows:
        p=str(period);key=(cusip or issuer).upper().strip()
        old=by_period[p].get(key)
        if old is None:by_period[p][key]={"issuer":issuer,"cusip":cusip,"title":title,"value":int(value or 0)}
        else:old["value"]+=int(value or 0)
    items=[]
    for key in sorted(set(k for m in by_period.values() for k in m)):
        history=[]
        meta=next((by_period[p][key] for p in reversed(periods) if key in by_period[p]),None)
        if meta is None:continue
        for p in periods:history.append({"period":p,"reported_value_usd":by_period[p].get(key,{}).get("value",0)*1000})
        latest=history[-1]["reported_value_usd"];prior=history[-2]["reported_value_usd"] if len(history)>1 else None
        kind,bucket=style(meta["issuer"],meta["title"])
        items.append({"issuer":meta["issuer"],"cusip":meta["cusip"],"title":meta["title"],"type":kind,"style_hint":bucket,"latest_value_usd":latest,"previous_value_usd":prior,"change_usd":latest-prior if prior is not None else None,"history":history})
    items.sort(key=lambda x:x["latest_value_usd"],reverse=True)
    declines=sorted((x for x in items if x["change_usd"] is not None and x["change_usd"]<0),key=lambda x:x["change_usd"])
    increases=sorted((x for x in items if x["change_usd"] is not None and x["change_usd"]>0),key=lambda x:-x["change_usd"])
    signals=[]
    for outgoing in declines[:20]:
        for incoming in increases[:30]:
            if outgoing["cusip"]==incoming["cusip"]:continue
            if outgoing["style_hint"]=="Unclassified" or outgoing["style_hint"]!=incoming["style_hint"]:continue
            overlap=min(-outgoing["change_usd"],incoming["change_usd"])
            ratio=overlap/max(-outgoing["change_usd"],incoming["change_usd"])
            if ratio<0.3:continue
            signals.append({"declining":outgoing["issuer"],"increasing":incoming["issuer"],"style_hint":outgoing["style_hint"],"decline_usd":outgoing["change_usd"],"increase_usd":incoming["change_usd"],"magnitude_similarity":round(ratio,2),"interpretation":"Concurrent reported-value changes only; not verified flows or replacement"})
    signals.sort(key=lambda x:-x["magnitude_similarity"])
    return {"periods":periods,"positions":items,"possible_rotations":signals[:20],"limitations":["13F identifies filer holdings, not the platform's outside SMA managers.","Quarter-to-quarter market-price changes, trades and corporate actions are not separated.","Potential rotations are exploratory coincidences, not measured transfers."]}
