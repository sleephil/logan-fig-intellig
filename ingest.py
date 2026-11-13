import os,time,requests,xml.etree.ElementTree as ET
SEC="https://data.sec.gov"
PLATFORMS={"Oppenheimer":"0001269119"}
def fetch(url):
    ua=os.getenv("SEC_USER_AGENT","")
    if "@" not in ua: raise RuntimeError("SEC_USER_AGENT must contain contact email")
    for n in range(4):
        r=requests.get(url,headers={"User-Agent":ua},timeout=30)
        if r.status_code in (429,500,502,503,504): time.sleep(2**n); continue
        r.raise_for_status(); time.sleep(.15); return r
    raise RuntimeError("SEC unavailable")
def submissions(cik):
    data=fetch(f"{SEC}/submissions/CIK{int(cik):010d}.json").json()
    recent=data["filings"]["recent"]
    records=[dict(zip(recent.keys(),v)) for v in zip(*recent.values())]
    for f in data["filings"].get("files",[]):
        if f.get("filingTo","")<os.getenv("HISTORY_START","2023-09-30"):continue
        old=fetch(f"{SEC}/submissions/{f['name']}").json()
        records.extend(dict(zip(old.keys(),v)) for v in zip(*old.values()))
    return [x for x in records if x.get("form") in ("13F-HR","13F-HR/A") and x.get("reportDate","")>=os.getenv("HISTORY_START","2023-09-30")]
def local(tag):return tag.rsplit("}",1)[-1].lower()
def parse_table(content):
    root=ET.fromstring(content);rows=[]
    for e in root.iter():
        if local(e.tag)!="infotable":continue
        d={local(x.tag):(x.text or "").strip() for x in e.iter() if x.text and x.text.strip()}
        rows.append((d.get("nameofissuer",""),d.get("cusip",""),d.get("titleofclass",""),int(d.get("value","0").replace(",","")),float(d.get("sshprnamt","0").replace(",",""))))
    return rows
def filing(cik,entry):
    acc=entry["accessionNumber"];base=f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-','')}/"
    index=fetch(base+"index.json").json();rows=[]
    for item in index["directory"]["item"]:
        name=item["name"]
        if not name.lower().endswith(".xml"):continue
        try:rows.extend(parse_table(fetch(base+name).content))
        except ET.ParseError:continue
    if not rows:raise RuntimeError("No holdings parsed; filing not stored")
    return rows,base+"index.json"
