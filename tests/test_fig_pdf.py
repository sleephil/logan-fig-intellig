from io import BytesIO
from pypdf import PdfReader
from fig_pdf import build

def test_pdf_has_landscape_pages_and_complete_profiles():
    report={"manager":{"name":"Example Manager"},"periods":["2026-06-30","2026-09-30"],
      "firms":[{"name":f"Firm {i:03d}","cik":str(i),"history":[
        {"period":"2026-06-30","reported_assets_usd":1000000,"change_usd":None,"year_change_usd":None,"status":"listed"},
        {"period":"2026-09-30","reported_assets_usd":1100000,"change_usd":100000,"year_change_usd":None,"status":"listed"}]} for i in range(40)],
      "limitations":["Reported holdings are not cash flows."],"coverage":{}}
    pdf=build(report)
    reader=PdfReader(BytesIO(pdf))
    assert len(reader.pages)>2
    assert all(float(page.mediabox.width)>float(page.mediabox.height) for page in reader.pages)
    content=" ".join(page.extract_text() for page in reader.pages)
    assert "Firm 000" in content and "Firm 039" in content
    assert "not cash flows" in content
