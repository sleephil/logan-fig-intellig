"""Landscape, multi-page internal FIG report. No invented style classifications."""
from __future__ import annotations
from io import BytesIO
from xml.sax.saxutils import escape
from datetime import datetime, timezone
from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_RIGHT
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether

NAVY=colors.HexColor("#17364B")
TEAL=colors.HexColor("#3D8290")
GRAY=colors.HexColor("#536574")
PALE=colors.HexColor("#EAF0F3")
def s(x): return escape(str(x if x is not None else "Not reported"))
def usd(x):
    if x is None:return "N/A"
    v=float(x)
    return ("-" if v<0 else "")+"$"+f"{abs(v)/1e6:,.2f}m"
def build(report):
    buf=BytesIO()
    doc=SimpleDocTemplate(buf,pagesize=landscape(letter),leftMargin=38,rightMargin=38,topMargin=49,bottomMargin=42,title="Logan FIG Allocation Intelligence")
    styles=getSampleStyleSheet()
    styles.add(ParagraphStyle(name="FigTitle",parent=styles["Title"],fontSize=19,leading=24,textColor=NAVY,spaceAfter=12))
    styles.add(ParagraphStyle(name="FigHead",parent=styles["Heading2"],fontSize=12,leading=15,textColor=NAVY,spaceBefore=13,spaceAfter=7))
    styles.add(ParagraphStyle(name="FigBody",parent=styles["BodyText"],fontSize=8.3,leading=12,spaceAfter=6))
    styles.add(ParagraphStyle(name="FigCell",parent=styles["BodyText"],fontSize=7.4,leading=10))
    story=[]
    def p(text,style="FigBody"):return Paragraph(s(text),styles[style])
    def heading(t):story.append(p(t,"FigHead"))
    def table(headers,rows,widths=None):
        data=[[p(v,"FigCell") for v in headers]]+[[p(v,"FigCell") for v in row] for row in rows]
        t=Table(data,colWidths=widths,repeatRows=1,hAlign="LEFT")
        t.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),PALE),("TEXTCOLOR",(0,0),(-1,0),NAVY),("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,colors.HexColor("#F7F9FA")]),("VALIGN",(0,0),(-1,-1),"TOP"),("BOTTOMPADDING",(0,0),(-1,-1),7),("TOPPADDING",(0,0),(-1,-1),7),("LINEBELOW",(0,0),(-1,0),0.6,TEAL)]))
        story.append(t)
    manager=report.get("manager") or {}
    firms=report.get("firms") or []
    periods=report.get("periods") or []
    latest=periods[-1] if periods else None
    current=[]
    for firm in firms:
        h=next((x for x in reversed(firm.get("history",[])) if x.get("period")==latest),None)
        if h and h.get("reported_assets_usd") is not None:current.append((firm,h))
    current.sort(key=lambda x:-(x[1].get("reported_assets_usd") or 0))
    story.append(p("LOGAN CAPITAL MANAGEMENT  |  FIG INTELLIGENCE","FigBody"))
    story.append(p("Manager Allocation Intelligence","FigTitle"))
    story.append(p("Manager: "+str(manager.get("name") or manager.get("id") or "Unknown")+"  |  Latest reported quarter: "+str(latest or "Unavailable")))
    story.append(p("Generated UTC: "+datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")+"  |  Reporting entities: "+str(len(firms))+"  |  SEC-derived holdings; not verified client flows"))
    heading("Executive summary")
    story.append(p("Latest-quarter reported holdings across listed entities: "+usd(sum(h["reported_assets_usd"] for _,h in current))+". Firms with latest-quarter observations: "+str(len(current))+". History spans "+str(periods[0] if periods else "N/A")+" to "+str(latest or "N/A")+"." ))
    heading("By investment style")
    story.append(p("Investment-style classification is not present in this SEC manager relationship dataset. No growth/value/core categories are inferred."))
    heading("Largest reported allocations")
    table(["Reporting entity","CIK","Latest reported holdings","Quarterly change"],[[f["name"],f["cik"],usd(h.get("reported_assets_usd")),usd(h.get("change_usd"))] for f,h in current] or [["No observations","","",""]],[320,100,155,150])
    growing=sorted([(f,h) for f,h in current if h.get("change_usd") is not None and h["change_usd"]>0],key=lambda x:-x[1]["change_usd"])
    falling=sorted([(f,h) for f,h in current if h.get("change_usd") is not None and h["change_usd"]<0],key=lambda x:x[1]["change_usd"])
    for label,items in [("Growing reported allocations",growing),("Falling reported allocations",falling)]:
        heading(label)
        table(["Reporting entity","Reported holdings","Comparable quarterly change"],[[f["name"],usd(h["reported_assets_usd"]),usd(h["change_usd"])] for f,h in items] or [["No comparable changes","N/A","N/A"]],[370,175,180])
    story.append(PageBreak())
    heading("Reporting firm profiles and full historical observations")
    for i,firm in enumerate(sorted(firms,key=lambda f:f["name"].casefold())):
        history=firm.get("history") or []
        block=[p(str(i+1)+". "+firm["name"],"FigHead"),p("SEC CIK: "+str(firm.get("cik") or "N/A")+"  |  Observations: "+str(len(history)))]
        story.extend(block)
        table(["Quarter","Reported holdings","Quarterly change","Year-over-year change","Status"],[[h.get("period"),usd(h.get("reported_assets_usd")),usd(h.get("change_usd")),usd(h.get("year_change_usd")),h.get("status","N/A")] for h in history] or [["N/A","N/A","N/A","N/A","N/A"]],[115,155,155,155,145])
        for h in history:
            sources=h.get("sources") or []
            if sources:
                story.append(p("Source filings ("+str(h.get("period"))+"): "+", ".join(str(z.get("accession","")) for z in sources)))
        story.append(Spacer(1,11))
    heading("Coverage, methodology and limitations")
    coverage=report.get("coverage") or {}
    for k in ("history_start","last_period","archive_through","index_published_at"):
        if coverage.get(k):story.append(p(k.replace("_"," ").title()+": "+str(coverage[k])))
    for limitation in report.get("limitations") or []:story.append(p("- "+str(limitation)))
    def page(canvas,doc):
        canvas.saveState();w,h=landscape(letter)
        canvas.setStrokeColor(TEAL);canvas.line(38,h-34,w-38,h-34)
        canvas.setFont("Helvetica",7);canvas.setFillColor(GRAY)
        canvas.drawString(38,25,"LOGAN CAPITAL  |  INTERNAL RESEARCH  |  FIG Intelligence")
        canvas.setFont("Helvetica",6)
        canvas.drawCentredString(w/2,25,"\u00a9 2026 Stephen S. Lee. All rights reserved.")
        canvas.drawRightString(w-38,25,"Page "+str(doc.page));canvas.restoreState()
    doc.build(story,onFirstPage=page,onLaterPages=page)
    return buf.getvalue()
