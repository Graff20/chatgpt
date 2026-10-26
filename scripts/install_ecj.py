"""Verified alternative Java compiler for environments containing a JRE only."""
import pathlib,urllib.request,hashlib
url='https://repo.maven.apache.org/maven2/org/eclipse/jdt/ecj/3.38.0/ecj-3.38.0.jar';path=pathlib.Path('/tmp/ceh-ecj.jar')
if not path.exists():urllib.request.urlretrieve(url,path)
expected=urllib.request.urlopen(url+'.sha1',timeout=30).read().decode().strip()
if hashlib.sha1(path.read_bytes()).hexdigest()!=expected:raise SystemExit('ECJ checksum mismatch')
print('Verified ECJ compiler:',path)
