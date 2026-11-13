from pathlib import Path
import hashlib,json,ast
p=Path('tests/test_ingest.py');s=p.read_text()
old="'/api/analysis/0001269119']:";assert s.count(old)==1
s=s.replace(old,"'/api/analysis/0001269119','/api/managers/0001269119']:")
old="assert result['version']=='0.2.0' and '/fig-intelligence' in result['changes'][0]['changed_pages']";assert s.count(old)==1
s=s.replace(old,"assert result['version']=='0.3.0' and result['changes'][0]['version']==result['version']\n    assert '/fig-intelligence' in result['changes'][0]['changed_pages']")
p.write_text(s)
p=Path('verify_manager_sources.py');s=p.read_text()
old="    with storage.connect() as c:storage.setup(c)";assert s.count(old)==1
s=s.replace(old,"    with storage.connect() as c:\n        storage.setup(c)\n        # Remove only prior synthetic fixtures in the guarded disposable CI database.\n        c.execute('TRUNCATE fig_filings_v2,fig_import_runs,fig_import_events,fig_release_events RESTART IDENTITY')")
p.write_text(s)
manifest=Path('/tmp/manager-manifest.json');expected=json.loads(manifest.read_text())
for name,digest in {'tests/test_ingest.py':'b13c3bae9edb2467f46647429abf3fa3e25c4e08ee9c7746a796c41a0e0c8aad','verify_manager_sources.py':'48b1a947b8d0555247b2a19136c872cdea4a1d22f94f4001309129f2063b33d4'}.items():
    ast.parse(Path(name).read_text())
    assert hashlib.sha256(Path(name).read_bytes()).hexdigest()==digest,name
    expected[name]=digest
manifest.write_text(json.dumps(expected,indent=2))
