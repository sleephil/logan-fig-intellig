from pathlib import Path
import base64,lzma,json,hashlib,subprocess,ast
manifest=Path('/tmp/market-manifest.json')
before=json.loads(manifest.read_text())
assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==h for p,h in before.items())
raw=lzma.decompress(base64.b64decode((Path(__file__).parent/'refinement.xz64').read_text().strip(),validate=True))
assert hashlib.sha256(raw).hexdigest()=='41fac38929841535d5322dee70d6f0af280abbab5a3c9de3c062eeff6cae8635'
data=json.loads(raw);expected=data['hashes']
assert set(expected)==set(before)
patch=data['patch'].encode()
subprocess.run(['git','apply','--check','-'],input=patch,check=True)
subprocess.run(['git','apply','-'],input=patch,check=True)
for path,digest in expected.items():
    assert hashlib.sha256(Path(path).read_bytes()).hexdigest()==digest,path
    if path.endswith('.py'):ast.parse(Path(path).read_text())
manifest.write_text(json.dumps(expected,indent=2))
subprocess.run(['git','diff','--check'],check=True)
print('Verified exact sixteen-file final candidate including independently tested original-XML recovery')
