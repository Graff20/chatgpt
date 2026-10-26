import urllib.request,xml.etree.ElementTree as ET,hashlib,zipfile,pathlib,shutil,concurrent.futures,os,argparse
p=argparse.ArgumentParser();p.add_argument('--sdk',default='/tmp/android-sdk');args=p.parse_args()
base='https://dl.google.com/android/repository/'
root=ET.fromstring(urllib.request.urlopen(base+'repository2-1.xml',timeout=30).read())
def install(spec):
 package=next(x for x in root.iter() if x.tag.endswith('remotePackage') and x.get('path')==spec)
 choices=[]
 for archive in package.iter('archive'):
  host=archive.findtext('host-os')
  if host in (None,'linux'):
   complete=archive.find('complete')
   if complete is not None:choices.append(complete)
 complete=choices[0];name=complete.findtext('url');expected=complete.findtext('checksum');algorithm=complete.find('checksum').get('type','sha1');dest=pathlib.Path('/tmp')/name.rsplit('/',1)[-1]
 if not dest.exists():urllib.request.urlretrieve(base+name,dest)
 actual=hashlib.new(algorithm,dest.read_bytes()).hexdigest()
 if actual!=expected:raise RuntimeError('Checksum mismatch '+name)
 tmp=pathlib.Path('/tmp/ceh-sdk-unpack')/spec.replace(';','-');tmp.mkdir(parents=True,exist_ok=True)
 with zipfile.ZipFile(dest) as z:z.extractall(tmp)
 parts=[p for p in tmp.iterdir() if p.is_dir()];out=pathlib.Path(args.sdk)/spec.replace(';','/');out.parent.mkdir(parents=True,exist_ok=True)
 if out.exists():shutil.rmtree(out)
 shutil.move(str(parts[0]),str(out))
 for p in out.rglob('*'):
  if p.is_file() and (p.name in ('aapt','aapt2','aidl','apksigner','d8','zipalign') or p.suffix=='.so'):p.chmod(0o755)
 print(spec,'verified and installed',flush=True)
with concurrent.futures.ThreadPoolExecutor() as pool:list(pool.map(install,['platforms;android-35','build-tools;35.0.0']))
